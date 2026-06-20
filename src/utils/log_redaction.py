"""
TinyPyMCP - redact `?token=...` from access logs.

The query-string token (used by hosted connectors like ChatGPT that can't send
an Authorization header) would otherwise be written verbatim to uvicorn's access
log. This logging.Filter scrubs it to `token=[REDACTED]`, matching the redaction
GPT_MCP already does in its perf log. Wire it onto uvicorn's "access" handler via
the log_config so it survives uvicorn's own logging setup.
"""

from __future__ import annotations

import logging
import re

_TOKEN_RX = re.compile(r'(token=)[^&\s"\']+', re.IGNORECASE)


class TokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_RX.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RX.sub(r"\1[REDACTED]", record.msg)
        return True


def build_redacting_log_config() -> dict:
    """uvicorn's default LOGGING_CONFIG with the token-redaction filter attached
    to the access handler."""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_token"] = {
        "()": "src.utils.log_redaction.TokenRedactionFilter"
    }
    access_handler = cfg.get("handlers", {}).get("access")
    if access_handler is not None:
        access_handler.setdefault("filters", []).append("redact_token")
    return cfg
