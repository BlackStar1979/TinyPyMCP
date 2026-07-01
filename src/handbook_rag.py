"""
TinyPyMCP — handbook RAG store (SEPARATE from agent memory).

A dedicated, incrementally-maintained retrieval index over the autonomous-LLM
handbook's technique cards. Kept apart from the agent memory store on purpose:
~100+ prose cards must not pollute operational agent memory.

Design (per 3 architecture consultations + OVH verification):
  * chunk identity = the card's stable `slug` (frontmatter id), NOT position/hash.
  * `embedding_input_hash` (over the CONTROLLED embed text) drives re-embed: a card
    is re-embedded ONLY when this hash changes; a pure metadata edit does not.
  * `content_hash` (whole card) = change detection for the ingest delta.
  * hybrid retrieval: dense (OVH bge-m3, dense-only) + lexical (local SQLite FTS5)
    fused; OVH gives no sparse, so lexical is built locally.
  * tombstone (status='tombstoned') instead of hard-delete; retrieval filters active.

The embedder is INJECTED (`embed_batch`) so the store is pure and testable without
network/sqlite-vec; the OVH wiring lives in the server layer.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "handbook_rag.db"
DB_PATH = Path(os.environ.get("MCP_HANDBOOK_DB", str(_DEFAULT_DB)))
_EMBED_DIM = int(os.environ.get("MCP_EMBED_DIM", "1024"))

# embed_batch(texts) -> list of (vec | None), same length/order as texts.
EmbedBatch = Callable[[Sequence[str]], list[Optional[list[float]]]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    slug                 TEXT PRIMARY KEY,       -- frontmatter id (stable chunk_id)
    title                TEXT NOT NULL DEFAULT '',
    layer                TEXT NOT NULL DEFAULT '',
    maturity             TEXT NOT NULL DEFAULT '',
    card_type            TEXT NOT NULL DEFAULT 'technika',
    language             TEXT NOT NULL DEFAULT 'pl',
    aliases              TEXT NOT NULL DEFAULT '[]',   -- JSON array
    triggers             TEXT NOT NULL DEFAULT '[]',
    anti_triggers        TEXT NOT NULL DEFAULT '[]',
    source_path          TEXT NOT NULL DEFAULT '',
    display_text         TEXT NOT NULL DEFAULT '',     -- raw card body (for the reader)
    retrieval_text       TEXT NOT NULL DEFAULT '',     -- controlled text that was embedded
    content_hash         TEXT NOT NULL DEFAULT '',
    embedding_input_hash TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'active', -- active | tombstoned
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status, layer, maturity);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    slug UNINDEXED, title, aliases, triggers, anti_triggers, body,
    tokenize = 'unicode61'
);
"""

_VEC_AVAILABLE: bool | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _load_vec(conn: sqlite3.Connection) -> bool:
    global _VEC_AVAILABLE
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS cards_vec "
            f"USING vec0(slug TEXT, embedding FLOAT[{_EMBED_DIM}])"
        )
        _VEC_AVAILABLE = True
    except Exception:
        _VEC_AVAILABLE = False
    return bool(_VEC_AVAILABLE)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _load_vec(conn)
    return conn


def build_embedding_input(card: dict[str, Any]) -> str:
    """Controlled text fed to the embedder (NOT raw markdown). Metadata-semantic:
    the fields that carry retrieval meaning, in a stable order. Changing this
    template changes every card's embedding_input_hash (intentional full re-embed)."""
    def _lst(k: str) -> str:
        v = card.get(k) or []
        return "; ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    # triggers/anti_triggers are DELIBERATELY excluded from the dense input: the
    # eval harness (scripts/eval_handbook.py) showed that embedding them dilutes the
    # card's core identity and drops top1 (-2pp) while only helping recall. They live
    # in the FTS/lexical lane instead (cards_fts) — that keeps recall (+5pp) without
    # the top1 penalty. Dense stays on the card's identity: title/aliases/layer/problem.
    parts = [
        f"Title: {card.get('title', '')}",
        f"Aliases: {_lst('aliases')}",
        f"Layer: {card.get('layer', '')}",
        f"Maturity: {card.get('maturity', '')}",
        f"Problem: {card.get('problem', '')}",
    ]
    return "\n".join(parts).strip()


def _card_row(card: dict[str, Any], retrieval_text: str) -> dict[str, Any]:
    return {
        "slug": card["slug"],
        "title": card.get("title", ""),
        "layer": card.get("layer", ""),
        "maturity": card.get("maturity", ""),
        "card_type": card.get("card_type", "technika"),
        "language": card.get("language", "pl"),
        "aliases": json.dumps(card.get("aliases") or [], ensure_ascii=False),
        "triggers": json.dumps(card.get("triggers") or [], ensure_ascii=False),
        "anti_triggers": json.dumps(card.get("anti_triggers") or [], ensure_ascii=False),
        "source_path": card.get("source_path", ""),
        "display_text": card.get("body", ""),
        "retrieval_text": retrieval_text,
        "content_hash": _sha(json.dumps(card, sort_keys=True, ensure_ascii=False, default=str)),
        "embedding_input_hash": _sha(retrieval_text),
        "status": "active",
        "updated_at": _now(),
    }


