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


def ensure_within(path: str | Path) -> Path:
    """
    Resolve path and verify it stays inside ALLOWED_ROOT.

    Returns the resolved absolute Path. Raises PermissionError otherwise.
    resolve() collapses .. and follows symlinks before the check, so neither
    can escape the root.
    """
    resolved = Path(path).resolve()
    if resolved != ALLOWED_ROOT and not resolved.is_relative_to(ALLOWED_ROOT):
        raise PermissionError(
            f"Path is outside the allowed workspace ({ALLOWED_ROOT}): {resolved}"
        )
    return resolved
