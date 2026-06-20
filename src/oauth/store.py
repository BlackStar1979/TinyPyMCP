"""
TinyPyMCP - OAuth 2.1 authorization-server storage (SQLite).

Persists registered clients (DCR), authorization codes (with PKCE), access and
refresh tokens. Backs the OAuthAuthorizationServerProvider implementation. Pure
(no MCP imports) so it's unit-testable. Pydantic models are stored as JSON blobs
so the schema doesn't churn as SDK model fields evolve.

DB path: MCP_OAUTH_DB env, else <project>/data/oauth.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "oauth.db"
DB_PATH = Path(os.environ.get("MCP_OAUTH_DB", str(_DEFAULT_DB)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    data      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code       TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token      TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token      TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auth (
    pid        TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


# ── Clients (DCR) ────────────────────────────────────────────────────────────

def put_client(client_id: str, data: dict[str, Any]) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO clients (client_id, data) VALUES (?, ?)",
                  (client_id, json.dumps(data)))
        c.commit()


def get_client(client_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    return json.loads(row["data"]) if row else None


# ── Authorization codes (single-use, short-lived) ────────────────────────────

def put_code(code: str, client_id: str, data: dict[str, Any], ttl: int = 600) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO auth_codes (code, client_id, data, expires_at) VALUES (?,?,?,?)",
                  (code, client_id, json.dumps(data), time.time() + ttl))
        c.commit()


def get_code(code: str) -> dict[str, Any] | None:
    """Peek an auth code without deleting (None if missing/expired)."""
    with _conn() as c:
        row = c.execute("SELECT data, expires_at FROM auth_codes WHERE code = ?", (code,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return json.loads(row["data"])


def delete_code(code: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
        c.commit()


# ── Pending authorizations (awaiting operator login) ─────────────────────────

def put_pending(pid: str, data: dict[str, Any], ttl: int = 600) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO pending_auth (pid, data, expires_at) VALUES (?,?,?)",
                  (pid, json.dumps(data), time.time() + ttl))
        c.commit()


def pop_pending(pid: str) -> dict[str, Any] | None:
    """Fetch and delete a pending authorization (single use)."""
    with _conn() as c:
        row = c.execute("SELECT data, expires_at FROM pending_auth WHERE pid = ?", (pid,)).fetchone()
        c.execute("DELETE FROM pending_auth WHERE pid = ?", (pid,))
        c.commit()
    if not row or row["expires_at"] < time.time():
        return None
    return json.loads(row["data"])


# ── Tokens ───────────────────────────────────────────────────────────────────

def put_access(token: str, client_id: str, data: dict[str, Any], ttl: int = 3600) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO access_tokens (token, client_id, data, expires_at) VALUES (?,?,?,?)",
                  (token, client_id, json.dumps(data), time.time() + ttl))
        c.commit()


def get_access(token: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data, expires_at FROM access_tokens WHERE token = ?", (token,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return json.loads(row["data"])


def put_refresh(token: str, client_id: str, data: dict[str, Any], ttl: int = 30 * 86400) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO refresh_tokens (token, client_id, data, expires_at) VALUES (?,?,?,?)",
                  (token, client_id, json.dumps(data), time.time() + ttl))
        c.commit()


def get_refresh(token: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT data, expires_at FROM refresh_tokens WHERE token = ?", (token,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return json.loads(row["data"])


def delete_token(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
        c.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
        c.commit()
