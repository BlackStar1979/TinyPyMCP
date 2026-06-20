"""
TinyPyMCP - Cloudflare API client (config-by-reference).

The CF API token (very powerful — DNS/tunnel/Access for the account) lives in a
JSON file OUTSIDE C:\\Work, referenced by path — never a tool argument, never
logged, never echoed. Responses are returned without ever including the token.

Config file (default ~/.romion/cloudflare.json, override MCP_CF_CONFIG):
{ "api_token": "...", "zone": "romionologic.dev" }

Phase 1 (now): read-only — verify token, list zones, resolve zone id, list DNS.
Writes (DNS/tunnel routes/Access) are added behind explicit, audited tools later.
NOTE: token currently lives on the operator PC; re-home to the VPS with proper
secret hardening after the VPS migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_CONFIG = Path.home() / ".romion" / "cloudflare.json"
CONFIG_PATH = Path(os.environ.get("MCP_CF_CONFIG", str(_DEFAULT_CONFIG)))
API = "https://api.cloudflare.com/client/v4"
DEFAULT_TIMEOUT = 20

# Production resources the agent must NOT delete/remove without an explicit
# force=True. Override/extend via MCP_CF_PROTECTED (comma/space-separated hosts).
_DEFAULT_PROTECTED = {
    "romionologic.dev", "status.romionologic.dev", "router.romionologic.dev",
    "deploy.romionologic.dev", "gpt-mcp.romionologic.dev", "mcp.romionologic.dev",
    "mcp-stc-safe.romionologic.dev", "mcp-tests-access.romionologic.dev",
    "mcp-tests-bearer.romionologic.dev", "tiny-py-mcp.romionologic.dev",
}


def _protected_hosts() -> set[str]:
    raw = os.environ.get("MCP_CF_PROTECTED", "").strip()
    if raw:
        return {h.strip().lower() for h in raw.replace(",", " ").split() if h.strip()}
    return set(_DEFAULT_PROTECTED)


def _blocked(name: str, kind: str) -> dict[str, Any]:
    return {"status": "blocked", "ok": False,
            "reason": f"protected {kind} '{name}' — pass force=true to override",
            "result": None}


class CFConfigError(Exception):
    pass


def _config(config_ref: str | None = None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else CONFIG_PATH
    if not path.is_file():
        raise CFConfigError(f"Cloudflare config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CFConfigError(f"cannot read Cloudflare config: {e}") from e
    if not cfg.get("api_token"):
        raise CFConfigError("Cloudflare config missing 'api_token'")
    return cfg


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg['api_token']}", "Content-Type": "application/json"}


def _request(method: str, path: str, config_ref: str | None = None,
             params: dict | None = None, body: Any | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as c:
        r = c.request(method, API + path, headers=_headers(cfg), params=params,
                      json=body if body is not None else None)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:1000]}
    # Surface CF's own success flag + errors; never include request headers.
    return {
        "status": r.status_code,
        "ok": bool(data.get("success")) if isinstance(data, dict) else r.is_success,
        "errors": data.get("errors") if isinstance(data, dict) else None,
        "result": data.get("result") if isinstance(data, dict) else data,
    }


def verify_token(config_ref: str | None = None) -> dict[str, Any]:
    """GET /user/tokens/verify — confirms the token is live and active."""
    return _request("GET", "/user/tokens/verify", config_ref)


def list_zones(config_ref: str | None = None, name: str | None = None) -> dict[str, Any]:
    cfg = _config(config_ref)
    name = name or cfg.get("zone")
    params = {"name": name} if name else None
    res = _request("GET", "/zones", config_ref, params=params)
    if isinstance(res.get("result"), list):
        res["result"] = [{"id": z.get("id"), "name": z.get("name"), "status": z.get("status")} for z in res["result"]]
    return res


def get_zone_id(config_ref: str | None = None, name: str | None = None) -> str | None:
    res = list_zones(config_ref, name)
    rows = res.get("result") or []
    return rows[0]["id"] if rows else None


def list_dns(config_ref: str | None = None, name: str | None = None) -> dict[str, Any]:
    zone_id = get_zone_id(config_ref, name)
    if not zone_id:
        return {"status": 404, "ok": False, "errors": "zone not found", "result": []}
    res = _request("GET", f"/zones/{zone_id}/dns_records", config_ref)
    if isinstance(res.get("result"), list):
        res["result"] = [{"id": d.get("id"), "type": d.get("type"), "name": d.get("name"), "content": d.get("content"), "proxied": d.get("proxied")} for d in res["result"]]
    return res


def get_account_id(config_ref: str | None = None) -> str | None:
    cfg = _config(config_ref)
    if cfg.get("account_id"):
        return cfg["account_id"]
    res = _request("GET", "/accounts", config_ref)
    rows = res.get("result") or []
    return rows[0]["id"] if rows else None


def list_tunnels(config_ref: str | None = None) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": []}
    res = _request("GET", f"/accounts/{acc}/cfd_tunnel", config_ref, params={"is_deleted": "false"})
    if isinstance(res.get("result"), list):
        res["result"] = [{"id": t.get("id"), "name": t.get("name"), "status": t.get("status")} for t in res["result"]]
    return res


def get_tunnel_config(tunnel_id: str, config_ref: str | None = None) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": None}
    return _request("GET", f"/accounts/{acc}/cfd_tunnel/{tunnel_id}/configurations", config_ref)


def create_service_token(name: str, config_ref: str | None = None) -> dict[str, Any]:
    """Create an Access service token. The client_secret is returned ONCE — the
    caller must store it immediately (it's never retrievable again)."""
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": None}
    return _request("POST", f"/accounts/{acc}/access/service_tokens", config_ref, body={"name": name})


def delete_service_token(token_id: str, config_ref: str | None = None) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    return _request("DELETE", f"/accounts/{acc}/access/service_tokens/{token_id}", config_ref)


def create_access_app(name: str, domain: str, config_ref: str | None = None) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": None}
    body = {"type": "self_hosted", "name": name, "domain": domain}
    res = _request("POST", f"/accounts/{acc}/access/apps", config_ref, body=body)
    if isinstance(res.get("result"), dict):
        r = res["result"]
        res["result"] = {"id": r.get("id"), "name": r.get("name"), "domain": r.get("domain")}
    return res


def delete_access_app(app_id: str, config_ref: str | None = None, force: bool = False) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    app = _request("GET", f"/accounts/{acc}/access/apps/{app_id}", config_ref)
    domain = (app.get("result") or {}).get("domain", "") if isinstance(app.get("result"), dict) else ""
    if str(domain).lower() in _protected_hosts() and not force:
        return _blocked(str(domain), "Access app")
    return _request("DELETE", f"/accounts/{acc}/access/apps/{app_id}", config_ref)


def add_access_service_policy(app_id: str, name: str, token_id: str, config_ref: str | None = None) -> dict[str, Any]:
    """Add a Service-Auth policy (decision=non_identity) allowing only the given
    service token — same shape Cloudflare's UI 'Service Auth' produces."""
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": None}
    body = {"name": name, "decision": "non_identity", "precedence": 1,
            "include": [{"service_token": {"token_id": token_id}}]}
    return _request("POST", f"/accounts/{acc}/access/apps/{app_id}/policies", config_ref, body=body)


def update_tunnel_config(tunnel_id: str, config: dict, config_ref: str | None = None) -> dict[str, Any]:
    acc = get_account_id(config_ref)
    if not acc:
        return {"status": 404, "ok": False, "errors": "account not found", "result": None}
    return _request("PUT", f"/accounts/{acc}/cfd_tunnel/{tunnel_id}/configurations", config_ref, body={"config": config})


def add_tunnel_route(tunnel_id: str, hostname: str, service: str, config_ref: str | None = None) -> dict[str, Any]:
    """GET current config, insert/replace one hostname rule BEFORE the catch-all,
    PUT the whole config back. Preserves all other ingress rules and config keys."""
    cur = get_tunnel_config(tunnel_id, config_ref)
    config = dict((cur.get("result") or {}).get("config") or {})
    ingress = [r for r in (config.get("ingress") or []) if r.get("hostname") != hostname]
    idx = next((i for i, r in enumerate(ingress) if not r.get("hostname")), len(ingress))
    ingress.insert(idx, {"hostname": hostname, "service": service})
    config["ingress"] = ingress
    return update_tunnel_config(tunnel_id, config, config_ref)


def remove_tunnel_route(tunnel_id: str, hostname: str, config_ref: str | None = None,
                        force: bool = False) -> dict[str, Any]:
    if hostname.lower() in _protected_hosts() and not force:
        return _blocked(hostname, "tunnel route")
    cur = get_tunnel_config(tunnel_id, config_ref)
    config = dict((cur.get("result") or {}).get("config") or {})
    config["ingress"] = [r for r in (config.get("ingress") or []) if r.get("hostname") != hostname]
    return update_tunnel_config(tunnel_id, config, config_ref)


def create_dns_record(rec_type: str, name: str, content: str, ttl: int = 60,
                      proxied: bool = False, config_ref: str | None = None,
                      zone_name: str | None = None) -> dict[str, Any]:
    zone_id = get_zone_id(config_ref, zone_name)
    if not zone_id:
        return {"status": 404, "ok": False, "errors": "zone not found", "result": None}
    body = {"type": rec_type, "name": name, "content": content, "ttl": ttl, "proxied": proxied}
    res = _request("POST", f"/zones/{zone_id}/dns_records", config_ref, body=body)
    if isinstance(res.get("result"), dict):
        r = res["result"]
        res["result"] = {"id": r.get("id"), "type": r.get("type"), "name": r.get("name"), "content": r.get("content")}
    return res


def delete_dns_record(record_id: str, config_ref: str | None = None,
                      zone_name: str | None = None, force: bool = False) -> dict[str, Any]:
    zone_id = get_zone_id(config_ref, zone_name)
    if not zone_id:
        return {"status": 404, "ok": False, "errors": "zone not found", "result": None}
    rec = _request("GET", f"/zones/{zone_id}/dns_records/{record_id}", config_ref)
    name = (rec.get("result") or {}).get("name", "") if isinstance(rec.get("result"), dict) else ""
    if name.lower() in _protected_hosts() and not force:
        return _blocked(name, "DNS record")
    return _request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}", config_ref)
