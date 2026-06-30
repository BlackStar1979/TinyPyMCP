"""
TinyPyMCP - AI Endpoints usage snapshots + time windows.

OVH /cloud/project/{id}/usage/current is CURRENT-MONTH cumulative per model. To
answer "usage over the last 8h/24h/3d/7d/1m" we periodically snapshot those
cumulative counters and diff against the snapshot closest to (now - window).

Snapshots are recorded opportunistically when collect_estate actually fetches
OVH usage (throttled), so history accrues while the dashboard/estate is in use.
For guaranteed cadence regardless of viewers, a background poller is the next
step (noted in memory). DB: MCP_USAGE_DB env, else /data/usage.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(os.environ.get("MCP_USAGE_DB", "/data/usage.db"))
_MIN_INTERVAL_S = 600  # don't persist snapshots more often than this
WINDOWS = {"8h": 8 * 3600, "24h": 24 * 3600, "3d": 3 * 86400, "7d": 7 * 86400, "1m": 30 * 86400}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage_snapshots (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT NOT NULL,
            models TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage_snapshots(ts)")
    return conn


def record(models: dict[str, Any]) -> bool:
    """Persist a snapshot of per-model cumulative {input,output,price}, throttled.
    Returns True if a snapshot was written."""
    now = _now()
    with _connect() as conn:
        last = conn.execute("SELECT ts FROM ai_usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            try:
                if (now - datetime.fromisoformat(last["ts"])).total_seconds() < _MIN_INTERVAL_S:
                    return False
            except ValueError:
                pass
        conn.execute("INSERT INTO ai_usage_snapshots (ts, models) VALUES (?,?)",
                     (now.isoformat(), json.dumps(models)))
        conn.commit()
    return True


def _totals(models: dict[str, Any]) -> tuple[int, int]:
    return (sum(int(v.get("input", 0)) for v in models.values()),
            sum(int(v.get("output", 0)) for v in models.values()))


def windows(current: dict[str, Any]) -> dict[str, Any]:
    """For each window, diff the current cumulative totals against the EARLIEST
    snapshot at or after (now - window). Cumulative resets at month rollover ->
    negative deltas are reported as null (insufficient/!comparable history)."""
    cur_in, cur_out = _totals(current)
    now = _now()
    out: dict[str, Any] = {}
    with _connect() as conn:
        for name, secs in WINDOWS.items():
            cutoff = (now.timestamp() - secs)
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            row = conn.execute(
                "SELECT ts, models FROM ai_usage_snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1",
                (cutoff_iso,),
            ).fetchone()
            if not row:
                out[name] = {"input": None, "output": None, "since": None, "note": "no snapshot yet"}
                continue
            base = json.loads(row["models"])
            b_in, b_out = _totals(base)
            d_in, d_out = cur_in - b_in, cur_out - b_out
            out[name] = {
                "input": d_in if d_in >= 0 else None,
                "output": d_out if d_out >= 0 else None,
                "since": row["ts"],
            }
    return out