def _fts_delete(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM cards_fts WHERE slug = ?", (slug,))


def _fts_insert(conn: sqlite3.Connection, row: dict[str, Any], card: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO cards_fts (slug, title, aliases, triggers, anti_triggers, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["slug"], row["title"], " ".join(card.get("aliases") or []),
         " ".join(card.get("triggers") or []), " ".join(card.get("anti_triggers") or []),
         card.get("body", "")),
    )


def ingest(cards: list[dict[str, Any]], embed_batch: EmbedBatch,
           *, prune_missing: bool = False, batch_size: int = 25) -> dict[str, Any]:
    """Incremental upsert of cards keyed by slug. Re-embeds ONLY cards whose
    embedding_input_hash changed (or new). `prune_missing` tombstones active cards
    whose slug is absent from `cards` (full-corpus sync). Returns counts."""
    seen: set[str] = set()
    to_embed: list[tuple[dict[str, Any], dict[str, Any]]] = []  # (row, card)
    inserted = updated = unchanged = 0
    with _connect() as conn:
        for card in cards:
            slug = card.get("slug")
            if not slug:
                continue
            seen.add(slug)
            rt = build_embedding_input(card)
            row = _card_row(card, rt)
            prev = conn.execute(
                "SELECT embedding_input_hash, content_hash FROM cards WHERE slug = ?", (slug,)
            ).fetchone()
            if prev is None:
                inserted += 1
                _upsert_card(conn, row)
                _fts_delete(conn, slug)
                _fts_insert(conn, row, card)
                to_embed.append((row, card))
            elif prev["embedding_input_hash"] != row["embedding_input_hash"]:
                updated += 1
                _upsert_card(conn, row)
                _fts_delete(conn, slug)
                _fts_insert(conn, row, card)
                to_embed.append((row, card))
            elif prev["content_hash"] != row["content_hash"]:
                # metadata/body changed but embed input identical -> refresh row+FTS, NO re-embed
                updated += 1
                _upsert_card(conn, row)
                _fts_delete(conn, slug)
                _fts_insert(conn, row, card)
            else:
                unchanged += 1

        embedded = 0
        if to_embed and _VEC_AVAILABLE:
            for i in range(0, len(to_embed), max(1, batch_size)):
                chunk = to_embed[i:i + batch_size]
                vecs = embed_batch([r["retrieval_text"] for r, _ in chunk])
                for (row, _), vec in zip(chunk, vecs):
                    if vec is None:
                        continue
                    conn.execute("DELETE FROM cards_vec WHERE slug = ?", (row["slug"],))
                    conn.execute("INSERT INTO cards_vec (slug, embedding) VALUES (?, ?)",
                                 (row["slug"], _pack(vec)))
                    embedded += 1

        tombstoned = 0
        if prune_missing:
            active = conn.execute("SELECT slug FROM cards WHERE status='active'").fetchall()
            for r in active:
                if r["slug"] not in seen:
                    conn.execute("UPDATE cards SET status='tombstoned', updated_at=? WHERE slug=?",
                                 (_now(), r["slug"]))
                    _fts_delete(conn, r["slug"])
                    if _VEC_AVAILABLE:
                        conn.execute("DELETE FROM cards_vec WHERE slug = ?", (r["slug"],))
                    tombstoned += 1
        conn.commit()
    return {"ok": True, "inserted": inserted, "updated": updated, "unchanged": unchanged,
            "embedded": embedded, "tombstoned": tombstoned, "vec_available": bool(_VEC_AVAILABLE)}


def _upsert_card(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ",".join(row.keys())
    ph = ",".join(f":{k}" for k in row)
    setc = ",".join(f"{k}=excluded.{k}" for k in row if k != "slug")
    conn.execute(
        f"INSERT INTO cards ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(slug) DO UPDATE SET {setc}",
        row,
    )


import re as _re


def _fts_query(query: str) -> str:
    """Sanitize a natural-language query into a safe FTS5 OR-query (recall-first).
    Avoids FTS5 special-syntax errors on raw user input."""
    toks = _re.findall(r"\w+", query.lower(), _re.UNICODE)
    return " OR ".join(toks) if toks else ""


def search(query: str, embed_batch: EmbedBatch, *, top_k: int = 5,
           layer: str = "", maturity: str = "", k_rrf: int = 60,
           candidates: int = 50) -> dict[str, Any]:
    """Hybrid retrieval: local FTS5 (lexical) + vec0 dense (OVH-embedded query),
    fused with Reciprocal Rank Fusion (two-stage: collect candidate slugs per lane,
    fuse in Python, then fetch rows — sidesteps the vec0 JOIN+WHERE limitation).
    Returns ranked cards with per-lane signals (decision-with-audit)."""
    with _connect() as conn:
        lexical: list[str] = []
        fq = _fts_query(query)
        if fq:
            try:
                lexical = [r["slug"] for r in conn.execute(
                    "SELECT slug FROM cards_fts WHERE cards_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fq, candidates)).fetchall()]
            except Exception:
                lexical = []

        dense: list[str] = []
        if _VEC_AVAILABLE:
            qv = embed_batch([query])[0] if query else None
            if qv is not None:
                try:
                    dense = [r["slug"] for r in conn.execute(
                        "SELECT slug, distance FROM cards_vec WHERE embedding MATCH ? AND k = ? "
                        "ORDER BY distance", (_pack(qv), candidates)).fetchall()]
                except Exception:
                    dense = []

        scores: dict[str, float] = {}
        for rank, slug in enumerate(lexical):
            scores[slug] = scores.get(slug, 0.0) + 1.0 / (k_rrf + rank + 1)
        for rank, slug in enumerate(dense):
            scores[slug] = scores.get(slug, 0.0) + 1.0 / (k_rrf + rank + 1)
        if not scores:
            return {"ok": True, "mode": "hybrid", "results": [],
                    "lexical_n": len(lexical), "dense_n": len(dense)}

        lex_set, den_set = set(lexical), set(dense)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[dict[str, Any]] = []
        for slug, sc in ranked:
            r = conn.execute(
                "SELECT slug, title, layer, maturity, card_type, display_text "
                "FROM cards WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
            if r is None:
                continue
            if layer and r["layer"] != layer:
                continue
            if maturity and r["maturity"] != maturity:
                continue
            e = dict(r)
            e["score"] = round(sc, 5)
            e["in_lexical"] = slug in lex_set
            e["in_dense"] = slug in den_set
            out.append(e)
            if len(out) >= top_k:
                break
    return {"ok": True, "mode": "hybrid", "results": out,
            "lexical_n": len(lexical), "dense_n": len(dense)}


def parse_card(text: str) -> tuple[Optional[dict[str, Any]], str]:
    """Split a card into (frontmatter dict, body). Minimal YAML: top-level
    `key: value` and inline `key: [a, b]` lists; nested blocks (related:) skipped."""
    m = _re.match(r"^---\n(.*?)\n---\n(.*)$", text, _re.S)
    if not m:
        return None, ""
    fm: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        mm = _re.match(r"^([a-z_]+):\s*(.*)$", line)
        if not mm:
            continue
        k, v = mm.group(1), mm.group(2).strip()
        if v.startswith("[") and v.endswith("]"):
            fm[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            fm[k] = v.strip("\"'")
    return fm, m.group(2)


def _body_field(body: str, label: str) -> str:
    mm = _re.search(r"^\s*" + label + r":\s*(.+?)(?:\n\n|\Z)", body, _re.S | _re.M)
    return " ".join(mm.group(1).split()) if mm else ""


def cards_from_dir(cards_dir: str) -> list[dict[str, Any]]:
    """Parse every frontmattered technique card in a directory into ingest dicts.
    `problem` = Sedno + Problem lines from the body (embedding-input signal)."""
    import glob
    import os
    out: list[dict[str, Any]] = []
    for p in sorted(glob.glob(os.path.join(cards_dir, "*.md"))):
        if os.path.basename(p) == "000_CARD_TEMPLATE.md":
            continue
        fm, body = parse_card(Path(p).read_text(encoding="utf-8"))
        if not fm or not fm.get("id"):
            continue
        problem = (_body_field(body, "Sedno") + " " + _body_field(body, "Problem")).strip()
        out.append({
            "slug": fm["id"], "title": fm.get("title", ""), "layer": fm.get("layer", ""),
            "maturity": fm.get("maturity", ""), "card_type": fm.get("card_type", "technika"),
            "language": fm.get("language", "pl"), "aliases": fm.get("aliases", []),
            "triggers": fm.get("triggers", []), "anti_triggers": fm.get("anti_triggers", []),
            "source_path": os.path.basename(p), "problem": problem, "body": body.strip(),
        })
    return out


def stats() -> dict[str, Any]:
    with _connect() as conn:
        active = conn.execute("SELECT COUNT(*) c FROM cards WHERE status='active'").fetchone()["c"]
        tomb = conn.execute("SELECT COUNT(*) c FROM cards WHERE status='tombstoned'").fetchone()["c"]
        embedded = 0
        if _VEC_AVAILABLE:
            try:
                embedded = conn.execute("SELECT COUNT(*) c FROM cards_vec").fetchone()["c"]
            except Exception:
                embedded = 0
        by_layer = {r["layer"] or "?": r["c"] for r in conn.execute(
            "SELECT layer, COUNT(*) c FROM cards WHERE status='active' GROUP BY layer ORDER BY c DESC"
        ).fetchall()}
    return {"ok": True, "active": active, "tombstoned": tomb, "embedded": embedded,
            "embed_coverage": round(embedded / active, 3) if active else 0.0,
            "by_layer": by_layer, "vec_available": bool(_VEC_AVAILABLE)}
