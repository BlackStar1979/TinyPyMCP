"""
TinyPyMCP - SIM job registry (stage 2: persist + state, still NO execution).

The compute-plane ADR puts the job registry + audit on the control plane while
heavy compute stays on a separate future plane. This module persists job
manifests and their state transitions to sqlite on /data (the backed-up volume),
append-only audited. It still does NOT execute anything: `submit` only records a
job as `pending_approval`. The approval -> queued -> running -> ... transitions
require a human-approval action that is intentionally NOT wired here yet (ADR:
no autonomous execution before manifest/validation/audit/approval/rollback rules
all exist).

DB: MCP_SIM_DB env, else /data/sim.db (same volume as memory.db/oauth.db).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import manifest as _manifest

_DB_PATH = Path(os.environ.get("MCP_SIM_DB", "/data/sim.db"))

# State machine (ADR §4). Stage 2 only ever creates `pending_approval`; the rest
# are declared so transitions/audit are schema-ready when the approval + compute
# layers land.
STATES = (
    "pending_approval", "approved", "queued", "running",
    "completed", "failed", "validated", "rejected", "archived",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_jobs (
            job_id     TEXT PRIMARY KEY,
            manifest   TEXT NOT NULL,
            state      TEXT NOT NULL,
            actor      TEXT,
            reason     TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_job_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id     TEXT NOT NULL,
            from_state TEXT,
            to_state   TEXT NOT NULL,
            actor      TEXT,
            reason     TEXT,
            ts         TEXT NOT NULL
        )
    """)
    return conn


def submit(manifest: Any, actor: str = "agent") -> dict[str, Any]:
    """Validate a manifest and persist it as a NEW job in `pending_approval`.
    Nothing is executed. Rejects an invalid manifest and a duplicate job_id."""
    v = _manifest.validate(manifest)
    if not v["ok"]:
        return {"ok": False, "errors": v["errors"], "note": "manifest invalid; not persisted"}
    job_id = manifest["job_id"]
    now = _now()
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM sim_jobs WHERE job_id=?", (job_id,)).fetchone()
        if exists:
            return {"ok": False, "errors": [f"job_id already exists: {job_id}"]}
        conn.execute(
            "INSERT INTO sim_jobs (job_id, manifest, state, actor, reason, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (job_id, json.dumps(manifest), "pending_approval", actor, None, now, now),
        )
        conn.execute(
            "INSERT INTO sim_job_events (job_id, from_state, to_state, actor, reason, ts)"
            " VALUES (?,?,?,?,?,?)",
            (job_id, None, "pending_approval", actor, "submitted", now),
        )
        conn.commit()
    return {
        "ok": True,
        "job_id": job_id,
        "state": "pending_approval",
        "executed": False,
        "note": "persisted to registry; NOT executed — approval + compute plane deferred (ADR)",
    }


def get(job_id: str) -> dict[str, Any]:
    """Read one job's state + manifest + audit trail."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sim_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "errors": [f"job not found: {job_id}"]}
        events = conn.execute(
            "SELECT from_state, to_state, actor, reason, ts FROM sim_job_events"
            " WHERE job_id=? ORDER BY id ASC", (job_id,),
        ).fetchall()
    return {
        "ok": True,
        "job": {
            "job_id": row["job_id"],
            "state": row["state"],
            "actor": row["actor"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "manifest": json.loads(row["manifest"]),
        },
        "events": [dict(e) for e in events],
    }


def list_jobs(state: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List jobs (newest first), optionally filtered by state."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        if state:
            rows = conn.execute(
                "SELECT job_id, state, created_at, updated_at FROM sim_jobs"
                " WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT job_id, state, created_at, updated_at FROM sim_jobs"
                " ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
    return {"ok": True, "count": len(rows), "jobs": [dict(r) for r in rows]}
