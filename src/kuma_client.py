"""
TinyPyMCP - Uptime Kuma client (config-by-reference).

Manages/reads the operator's Uptime Kuma (status.romionologic.dev) via the
socket.io API (uptime-kuma-api). Kuma has NO scoped API key for management, so
the credential here is the FULL-ADMIN login — the bound is the exposed TOOL
SURFACE + profile tier, not the credential. Creds live in a JSON file OUTSIDE
C:\\Work / on the VPS at /secrets, referenced by path — never a tool argument,
never logged.

Config (default ~/.romion/kuma.json, override MCP_KUMA_CONFIG):
{ "url": "https://status.romionologic.dev", "username": "...", "password": "..." }
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".romion" / "kuma.json"
CONFIG_PATH = Path(os.environ.get("MCP_KUMA_CONFIG", str(_DEFAULT_CONFIG)))


class KumaConfigError(Exception):
    pass


def _config(config_ref: str | None = None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else CONFIG_PATH
    if not path.is_file():
        raise KumaConfigError(f"Kuma config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise KumaConfigError(f"cannot read Kuma config: {e}") from e
    for key in ("url", "username", "password"):
        if not cfg.get(key):
            raise KumaConfigError(f"Kuma config missing '{key}'")
    return cfg


@contextlib.contextmanager
def _api(config_ref: str | None = None):
    cfg = _config(config_ref)
    try:
        from uptime_kuma_api import UptimeKumaApi  # lazy; server starts without it
    except ImportError as e:
        raise KumaConfigError("uptime-kuma-api not installed (pip install uptime-kuma-api)") from e
    api = UptimeKumaApi(cfg["url"])
    try:
        api.login(cfg["username"], cfg["password"])
        yield api
    finally:
        with contextlib.suppress(Exception):
            api.disconnect()


def _slim(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "name", "url", "type", "active", "interval") if k in m}


# ---- read ----
def list_monitors(config_ref: str | None = None) -> dict[str, Any]:
    with _api(config_ref) as api:
        return {"ok": True, "monitors": [_slim(m) for m in api.get_monitors()]}


def monitor_status(config_ref: str | None = None) -> dict[str, Any]:
    with _api(config_ref) as api:
        names = {m["id"]: m.get("name") for m in api.get_monitors()}
        beats = api.get_heartbeats() or {}
        out = []
        for mid, name in names.items():
            last = (beats.get(mid) or [])
            last = last[-1] if last else {}
            out.append({"id": mid, "name": name, "status": last.get("status"),
                        "msg": last.get("msg"), "time": last.get("time")})
        return {"ok": True, "monitors": out}


# ---- manage (cloud_admin tier; tools guard with confirm) ----
def add_monitor(name: str, url: str, type: str = "http", interval: int = 60,
                accepted_statuscodes: list[str] | None = None, config_ref: str | None = None) -> dict[str, Any]:
    with _api(config_ref) as api:
        from uptime_kuma_api import MonitorType
        mt = MonitorType.HTTP if type == "http" else MonitorType(type)
        kwargs: dict[str, Any] = {"type": mt, "name": name, "url": url, "interval": int(interval)}
        if accepted_statuscodes:
            kwargs["accepted_statuscodes"] = list(accepted_statuscodes)
        return {"ok": True, "result": api.add_monitor(**kwargs)}


def pause_monitor(monitor_id: int, config_ref: str | None = None) -> dict[str, Any]:
    with _api(config_ref) as api:
        return {"ok": True, "result": api.pause_monitor(int(monitor_id))}


def resume_monitor(monitor_id: int, config_ref: str | None = None) -> dict[str, Any]:
    with _api(config_ref) as api:
        return {"ok": True, "result": api.resume_monitor(int(monitor_id))}
