"""
TinyPyMCP - Persistent memory store (SQLite, no embeddings yet).

Mirrors the mcp-tests workbench memory shape (agent state / memory entries /
tasks) but backed by SQLite instead of JSON+JSONL. Search is keyword
token-overlap scoring, identical in spirit to the reference.

Schema is intentionally split so a future embeddings layer (sqlite-vec) can be
added as a separate `embeddings` table keyed by memories.id, with no migration
of the tables defined here.

DB path: MCP_MEMORY_DB env var, else <project>/data/memory.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "memory.db"
DB_PATH = Path(os.environ.get("MCP_MEMORY_DB", str(_DEFAULT_DB)))

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
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
    with _connect() as conn:
        conn.execute(
            """INSERT INTO memories (id, agent_name, type, content, category, is_archived, created_at)
               VALUES (:id, :agent_name, :type, :content, :category, :is_archived, :created_at)""",
            entry,
        )
        conn.commit()
    return entry


def _score(content: str, category: str, tokens: list[str]) -> float:
    """Fraction of query tokens present in content+category. Mirrors mcp-tests."""
    if not tokens:
        return 0.0
    hay = (content + " " + category).lower()
    hits = sum(1 for t in tokens if t in hay)
    return hits / len(tokens)


def search_memory(
    query: str,
    agent_name: str = "",
    top_k: int = 5,
    min_score: float = 0.1,
) -> dict[str, Any]:
    with _connect() as conn:
        if agent_name:
            rows = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0 AND agent_name = ?",
                (agent_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE is_archived = 0"
            ).fetchall()

    tokens = [t for t in query.lower().split() if len(t) > 1]
    scored = []
    for r in rows:
        s = _score(r["content"], r["category"], tokens)
        if s >= min_score:
            d = dict(r)
            d["score"] = s
            scored.append(d)
    scored.sort(key=lambda e: e["score"], reverse=True)
    return {"results": scored[:top_k], "total_searched": len(rows)}


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
