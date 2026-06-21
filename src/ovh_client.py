"""
TinyPyMCP - OVHcloud API client (config-by-reference, scoped consumer key).

Drives the VPS HOST layer via the official python-ovh SDK. Credentials
(application key/secret + consumer key) live in a JSON file OUTSIDE C:\\Work,
referenced by path — never a tool argument, never logged, never echoed.

The HARD security bound is the consumer key's ACCESS RULES (a per-route+method
allowlist set at OVH). Even if a function here asked for more, OVH rejects it.
Host-DESTRUCTIVE ops (reinstall / rebuild / stop) are deliberately NOT exposed
here — they stay operator-only / out of the agent's consumer key.

Config (default ~/.romion/ovh.json, override MCP_OVH_CONFIG):
{
  "endpoint": "ovh-eu",
  "application_key": "...",
  "application_secret": "...",
  "consumer_key": "...",
  "service_name": "vps-2f267042.vps.ovh.net"
}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".romion" / "ovh.json"
CONFIG_PATH = Path(os.environ.get("MCP_OVH_CONFIG", str(_DEFAULT_CONFIG)))


class OVHConfigError(Exception):
    pass


def _config(config_ref: str | None = None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else CONFIG_PATH
    if not path.is_file():
        raise OVHConfigError(f"OVH config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OVHConfigError(f"cannot read OVH config: {e}") from e
    for key in ("application_key", "application_secret", "consumer_key"):
        if not cfg.get(key):
            raise OVHConfigError(f"OVH config missing '{key}'")
    return cfg


def _svc(cfg: dict[str, Any], service_name: str | None) -> str:
    sn = service_name or cfg.get("service_name")
    if not sn:
        raise OVHConfigError("no VPS service_name (config 'service_name' or argument)")
    return str(sn)


def _client(cfg: dict[str, Any]):
    try:
        import ovh  # lazy: server starts without the SDK; only needed on a live call
    except ImportError as e:
        raise OVHConfigError("python-ovh SDK not installed (pip install ovh)") from e
    return ovh.Client(
        endpoint=cfg.get("endpoint", "ovh-eu"),
        application_key=cfg["application_key"],
        application_secret=cfg["application_secret"],
        consumer_key=cfg["consumer_key"],
    )


def _request(cfg: dict[str, Any], method: str, path: str, **body) -> dict[str, Any]:
    client = _client(cfg)
    fn = getattr(client, method.lower())
    try:
        result = fn(path, **body) if body else fn(path)
        return {"ok": True, "result": result}
    except Exception as e:  # ovh.exceptions.APIError etc. — surface message, never creds
        return {"ok": False, "error": type(e).__name__, "message": str(e)[:400]}


# ---- read-only ----
def vps_info(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "GET", f"/vps/{_svc(cfg, service_name)}")


def snapshot_status(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "GET", f"/vps/{_svc(cfg, service_name)}/snapshot")


def automated_backup_status(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "GET", f"/vps/{_svc(cfg, service_name)}/automatedBackup")


def images_available(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "GET", f"/vps/{_svc(cfg, service_name)}/images/available")


# ---- mutating (cloud_admin tier; tools guard with confirm) ----
def create_snapshot(description: str | None = None, service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    body = {"description": description} if description else {}
    return _request(cfg, "POST", f"/vps/{_svc(cfg, service_name)}/createSnapshot", **body)


def revert_snapshot(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "POST", f"/vps/{_svc(cfg, service_name)}/snapshot/revert")


def abort_snapshot(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "POST", f"/vps/{_svc(cfg, service_name)}/abortSnapshot")


def automated_backup_restore(restore_point: str, service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "POST", f"/vps/{_svc(cfg, service_name)}/automatedBackup/restore", restorePoint=restore_point)


def reboot(service_name: str | None = None, config_ref: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    return _request(cfg, "POST", f"/vps/{_svc(cfg, service_name)}/reboot")
