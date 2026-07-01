"""
TinyPyMCP - Persistent memory store (SQLite + sqlite-vec semantic search).

Agent state / memory entries / tasks backed by SQLite. Memory SEARCH is semantic
when available: each memory's content is embedded (OVH AI Endpoints bge-m3, 1024-d,
via src.ovh_ai_client on the clean-IP VPS) and stored in a sqlite-vec `vec0` table
keyed by memories.id; queries embed and KNN-rank by cosine. Falls back to keyword
token-overlap when sqlite-vec or the embedding provider is unavailable, so the
store never hard-depends on the network.

The base tables carry no embedding columns — the vector layer is a separate
`memory_vec` virtual table, added with no migration of the tables below.

Config: MCP_MEMORY_DB (db path), MCP_MEMORY_EMBED (1/0, default 1),
MCP_EMBED_MODEL (default bge-m3), MCP_EMBED_DIM (default 1024).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "memory.db"
DB_PATH = Path(os.environ.get("MCP_MEMORY_DB", str(_DEFAULT_DB)))

_EMBED_ENABLED = os.environ.get("MCP_MEMORY_EMBED", "1").strip().lower() not in ("0", "false", "no", "off")
_EMBED_MODEL = os.environ.get("MCP_EMBED_MODEL", "bge-m3")
_EMBED_DIM = int(os.environ.get("MCP_EMBED_DIM", "1024"))

# Semantic dedup-on-write: skip saving a near-duplicate of an existing non-archived
# memory of the SAME (agent, type) when cosine >= threshold. Keeps the shared store
# from bloating under concurrent/repeated writes (first hygiene brick for shared
# context). Nothing is deleted — the original is returned. Needs embeddings+vec.
_DEDUP_ENABLED = os.environ.get("MCP_MEMORY_DEDUP", "1").strip().lower() not in ("0", "false", "no", "off")
_DEDUP_THRESHOLD = float(os.environ.get("MCP_MEMORY_DEDUP_THRESHOLD", "0.97"))

# Embedding write-throttle: process-local min-interval gate so many concurrent
# writers don't stampede the OVH provider (429). Rate 0 = disabled. If the needed
# wait would exceed _EMBED_MAX_WAIT, the call skips embedding -> lexical fallback
# (never blocks unbounded). Sync (consistent with the blocking httpx in _embed).
_EMBED_RATE = float(os.environ.get("MCP_EMBED_RATE_PER_SEC", "8"))
_EMBED_MAX_WAIT = float(os.environ.get("MCP_EMBED_MAX_WAIT_S", "2.0"))
_embed_lock = threading.Lock()
_last_embed = [0.0]  # monotonic ts of the last permitted embed (list = mutable cell)


def _throttle_ok() -> bool:
    """Min-interval rate gate. Returns False if the wait would exceed the cap
    (caller degrades to lexical); otherwise sleeps the remainder and admits."""
    if _EMBED_RATE <= 0:
        return True
    min_interval = 1.0 / _EMBED_RATE
    with _embed_lock:
        now = time.monotonic()
        wait = _last_embed[0] + min_interval - now
        if wait > _EMBED_MAX_WAIT:
            return False
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_embed[0] = now
        return True

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    agent_name   TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    current_task TEXT NOT NULL DEFAULT '',
    context      TEXT NOT NULL DEFAULT '{}',   -- JSON object
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    agent_name  TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT 'fact',
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_name, is_archived);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    created_by  TEXT NOT NULL DEFAULT '',
    assigned_to TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 5,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""

_VEC_AVAILABLE: bool | None = None  # set on first _connect; cached per process


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── sqlite-vec + embeddings ──────────────────────────────────────────────────

def _load_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension into this connection and ensure memory_vec.
    Returns False (cached) if the extension or load-extension support is absent."""
    global _VEC_AVAILABLE
    try:
        import sqlite_vec  # bundled loadable extension
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec "
            f"USING vec0(memory_id TEXT, embedding FLOAT[{_EMBED_DIM}])"
        )
        _VEC_AVAILABLE = True
    except Exception:
        _VEC_AVAILABLE = False
    return bool(_VEC_AVAILABLE)


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _embed(text: str) -> list[float] | None:
    """Embed text via OVH bge-m3 (L2-normalized so vec0 L2 ~ cosine). None on any
    failure (no key / network / wrong dim) -> caller degrades to lexical."""
    if not _EMBED_ENABLED or not text:
        return None
    if not _throttle_ok():  # over the rate cap -> degrade to lexical, never block unbounded
        return None
    try:
        from src import ovh_ai_client
        r = ovh_ai_client.embeddings(text, model=_EMBED_MODEL)
        if not r.get("ok"):
            return None
        vecs = r.get("embeddings") or []
        v = vecs[0] if vecs else None
        if not v or len(v) != _EMBED_DIM:
            return None
        return _normalize(v)
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _load_vec(conn)
    return conn


