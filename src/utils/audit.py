"""
TinyPyMCP - append-only audit log (JSONL).

Best-effort: never raises into the caller. One JSON line per event with a UTC
timestamp. Used to record process runs (and later remote ops) so diagnosis is
fast. Does NOT log full stdout/stderr — only sizes/flags — to avoid leaking or
bloating. Path: MCP_AUDIT_LOG env, else <project>/logs/.tinypymcp-audit.jsonl.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT = Path(__file__).resolve().parents[2] / "logs" / ".tinypymcp-audit.jsonl"
AUDIT_LOG_PATH = Path(os.environ.get("MCP_AUDIT_LOG", str(_DEFAULT)))


def audit(event: str, fields: dict[str, Any] | None = None) -> None:
    """Append one audit event. Swallows all errors (logging must never break a tool)."""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        if fields:
            record.update(fields)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
