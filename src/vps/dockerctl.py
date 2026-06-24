"""
TinyPyMCP - host docker control plane (via the mounted docker.sock).

Runs the `docker` CLI on the VPS host through the read/write docker socket
bind-mounted into this container. This is the host-exec keystone: it ends the
"operator as the agent's hands" pattern for container/host ops (logs, inspect,
restart, compose, exec).

Gating: READ subcommands (ps/logs/inspect/images/stats/...) run ungated.
MUTATING subcommands (run/exec/rm/stop/restart/build/compose/prune/...) require
confirm=true and are audited. No shell — args are a list, never a command string.

docker.sock ~= root on the host; the boundary here is confirm+audit (for this
internet-exposed instance) and, ultimately, the consumer's egress isolation
(see memory [[egress-gated-secret-access]]). The socket mount + group_add are in
docker-compose.yml.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from src.utils.audit import audit

_DOCKER = "docker"
MAX_OUTPUT = 100_000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600

# Subcommands that change state -> require confirm=true + audit.
_MUTATING = {
    "run", "exec", "rm", "rmi", "stop", "start", "restart", "kill", "pause",
    "unpause", "create", "build", "buildx", "pull", "push", "prune", "commit",
    "cp", "update", "rename", "network", "volume", "compose", "login", "logout",
    "save", "load", "import", "tag", "swarm", "service", "stack", "node", "plugin",
}


def _subcommand(args: list[str]) -> str:
    for a in args:
        if not a.startswith("-"):
            return a.lower()
    return ""


def docker(args: list[str], confirm: bool = False, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run `docker <args>` on the host. Mutating subcommands need confirm=true."""
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        return {"ok": False, "error": "args must be a non-empty list of strings"}
    sub = _subcommand(args)
    mutating = sub in _MUTATING
    if mutating and not confirm:
        return {"dry_run": True, "would": [_DOCKER] + args, "subcommand": sub,
                "note": "mutating docker subcommand; set confirm=true to apply"}
    if shutil.which(_DOCKER) is None:
        return {"ok": False, "error": "docker CLI not available in container (image/socket not provisioned)"}
    if mutating:
        audit("vps_docker", {"args": args, "subcommand": sub})
    try:
        proc = subprocess.run([_DOCKER, *args], capture_output=True, text=True,
                              timeout=max(1, min(int(timeout), MAX_TIMEOUT)))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker timed out after {timeout}s", "args": args}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "subcommand": sub, "mutating": mutating,
            "stdout": proc.stdout[:MAX_OUTPUT], "stderr": proc.stderr[:MAX_OUTPUT]}
