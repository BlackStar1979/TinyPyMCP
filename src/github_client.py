"""
TinyPyMCP - GitHub client (config-by-reference).

Talks to the GitHub REST API with a classic (or fine-grained) token. The token
lives in a JSON file OUTSIDE C:\\Work / on the VPS at /secrets, referenced by
path — never a tool argument, never logged, never surfaced in results. httpx is
imported lazily so the server starts without it.

Config (default ~/.romion/github.json, override MCP_GITHUB_CONFIG):
{ "token": "ghp_...", "owner": "BlackStar1979", "default_repo": "TinyPyMCP" }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".romion" / "github.json"
CONFIG_PATH = Path(os.environ.get("MCP_GITHUB_CONFIG", str(_DEFAULT_CONFIG)))
_API = "https://api.github.com"


class GitHubConfigError(Exception):
    pass


def _config(config_ref: str | None = None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else CONFIG_PATH
    if not path.is_file():
        raise GitHubConfigError(f"GitHub config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise GitHubConfigError(f"cannot read GitHub config: {e}") from e
    if not cfg.get("token"):
        raise GitHubConfigError("GitHub config missing 'token'")
    return cfg


def _resolve(owner: str | None, repo: str | None, cfg: dict) -> tuple[str, str]:
    o = owner or cfg.get("owner")
    r = repo or cfg.get("default_repo")
    if not o or not r:
        raise GitHubConfigError("owner/repo not given and not in config (owner/default_repo)")
    return o, r


def _request(method: str, path: str, cfg: dict, json_body: dict | None = None,
             params: dict | None = None) -> Any:
    try:
        import httpx  # lazy
    except ImportError as e:
        raise GitHubConfigError("httpx not installed") from e
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TinyPyMCP",
    }
    with httpx.Client(timeout=30) as c:
        resp = c.request(method, _API + path, headers=headers, json=json_body, params=params)
    if resp.status_code >= 400:  # surface status + GitHub message, never the token
        try:
            msg = resp.json().get("message")
        except Exception:
            msg = resp.text[:300]
        return {"ok": False, "status": resp.status_code, "error": msg}
    return resp.json()


def _pr_slim(pr: dict) -> dict:
    return {"number": pr.get("number"), "title": pr.get("title"), "state": pr.get("state"),
            "head": (pr.get("head") or {}).get("ref"), "base": (pr.get("base") or {}).get("ref"),
            "url": pr.get("html_url"), "merged": pr.get("merged"), "draft": pr.get("draft")}


# ---- read (read_only tier) ----
def repo_info(owner=None, repo=None, config_ref=None) -> dict[str, Any]:
    cfg = _config(config_ref)
    o, r = _resolve(owner, repo, cfg)
    data = _request("GET", f"/repos/{o}/{r}", cfg)
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "repo": {k: data.get(k) for k in
            ("full_name", "default_branch", "private", "html_url", "description")}}


def list_prs(owner=None, repo=None, state="open", config_ref=None) -> dict[str, Any]:
    cfg = _config(config_ref)
    o, r = _resolve(owner, repo, cfg)
    data = _request("GET", f"/repos/{o}/{r}/pulls", cfg, params={"state": state, "per_page": 50})
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "pulls": [_pr_slim(p) for p in data]}


def get_pr(number, owner=None, repo=None, config_ref=None) -> dict[str, Any]:
    cfg = _config(config_ref)
    o, r = _resolve(owner, repo, cfg)
    data = _request("GET", f"/repos/{o}/{r}/pulls/{int(number)}", cfg)
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "pull": _pr_slim(data)}


# ---- mutate (cloud_admin tier; tools guard with confirm) ----
def create_pr(title, head, base="main", body="", owner=None, repo=None, config_ref=None) -> dict[str, Any]:
    cfg = _config(config_ref)
    o, r = _resolve(owner, repo, cfg)
    data = _request("POST", f"/repos/{o}/{r}/pulls", cfg,
                    json_body={"title": title, "head": head, "base": base, "body": body})
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "pull": _pr_slim(data)}


def merge_pr(number, method="squash", owner=None, repo=None, config_ref=None) -> dict[str, Any]:
    cfg = _config(config_ref)
    o, r = _resolve(owner, repo, cfg)
    data = _request("PUT", f"/repos/{o}/{r}/pulls/{int(number)}/merge", cfg,
                    json_body={"merge_method": method})
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    return {"ok": True, "result": {"merged": data.get("merged"), "message": data.get("message")}}
