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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_artifacts (
            artifact_id     TEXT PRIMARY KEY,
            job_id          TEXT NOT NULL,
            kind            TEXT NOT NULL,
            sha256          TEXT NOT NULL,
            engine_version  TEXT,
            bytes           INTEGER,
            retention_class TEXT NOT NULL DEFAULT 'standard',
            r2_path         TEXT,
            created_at      TEXT NOT NULL
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


# Allowed state transitions (ADR §4). Stage 2 only wires pending_approval ->
# approved|rejected (the human gate); the rest are declared for when the compute
# layer lands. NO transition is autonomous — approve/reject is a human action via
# a session-gated route, never an agent tool.
_TRANSITIONS: dict[str, set[str]] = {
    "pending_approval": {"approved", "rejected"},
    "approved": {"queued", "rejected"},
    "queued": {"running", "rejected"},
    "running": {"completed", "failed"},
    "completed": {"validated", "rejected"},
    "failed": {"archived"},
    "validated": {"archived"},
    "rejected": {"archived"},
}


def set_state(job_id: str, to_state: str, actor: str, reason: str | None = None) -> dict[str, Any]:
    """Transition a job, validating it against the allowed state machine. Audited."""
    if to_state not in STATES:
        return {"ok": False, "errors": [f"unknown state: {to_state}"]}
    with _connect() as conn:
        row = conn.execute("SELECT state FROM sim_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return {"ok": False, "errors": [f"job not found: {job_id}"]}
        cur = row["state"]
        if to_state not in _TRANSITIONS.get(cur, set()):
            return {"ok": False, "errors": [f"illegal transition: {cur} -> {to_state}"]}
        now = _now()
        conn.execute("UPDATE sim_jobs SET state=?, actor=?, reason=?, updated_at=? WHERE job_id=?",
                     (to_state, actor, reason, now, job_id))
        conn.execute("INSERT INTO sim_job_events (job_id, from_state, to_state, actor, reason, ts)"
                     " VALUES (?,?,?,?,?,?)", (job_id, cur, to_state, actor, reason, now))
        conn.commit()
    return {"ok": True, "job_id": job_id, "from": cur, "state": to_state}


def approve(job_id: str, actor: str = "operator", reason: str | None = None) -> dict[str, Any]:
    """Human gate: pending_approval -> approved. Still does NOT execute the job."""
    return set_state(job_id, "approved", actor, reason)


def reject(job_id: str, actor: str = "operator", reason: str | None = None) -> dict[str, Any]:
    return set_state(job_id, "rejected", actor, reason)


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


# Artifact metadata index (sim-job-governance-spec §3). The control plane owns the
# artifact INDEX (metadata only — never payloads); payloads + R2 archive belong to
# the future compute plane. register_artifact records one index entry.
ARTIFACT_KINDS = {"event_log", "metrics", "validation_report", "run_manifest",
                  "snapshot", "plot", "summary", "comparison"}
RETENTION_CLASSES = {"ephemeral", "standard", "long_term"}


def register_artifact(meta: Any) -> dict[str, Any]:
    """Record one artifact's METADATA in the index (no payload). Requires the
    referenced job to exist. Validates kind + retention_class."""
    if not isinstance(meta, dict):
        return {"ok": False, "errors": ["artifact metadata must be a JSON object"]}
    errors: list[str] = []
    for k in ("artifact_id", "job_id", "kind", "sha256"):
        if not meta.get(k):
            errors.append(f"missing required field: {k}")
    kind = meta.get("kind")
    if kind is not None and kind not in ARTIFACT_KINDS:
        errors.append(f"kind must be one of {sorted(ARTIFACT_KINDS)}")
    rc = meta.get("retention_class", "standard")
    if rc not in RETENTION_CLASSES:
        errors.append(f"retention_class must be one of {sorted(RETENTION_CLASSES)}")
    if errors:
        return {"ok": False, "errors": errors}
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM sim_jobs WHERE job_id=?", (meta["job_id"],)).fetchone():
            return {"ok": False, "errors": [f"job not found: {meta['job_id']}"]}
        if conn.execute("SELECT 1 FROM sim_artifacts WHERE artifact_id=?", (meta["artifact_id"],)).fetchone():
            return {"ok": False, "errors": [f"artifact_id already exists: {meta['artifact_id']}"]}
        conn.execute(
            "INSERT INTO sim_artifacts (artifact_id, job_id, kind, sha256, engine_version,"
            " bytes, retention_class, r2_path, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (meta["artifact_id"], meta["job_id"], kind, meta["sha256"],
             meta.get("engine_version"), meta.get("bytes"), rc, meta.get("r2_path"), _now()),
        )
        conn.commit()
    return {"ok": True, "artifact_id": meta["artifact_id"], "job_id": meta["job_id"], "kind": kind}


def list_artifacts(job_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List artifact metadata (no payloads), optionally for one job."""
    limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        if job_id:
            rows = conn.execute("SELECT artifact_id, job_id, kind, retention_class, bytes, created_at"
                                " FROM sim_artifacts WHERE job_id=? ORDER BY created_at DESC LIMIT ?",
                                (job_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT artifact_id, job_id, kind, retention_class, bytes, created_at"
                                " FROM sim_artifacts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "count": len(rows), "artifacts": [dict(r) for r in rows]}


def get_artifact(artifact_id: str) -> dict[str, Any]:
    """Bounded metadata for one artifact (never the payload)."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sim_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    if not row:
        return {"ok": False, "errors": [f"artifact not found: {artifact_id}"]}
    return {"ok": True, "artifact": dict(row)}


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
