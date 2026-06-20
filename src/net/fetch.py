"""
TinyPyMCP - network helpers (Stage 2).

HTTP probe + npm/pypi package lookups. Supports porting (finding the Python
equivalent of a JS package) and testing endpoints. Network is unrestricted by
policy; bounded only by timeout and body-size caps.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 120
MAX_BODY_CHARS = 200_000
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"}


def http_probe(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BODY_CHARS,
) -> dict[str, Any]:
    """Make an HTTP request and return status, headers and a capped body."""
    method = method.upper()
    if method not in _HTTP_METHODS:
        raise ValueError(f"unsupported method: {method}")
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    max_bytes = max(0, min(int(max_bytes), MAX_BODY_CHARS))

    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.request(method, url, headers=headers or {}, content=body)
        text = r.text if method != "HEAD" else ""
        truncated = len(text) > max_bytes
        return {
            "url": str(r.url),
            "status": r.status_code,
            "ok": r.is_success,
            "content_type": r.headers.get("content-type", ""),
            "elapsed_ms": int(r.elapsed.total_seconds() * 1000),
            "headers": dict(r.headers),
            "body": text[:max_bytes],
            "body_truncated": truncated,
        }


def check_npm_package(name: str) -> dict[str, Any]:
    """Look up a package on the npm registry."""
    if not name or not name.strip():
        raise ValueError("package name required")
    url = f"https://registry.npmjs.org/{quote(name.strip(), safe='@/')}"
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
    if r.status_code == 404:
        return {"name": name, "found": False}
    r.raise_for_status()
    doc = r.json()
    latest = (doc.get("dist-tags") or {}).get("latest", "")
    latest_meta = (doc.get("versions") or {}).get(latest, {})
    return {
        "name": doc.get("name", name),
        "found": True,
        "latest": latest,
        "description": doc.get("description", ""),
        "homepage": doc.get("homepage", ""),
        "license": doc.get("license", "") or latest_meta.get("license", ""),
        "dependencies": list((latest_meta.get("dependencies") or {}).keys()),
        "version_count": len(doc.get("versions") or {}),
    }


def check_pypi_package(name: str) -> dict[str, Any]:
    """Look up a package on PyPI."""
    if not name or not name.strip():
        raise ValueError("package name required")
    url = f"https://pypi.org/pypi/{quote(name.strip(), safe='')}/json"
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url)
    if r.status_code == 404:
        return {"name": name, "found": False}
    r.raise_for_status()
    info = (r.json() or {}).get("info") or {}
    return {
        "name": info.get("name", name),
        "found": True,
        "latest": info.get("version", ""),
        "summary": info.get("summary", ""),
        "homepage": info.get("home_page", "") or (info.get("project_urls") or {}).get("Homepage", ""),
        "license": info.get("license", ""),
        "requires_python": info.get("requires_python", ""),
        "requires_dist": info.get("requires_dist") or [],
    }
