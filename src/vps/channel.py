"""
TinyPyMCP - bounded VPS channel client (config-by-reference).

Calls a bounded HTTP channel on the VPS (the existing romion-llm-router, or the
new romion-deploy channel) that sits behind Cloudflare Access Service Auth.

Secrets (CF Access service token, optional bearer) live in a JSON file on disk,
referenced by path — NEVER passed as tool arguments, never logged, never echoed
back. The agent only chooses a method/path; credentials are attached here from
the config file.

Config file shape (keep it private, e.g. chmod 600 / not committed):
{
  "base_url": "https://router.romionologic.dev",
  "cf_access_client_id": "....access",
  "cf_access_client_secret": "....",
  "bearer_token": ""
}

Default config path: MCP_VPS_CONFIG env, else ~/.romion/vps-channel.json.
IMPORTANT: keep this file OUTSIDE C:\\Work. The agent's file tools (read_file,
search_codebase) are sandboxed to C:\\Work by path_guard, so a config there
would be readable by the agent and leak the token. ~/.romion is out of reach.

The request path is always appended to the configured base_url, so the agent
can only reach the one configured channel host — not arbitrary URLs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

# Outside C:\Work on purpose, so the agent's sandboxed file tools cannot read it.
_DEFAULT_CONFIG = Path.home() / ".romion" / "vps-channel.json"
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_BODY_CHARS = 200_000
_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


class ChannelConfigError(Exception):
    pass


def _config_path(config_ref: str | None) -> Path:
    if config_ref:
        return Path(config_ref)
    return Path(os.environ.get("MCP_VPS_CONFIG", str(_DEFAULT_CONFIG)))


def load_channel_config(config_ref: str | None = None) -> dict[str, Any]:
    path = _config_path(config_ref)
    if not path.is_file():
        raise ChannelConfigError(f"channel config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ChannelConfigError(f"cannot read channel config: {e}") from e
    if not cfg.get("base_url"):
        raise ChannelConfigError("channel config missing 'base_url'")
    return cfg


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    h: dict[str, str] = {}
    if cfg.get("cf_access_client_id") and cfg.get("cf_access_client_secret"):
        h["CF-Access-Client-Id"] = str(cfg["cf_access_client_id"])
        h["CF-Access-Client-Secret"] = str(cfg["cf_access_client_secret"])
    if cfg.get("bearer_token"):
        h["Authorization"] = f"Bearer {cfg['bearer_token']}"
    return h


def call(
    method: str,
    path: str,
    body: Any | None = None,
    config_ref: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Make an authenticated request to the configured channel. Returns response
    status/body only — never the request headers (which carry secrets)."""
    method = method.upper()
    if method not in _METHODS:
        raise ValueError(f"unsupported method: {method}")
    cfg = load_channel_config(config_ref)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))

    base = str(cfg["base_url"]).rstrip("/")
    url = f"{base}/{path.lstrip('/')}"

    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        r = c.request(method, url, headers=_headers(cfg), json=body if body is not None else None)
        text = r.text
        truncated = len(text) > MAX_BODY_CHARS
        return {
            "url": url,
            "status": r.status_code,
            "ok": r.is_success,
            "content_type": r.headers.get("content-type", ""),
            "body": text[:MAX_BODY_CHARS],
            "body_truncated": truncated,
        }
