"""
TinyPyMCP - Path guard.

Confines every filesystem operation to a single allowed root (default
C:\\Work). This is an in-process guard: it gives clear "out of scope" errors
and blocks path tricks (.., symlinks) via resolve() + is_relative_to. It is
NOT an OS-level security boundary - a tool doing raw open() bypasses it, and
anyone editing this file removes it. For a hard wall, run the server as a
low-privilege user with NTFS ACLs.

Override the root with the MCP_ALLOWED_ROOT environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

ALLOWED_ROOT = Path(os.environ.get("MCP_ALLOWED_ROOT", r"C:\Work")).resolve()

# Server-internal datastores must never be reachable through the file tools,
# even though they live inside ALLOWED_ROOT. The OAuth DB holds live access /
# refresh tokens; blocking the whole `data/` dir (plus any env-relocated DB and
# its SQLite sidecars) stops an authenticated or prompt-injected agent from
# exfiltrating them via read_file. This is deliberately scoped to the server's
# own datastores - the agent stays free to work on other .db files in C:\Work.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DENIED_DIRS = {(_PROJECT_ROOT / "data").resolve()}


def _denied_files() -> set[Path]:
    out: set[Path] = set()
    for env_var, default_rel in (("MCP_OAUTH_DB", "data/oauth.db"),
                                 ("MCP_MEMORY_DB", "data/memory.db")):
        raw = os.environ.get(env_var) or str(_PROJECT_ROOT / default_rel)
        try:
            base = Path(raw).resolve()
        except OSError:
            continue
        out.add(base)
        for suffix in ("-wal", "-shm", "-journal"):
            out.add(base.with_name(base.name + suffix))
    return out


def _is_protected_datastore(resolved: Path) -> bool:
    if resolved in _denied_files():
        return True
    for denied_dir in _DENIED_DIRS:
        if resolved == denied_dir or resolved.is_relative_to(denied_dir):
            return True
    return False


def ensure_within(path: str | Path) -> Path:
    """
    Resolve path and verify it stays inside ALLOWED_ROOT and is not a
    server-internal datastore.

    Returns the resolved absolute Path. Raises PermissionError otherwise.
    resolve() collapses .. and follows symlinks before the check, so neither
    can escape the root.
    """
    resolved = Path(path).resolve()
    if resolved != ALLOWED_ROOT and not resolved.is_relative_to(ALLOWED_ROOT):
        raise PermissionError(
            f"Path is outside the allowed workspace ({ALLOWED_ROOT}): {resolved}"
        )
    if _is_protected_datastore(resolved):
        raise PermissionError(
            f"Path is a protected server datastore and is not accessible via file tools: {resolved}"
        )
    return resolved
