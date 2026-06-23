"""
TinyPyMCP - OVH AI Endpoints client (config-by-reference).

Talks to OVHcloud AI Endpoints (OpenAI-compatible: /chat/completions,
/embeddings) with a single Bearer access key. The key lives in a JSON file
OUTSIDE C:\\Work / on the VPS at /secrets, referenced by path — never a tool
argument, never logged, never surfaced in results. httpx is imported lazily so
the server starts without it.

This is the VPS-side ("clean IP") direct path: AI Endpoints rejects the
operator's PC + Arena server IPs, but the VPS is clean, so TinyPyMCP can call
the key directly. (The Persistent LLM Engine on the PC escalates via the
romion-llm-router proxy instead.) Separate key from the MVLTT `llm-agent-router`.

Config (default ~/.romion/ovh-ai.json, override MCP_OVH_AI_CONFIG):
{ "api_key": "...", "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1" }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = Path.home() / ".romion" / "ovh-ai.json"
CONFIG_PATH = Path(os.environ.get("MCP_OVH_AI_CONFIG", str(_DEFAULT_CONFIG)))
_FALLBACK_BASE = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"


class OVHAIConfigError(Exception):
    pass


def _config(config_ref: str | None = None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else CONFIG_PATH
    if not path.is_file():
        raise OVHAIConfigError(f"OVH AI config not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OVHAIConfigError(f"cannot read OVH AI config: {e}") from e
    if not cfg.get("api_key"):
        raise OVHAIConfigError("OVH AI config missing 'api_key'")
    return cfg


def _post(path: str, cfg: dict, json_body: dict, timeout: float = 60.0) -> Any:
    try:
        import httpx  # lazy
    except ImportError as e:
        raise OVHAIConfigError("httpx not installed") from e
    base = (cfg.get("base_url") or _FALLBACK_BASE).rstrip("/")
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "TinyPyMCP",
    }
    with httpx.Client(timeout=timeout) as c:
        resp = c.post(base + path, headers=headers, json=json_body)
    if resp.status_code >= 400:  # surface status + upstream message, never the key
        try:
            body = resp.json()
            msg = body.get("error") or body.get("message") or body
        except Exception:
            msg = resp.text[:300]
        return {"ok": False, "status": resp.status_code, "error": msg}
    return resp.json()


# ---- embeddings (read_only tier) ----
def embeddings(text, model: str = "bge-m3", config_ref: str | None = None) -> dict[str, Any]:
    """Return embedding vectors for a string or list of strings (bge-m3 = 1024-dim)."""
    cfg = _config(config_ref)
    data = _post("/embeddings", cfg, {"model": model, "input": text})
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    rows = data.get("data") or []
    vectors = [r.get("embedding") for r in rows]
    return {
        "ok": True,
        "model": data.get("model", model),
        "count": len(vectors),
        "dim": len(vectors[0]) if vectors and vectors[0] else 0,
        "embeddings": vectors,
        "usage": data.get("usage"),
    }


# ---- chat (read_only tier; non-mutating external call) ----
def chat(messages, model: str = "gpt-oss-20b", max_tokens: int = 512,
         temperature: float | None = None, config_ref: str | None = None) -> dict[str, Any]:
    """Single chat completion. Returns the assistant content + usage (no reasoning leak)."""
    cfg = _config(config_ref)
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": int(max_tokens)}
    if temperature is not None:
        body["temperature"] = float(temperature)
    data = _post("/chat/completions", cfg, body)
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    choices = data.get("choices") or [{}]
    msg = (choices[0] or {}).get("message") or {}
    return {
        "ok": True,
        "model": data.get("model", model),
        "content": msg.get("content"),
        "finish_reason": (choices[0] or {}).get("finish_reason"),
        "usage": data.get("usage"),
    }
