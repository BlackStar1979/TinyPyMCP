"""
TinyPyMCP - whole-VPS read-only filesystem plane.

Reads ANY file/dir on the VPS through a read-only host bind-mount (default
/hostfs == the host root). Deliberately NOT path_guard-confined: the point is to
remove the over-restriction that forced the operator to act as the agent's hands.
Read-only by design — the security boundary is write/exec, not reading.

Secret handling is a PER-INSTANCE POLICY, not a hardcoded blind spot
(`MCP_FS_SECRET_MODE`, default "redact"):
  - redact: paths + metadata of secret files are visible, but their BYTES are
    withheld from results (so they never enter an internet-exposed agent's chat
    transcript). This is the right mode for THIS instance.
  - allow: full contents. Intended for a future air-gapped on-infra agent with NO
    internet egress, which can safely read/manage secrets (see memory
    [[egress-gated-secret-access]]).

Config:
  MCP_HOSTFS_ROOT     mount root for the host FS (default /hostfs)
  MCP_FS_SECRET_MODE  redact | allow (default redact)
  MCP_FS_SECRET_GLOBS extra comma-separated fnmatch patterns (matched against the
                      host-absolute path, e.g. /etc/shadow, and the basename)
"""

from __future__ import annotations

import fnmatch
import os
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("MCP_HOSTFS_ROOT", "/hostfs"))
SECRET_MODE = os.environ.get("MCP_FS_SECRET_MODE", "redact").strip().lower()
DEFAULT_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 1024 * 1024

# host-absolute paths/basenames whose CONTENTS are secret. Listing + metadata of
# these stay visible; only the bytes are withheld when SECRET_MODE == "redact".
_SECRET_GLOBS = [
    "*/secrets/*", "*/.ssh/*", "*/ssl/private/*", "*/cloudflared/*.json",
    "*.pem", "*.key", "*.ppk", "*.kdbx", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "*.env", ".env", "*credentials*", "*secret*", "*.secret",
    "/etc/shadow", "/etc/gshadow", "/etc/shadow-", "/etc/gshadow-",
]
_extra = os.environ.get("MCP_FS_SECRET_GLOBS", "")
if _extra.strip():
    _SECRET_GLOBS.extend(p.strip() for p in _extra.split(",") if p.strip())


class HostFSError(Exception):
    pass


def _resolve(path: str) -> tuple[Path, str]:
    """Map a user path to a real path under ROOT and the host-absolute view.

    Accepts host-absolute ("/etc/hosts"), ROOT-prefixed ("/hostfs/etc/hosts"),
    or relative paths. Returns (real_path, host_abs_str).
    """
    p = str(path).replace("\\", "/").strip()
    root_str = str(ROOT)
    if p == root_str or p.startswith(root_str + "/"):
        real = Path(p)
    else:
        real = ROOT / p.lstrip("/")
    try:
        rel = real.relative_to(ROOT)
        host_abs = "/" + str(rel).replace("\\", "/")
    except ValueError:
        host_abs = p if p.startswith("/") else "/" + p
    return real, ("/" if host_abs == "/." else host_abs)


def _is_secret(host_abs: str) -> bool:
    base = host_abs.rsplit("/", 1)[-1]
    for g in _SECRET_GLOBS:
        if fnmatch.fnmatch(host_abs, g) or fnmatch.fnmatch(base, g):
            return True
    return False


def _mode_str(st: os.stat_result) -> str:
    return stat_mod.filemode(st.st_mode)


def _meta(real: Path, host_abs: str) -> dict[str, Any]:
    st = real.lstat()
    is_link = stat_mod.S_ISLNK(st.st_mode)
    kind = "dir" if real.is_dir() else ("link" if is_link else "file")
    out: dict[str, Any] = {
        "path": host_abs,
        "type": kind,
        "size": st.st_size,
        "mode": _mode_str(st),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "secret": _is_secret(host_abs),
    }
    if is_link:
        try:
            out["target"] = os.readlink(real)
        except OSError:
            pass
    return out


def fs_list(path: str = "/", limit: int = 200) -> dict[str, Any]:
    """List a directory anywhere on the VPS. Metadata only (always safe)."""
    real, host_abs = _resolve(path)
    if not real.exists():
        return {"ok": False, "error": f"not found: {host_abs}"}
    if not real.is_dir():
        return {"ok": False, "error": f"not a directory: {host_abs}"}
    entries: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(real))
    except PermissionError:
        return {"ok": False, "error": f"permission denied: {host_abs}"}
    truncated = len(names) > limit
    for name in names[:limit]:
        child_abs = (host_abs.rstrip("/") + "/" + name) if host_abs != "/" else "/" + name
        try:
            entries.append(_meta(real / name, child_abs))
        except OSError as e:
            entries.append({"path": child_abs, "error": str(e)})
    return {"ok": True, "path": host_abs, "count": len(entries),
            "truncated": truncated, "entries": entries}


def fs_stat(path: str) -> dict[str, Any]:
    """Stat any path on the VPS (type, size, mode, owner, mtime)."""
    real, host_abs = _resolve(path)
    try:
        if not real.exists() and not real.is_symlink():
            return {"ok": False, "error": f"not found: {host_abs}"}
        return {"ok": True, **_meta(real, host_abs)}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "path": host_abs}


def fs_read(path: str, max_bytes: int = DEFAULT_READ_BYTES,
            offset: int = 0) -> dict[str, Any]:
    """Read a file anywhere on the VPS. Secret-file bytes are withheld when
    MCP_FS_SECRET_MODE=redact (metadata still returned)."""
    real, host_abs = _resolve(path)
    is_secret = _is_secret(host_abs)
    try:
        if not real.is_file():
            return {"ok": False, "error": f"not a file: {host_abs}"}
        meta = _meta(real, host_abs)
    except OSError as e:
        # e.g. the container user can't stat a 0600/0640 file -> clean error,
        # never a stack trace (and never leaks content).
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "path": host_abs, "secret": is_secret}
    if is_secret and SECRET_MODE != "allow":
        return {"ok": True, "redacted": True, "secret_mode": SECRET_MODE,
                "reason": "secret file; bytes withheld (set MCP_FS_SECRET_MODE=allow "
                          "on an air-gapped instance to read)",
                **meta}
    cap = max(1, min(int(max_bytes), MAX_READ_BYTES))
    try:
        with open(real, "rb") as f:
            if offset:
                f.seek(int(offset))
            raw = f.read(cap)
    except PermissionError:
        return {"ok": False, "error": f"permission denied: {host_abs}"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    binary = b"\x00" in raw
    result: dict[str, Any] = {
        "ok": True, "path": host_abs, "size": meta["size"],
        "mode": meta["mode"], "secret": is_secret,
        "bytes_returned": len(raw), "offset": int(offset),
        "truncated": (int(offset) + len(raw)) < meta["size"],
    }
    if binary:
        result["binary"] = True
        result["note"] = "binary file; content not decoded"
    else:
        result["content"] = raw.decode("utf-8", errors="replace")
    return result
