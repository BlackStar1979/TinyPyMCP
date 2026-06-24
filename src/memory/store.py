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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "memory.db"
DB_PATH = Path(os.environ.get("MCP_MEMORY_DB", str(_DEFAULT_DB)))

_EMBED_ENABLED = os.environ.get("MCP_MEMORY_EMBED", "1").strip().lower() not in ("0", "false", "no", "off")
_EMBED_MODEL = os.environ.get("MCP_EMBED_MODEL", "bge-m3")
_EMBED_DIM = int(os.environ.get("MCP_EMBED_DIM", "1024"))

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