# ── Agent state ────────────────────────────────────────────────────────────

def get_agent_state(agent_name: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM agent_state WHERE agent_name = ?", (agent_name,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["context"] = json.loads(d["context"])
    return d


def set_agent_state(
    agent_name: str,
    session_id: str | None = None,
    current_task: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert: merge provided fields over existing state, refresh updated_at."""
    with _connect() as conn:
        prev = conn.execute(
            "SELECT * FROM agent_state WHERE agent_name = ?", (agent_name,)
        ).fetchone()
        prev = dict(prev) if prev else {}
        merged = {
            "agent_name": agent_name,
            "session_id": session_id if session_id is not None else prev.get("session_id", ""),
            "current_task": current_task if current_task is not None else prev.get("current_task", ""),
            "context": json.dumps(
                context if context is not None else json.loads(prev.get("context", "{}"))
            ),
            "updated_at": _now(),
        }
        conn.execute(
            """INSERT INTO agent_state (agent_name, session_id, current_task, context, updated_at)
               VALUES (:agent_name, :session_id, :current_task, :context, :updated_at)
               ON CONFLICT(agent_name) DO UPDATE SET
                 session_id=excluded.session_id,
                 current_task=excluded.current_task,
                 context=excluded.context,
                 updated_at=excluded.updated_at""",
            merged,
        )
        conn.commit()
    merged["context"] = json.loads(merged["context"])
    return merged


# ── Memory entries ───────────────────────────────────────────────────────────

def _find_duplicate(conn: sqlite3.Connection, vec: list[float], agent_name: str,
                    type_: str, threshold: float) -> dict[str, Any] | None:
    """Nearest existing non-archived memory of the SAME (agent, type) with cosine
    >= threshold, or None. KNN is sorted by ascending distance (descending cosine),
    so we can stop at the first below-threshold candidate."""
    try:
        knn = conn.execute(
            "SELECT memory_id, distance FROM memory_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_pack(vec), 5),
        ).fetchall()
    except Exception:
        return None
    for r in knn:
        sim = max(0.0, 1.0 - (r["distance"] * r["distance"]) / 2.0)
        if sim < threshold:
            break
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND is_archived = 0 AND agent_name = ? AND type = ?",
            (r["memory_id"], agent_name, type_),
        ).fetchone()
        if row:
            d = dict(row)
            d["_similarity"] = round(sim, 4)
            return d
    return None


def save_memory(
    content: str,
    agent_name: str = "",
    type: str = "fact",
    category: str = "",
) -> dict[str, Any]:
    entry = {
        "id": _new_id(),
        "agent_name": agent_name,
        "type": type,
        "content": content,
        "category": category,
        "is_archived": 0,
        "created_at": _now(),
    }
    vec = _embed((content + " " + category).strip())  # network call before the txn
    with _connect() as conn:
        # Semantic dedup-on-write: a near-identical memory already exists -> return
        # it instead of inserting a duplicate (store hygiene; never deletes).
        if _DEDUP_ENABLED and vec is not None and _VEC_AVAILABLE:
            dup = _find_duplicate(conn, vec, agent_name, type, _DEDUP_THRESHOLD)
            if dup is not None:
                sim = dup.pop("_similarity", None)
                dup["embedded"] = True
                dup["deduped"] = True
                dup["similarity"] = sim
                return dup
        conn.execute(
            """INSERT INTO memories (id, agent_name, type, content, category, is_archived, created_at)
               VALUES (:id, :agent_name, :type, :content, :category, :is_archived, :created_at)""",
            entry,
        )
        if vec is not None and _VEC_AVAILABLE:
            try:
                conn.execute(
                    "INSERT INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
                    (entry["id"], _pack(vec)),
                )
            except Exception:
                pass
        conn.commit()
    entry["embedded"] = vec is not None and bool(_VEC_AVAILABLE)
    return entry


def _score(content: str, category: str, tokens: list[str]) -> float:
    """Fraction of query tokens present in content+category. Mirrors mcp-tests."""
    if not tokens:
        return 0.0
    hay = (content + " " + category).lower()
    hits = sum(1 for t in tokens if t in hay)
    return hits / len(tokens)


def _search_lexical(conn: sqlite3.Connection, query: str, agent_name: str,
                    top_k: int, min_score: float) -> dict[str, Any]:
    if agent_name:
        rows = conn.execute(
            "SELECT * FROM memories WHERE is_archived = 0 AND agent_name = ?", (agent_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM memories WHERE is_archived = 0").fetchall()
    tokens = [t for t in query.lower().split() if len(t) > 1]
    scored = []
    for r in rows:
        s = _score(r["content"], r["category"], tokens)
        if s >= min_score:
            d = dict(r)
            d["score"] = s
            scored.append(d)
    scored.sort(key=lambda e: e["score"], reverse=True)
    return {"results": scored[:top_k], "total_searched": len(rows), "mode": "lexical"}


def search_memory(
    query: str,
    agent_name: str = "",
    top_k: int = 5,
    min_score: float = 0.1,
) -> dict[str, Any]:
    """Semantic KNN (sqlite-vec + bge-m3) when available; else keyword overlap."""
    qvec = _embed(query)
    with _connect() as conn:
        if not (_VEC_AVAILABLE and qvec is not None):
            return _search_lexical(conn, query, agent_name, top_k, min_score)

        # Over-fetch by KNN, then filter (archived/agent) and re-threshold by cosine.
        k = max(top_k * 4, 20)
        knn = conn.execute(
            "SELECT memory_id, distance FROM memory_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_pack(qvec), k),
        ).fetchall()
        dist = {r["memory_id"]: r["distance"] for r in knn}
        ids = list(dist.keys())
        if not ids:
            return {"results": [], "total_searched": 0, "mode": "semantic"}
        ph = ",".join("?" * len(ids))
        sql = f"SELECT * FROM memories WHERE is_archived = 0 AND id IN ({ph})"
        params: list[Any] = list(ids)
        if agent_name:
            sql += " AND agent_name = ?"
            params.append(agent_name)
        rows = conn.execute(sql, params).fetchall()
        scored = []
        for r in rows:
            d = dist.get(r["id"], 4.0)
            sim = max(0.0, 1.0 - (d * d) / 2.0)  # cosine for unit vectors (L2^2 = 2-2cos)
            if sim >= min_score:
                e = dict(r)
                e["score"] = round(sim, 4)
                scored.append(e)
        scored.sort(key=lambda e: e["score"], reverse=True)
        return {"results": scored[:top_k], "total_searched": len(ids), "mode": "semantic"}


def reindex_embeddings(agent_name: str = "", limit: int = 1000) -> dict[str, Any]:
    """Backfill memory_vec for memories that lack an embedding (e.g. saved while
    the provider was off). Returns counts. Idempotent."""
    with _connect() as conn:
        if not _VEC_AVAILABLE:
            return {"ok": False, "error": "sqlite-vec not available in this build"}
        q = "SELECT id, content, category FROM memories WHERE is_archived = 0"
        params: list[Any] = []
        if agent_name:
            q += " AND agent_name = ?"
            params.append(agent_name)
        rows = conn.execute(q, params).fetchall()
        have = {r["memory_id"] for r in conn.execute("SELECT memory_id FROM memory_vec").fetchall()}
        indexed = failed = 0
        for r in rows:
            if r["id"] in have:
                continue
            if indexed >= limit:
                break
            v = _embed((r["content"] + " " + (r["category"] or "")).strip())
            if v is None:
                failed += 1
                continue
            conn.execute("INSERT INTO memory_vec (memory_id, embedding) VALUES (?, ?)", (r["id"], _pack(v)))
            indexed += 1
        conn.commit()
    return {"ok": True, "indexed": indexed, "failed": failed, "candidates": len(rows)}


def stats() -> dict[str, Any]:
    """Read-only health snapshot of the shared memory store: counts (total /
    active / archived), embedding coverage, breakdown by type and top agents,
    and the hygiene config (dedup, embed rate). Pure COUNTs — cheap, safe."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM memories WHERE is_archived=0").fetchone()["c"]
        by_type = {r["type"]: r["c"] for r in conn.execute(
            "SELECT type, COUNT(*) c FROM memories WHERE is_archived=0 GROUP BY type ORDER BY c DESC"
        ).fetchall()}
        by_agent = {(r["agent_name"] or "(none)"): r["c"] for r in conn.execute(
            "SELECT agent_name, COUNT(*) c FROM memories WHERE is_archived=0 "
            "GROUP BY agent_name ORDER BY c DESC LIMIT 20"
        ).fetchall()}
        embedded = 0
        if _VEC_AVAILABLE:
            try:
                embedded = conn.execute("SELECT COUNT(*) c FROM memory_vec").fetchone()["c"]
            except Exception:
                embedded = 0
        tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    return {
        "ok": True,
        "memories_total": total,
        "memories_active": active,
        "memories_archived": total - active,
        "embedded": embedded,
        "embed_coverage": round(embedded / active, 3) if active else 0.0,
        "vec_available": bool(_VEC_AVAILABLE),
        "by_type": by_type,
        "by_agent": by_agent,
        "tasks": tasks,
        "dedup_enabled": _DEDUP_ENABLED,
        "dedup_threshold": _DEDUP_THRESHOLD,
        "embed_rate_per_sec": _EMBED_RATE,
    }


# ── ADR-as-memory (architectural decisions as first-class, searchable memory) ─

def save_adr(
    title: str,
    decision: str,
    context: str = "",
    consequences: str = "",
    alternatives: str = "",
    status: str = "accepted",
    agent_name: str = "",
) -> dict[str, Any]:
    """Persist an Architecture Decision Record as a structured memory (type='adr').
    Embedded like any memory, so it surfaces in semantic memory_search and is
    listable via list_adrs. Keeps decisions first-class (romionology discipline)."""
    content = (
        f"# ADR: {title}\n"
        f"Status: {status}\n"
        f"Date: {_now()}\n\n"
        f"## Context\n{context or '(none)'}\n\n"
        f"## Decision\n{decision}\n\n"
        f"## Alternatives considered\n{alternatives or '(none)'}\n\n"
        f"## Consequences\n{consequences or '(none)'}\n"
    )
    entry = save_memory(content, agent_name=agent_name, type="adr", category="adr")
    entry["title"] = title
    entry["status"] = status
    return entry


def list_adrs(agent_name: str = "", limit: int = 50) -> dict[str, Any]:
    """List ADRs (newest first), parsing title/status from the structured content."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        q = "SELECT id, content, created_at FROM memories WHERE type='adr' AND is_archived=0"
        params: list[Any] = []
        if agent_name:
            q += " AND agent_name=?"
            params.append(agent_name)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        c = r["content"] or ""
        first = c.split("\n", 1)[0]
        title = first[len("# ADR:"):].strip() if first.startswith("# ADR:") else "(untitled)"
        status = ""
        for line in c.split("\n")[:4]:
            if line.lower().startswith("status:"):
                status = line.split(":", 1)[1].strip()
                break
        out.append({"id": r["id"], "title": title, "status": status, "created_at": r["created_at"]})
    return {"ok": True, "count": len(out), "adrs": out}


# ── Tasks ──────────────────────────────────────────────────────────────────

def create_task(
    title: str,
    created_by: str = "",
    assigned_to: str = "",
    description: str = "",
    priority: int = 5,
) -> dict[str, Any]:
    task = {
        "id": _new_id(),
        "created_by": created_by,
        "assigned_to": assigned_to,
        "title": title,
        "description": description,
        "priority": priority,
        "status": "pending",
        "created_at": _now(),
    }
    with _connect() as conn:
        conn.execute(
            """INSERT INTO tasks (id, created_by, assigned_to, title, description, priority, status, created_at)
               VALUES (:id, :created_by, :assigned_to, :title, :description, :priority, :status, :created_at)""",
            task,
        )
        conn.commit()
    return task


def get_tasks(
    assigned_to: str = "",
    status: str = "pending",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Higher priority first, newest first within priority. Unassigned tasks
    are included for any assignee filter (mirrors mcp-tests)."""
    with _connect() as conn:
        if assigned_to:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status = ? AND (assigned_to = ? OR assigned_to = '')
                   ORDER BY priority DESC, created_at DESC LIMIT ?""",
                (status, assigned_to, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE status = ?
                   ORDER BY priority DESC, created_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
    return [dict(r) for r in rows]
