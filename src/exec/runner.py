"""
TinyPyMCP - allowlisted local process runner (Stage 1).

Runs only a fixed set of dev binaries (git/node/npm/python/...), never through
a shell (args passed as a list, no injection), with cwd confined to C:\\Work
via path_guard, a timeout and output-size caps. This is the "bounded by task"
exec layer: free to reach the network (git clone, npm install), but on the
machine it can only invoke allowlisted programs inside the workspace.

Workspace root: MCP_WORKSPACE_ROOT env, else C:\\Work\\TinyPyMCP\\workspaces.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from src.utils.path_guard import ALLOWED_ROOT, ensure_within
from src.utils.audit import audit

WORKSPACE_ROOT = Path(
    os.environ.get("MCP_WORKSPACE_ROOT", str(ALLOWED_ROOT / "TinyPyMCP" / "workspaces"))
)

# Programs the agent may invoke. Bounded to the porting/dev task.
_DEFAULT_ALLOWED = {
    "git", "node", "npm", "npx", "python", "python3",
    "pip", "pip3", "pytest", "ruff", "uv",
}


def _load_allowlist() -> set[str]:
    """Allowlist from MCP_EXEC_ALLOWLIST (comma/space-separated) if set, else the
    default. Lets the operator run a locked-down profile (e.g. drop interpreters:
    MCP_EXEC_ALLOWLIST="git" — no python/node)."""
    raw = os.environ.get("MCP_EXEC_ALLOWLIST", "").strip()
    if not raw:
        return set(_DEFAULT_ALLOWED)
    return {p.strip().lower() for p in raw.replace(",", " ").split() if p.strip()}


ALLOWED_PROGRAMS = _load_allowlist()

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 100_000
MAX_ARGS = 100
MAX_ARG_LEN = 4000

# Child processes get ONLY these env keys, never the full parent environment.
# This stops a child (e.g. `python -c "print(os.environ)"`) from reading
# MCP_AUTH_TOKEN or any other secret the server holds.
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "LANG", "LC_ALL", "TERM",
}


def _clean_env() -> dict[str, str]:
    """Build a minimal child environment from an allowlist of safe keys."""
    return {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}


def _resolve_program(program: str) -> list[str]:
    """Validate against the allowlist and resolve to an executable prefix.

    Returns the command prefix as a list. On Windows, .cmd/.bat shims
    (npm/npx) are wrapped with `cmd /c` so subprocess can launch them
    without shell=True.
    """
    if "/" in program or "\\" in program:
        raise PermissionError("program must be a bare executable name, not a path")
    base = Path(program).name.lower()
    base_noext = base.rsplit(".", 1)[0]
    if base_noext not in ALLOWED_PROGRAMS:
        raise PermissionError(
            f"Program not allowed: {program}. Allowed: {sorted(ALLOWED_PROGRAMS)}"
        )
    resolved = shutil.which(program) or shutil.which(base_noext)
    if not resolved:
        raise FileNotFoundError(f"Program not found on PATH: {program}")
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved]
    return [resolved]


def _truncate(s: str) -> tuple[str, bool]:
    if len(s) > MAX_OUTPUT_CHARS:
        return s[:MAX_OUTPUT_CHARS], True
    return s, False


def run_command(
    program: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one allowlisted program with the given args. No shell."""
    args = [str(a) for a in (args or [])]
    if len(args) > MAX_ARGS:
        raise ValueError(f"too many args (max {MAX_ARGS})")
    if any(len(a) > MAX_ARG_LEN for a in args):
        raise ValueError(f"single arg too long (max {MAX_ARG_LEN} chars)")
    cmd_prefix = _resolve_program(program)

    if cwd is None:
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        run_cwd = WORKSPACE_ROOT
    else:
        run_cwd = ensure_within(cwd)
        if not run_cwd.is_dir():
            raise NotADirectoryError(f"cwd is not a directory: {run_cwd}")

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    full = cmd_prefix + args

    audit("process_start", {"program": program, "args": args, "cwd": str(run_cwd), "timeout": timeout})
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            full, cwd=str(run_cwd), capture_output=True, text=True,
            timeout=timeout, shell=False, encoding="utf-8", errors="replace",
            env=_clean_env(),
        )
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out, rc = True, None
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")

    duration = round(time.monotonic() - start, 3)
    out, out_trunc = _truncate(out)
    err, err_trunc = _truncate(err)
    audit("process_finish", {
        "program": program, "cwd": str(run_cwd), "exit_code": rc,
        "timed_out": timed_out, "duration_s": duration,
        "stdout_bytes": len(out), "stderr_bytes": len(err),
    })
    return {
        "program": program,
        "args": args,
        "cwd": str(run_cwd),
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_s": duration,
        "stdout": out,
        "stdout_truncated": out_trunc,
        "stderr": err,
        "stderr_truncated": err_trunc,
    }


def clone_repo(
    repo_url: str,
    dest_name: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Clone a git repo into the workspace. Network is allowed; the
    destination is confined to the workspace and never overwrites."""
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValueError("repo_url is required")

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    name = dest_name or repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"invalid dest_name: {name}")

    dest = ensure_within(WORKSPACE_ROOT / name)
    if dest.exists():
        raise FileExistsError(f"destination already exists: {dest}")

    args = ["clone"]
    if depth and int(depth) > 0:
        args += ["--depth", str(int(depth))]
    args += [repo_url, str(dest)]

    result = run_command("git", args, cwd=str(WORKSPACE_ROOT), timeout=300)
    result["dest"] = str(dest)
    result["cloned"] = result["exit_code"] == 0 and dest.exists()
    return result
