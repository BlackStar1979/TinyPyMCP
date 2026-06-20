"""
TinyPyMCP - network helpers (Stage 2).

HTTP probe + npm/pypi package lookups. Supports porting (finding the Python
equivalent of a JS package) and testing endpoints. Network is unrestricted by
policy; bounded only by timeout and body-size caps.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 120
MAX_BODY_CHARS = 200_000
MAX_REDIRECTS = 5
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"}


def _ip_is_public(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    # Block loopback, RFC1918/ULA private, link-local (incl. 169.254.169.254
    # cloud metadata), reserved, multicast, and unspecified ranges.
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _guard_public_url(url: str) -> None:
    """SSRF guard: only http/https to a host whose every resolved IP is public.

    Re-run for each redirect hop. resolve-then-connect leaves a small DNS-rebind
    TOCTOU window, but this blocks the obvious SSRF (localhost/LAN/metadata).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http/https URLs are allowed: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"host did not resolve: {host} ({e})") from e
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise ValueError(f"host did not resolve: {host}")
    for addr in addrs:
        if not _ip_is_public(addr):
            raise PermissionError(f"blocked non-public address for host {host}: {addr}")


def http_probe(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BODY_CHARS,
) -> dict[str, Any]:
    """Make an HTTP request and return status, headers and a capped body.

    SSRF-guarded: scheme is http/https only and the target (and every redirect
    hop) must resolve to public IPs - localhost/LAN/cloud-metadata are blocked.
    """
    method = method.upper()
    if method not in _HTTP_METHODS:
        raise ValueError(f"unsupported method: {method}")
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    max_bytes = max(0, min(int(max_bytes), MAX_BODY_CHARS))

    current = url
    cur_method = method
    cur_body = body
    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        for _ in range(MAX_REDIRECTS + 1):
            _guard_public_url(current)
            r = c.request(cur_method, current, headers=headers or {}, content=cur_body)
            if r.is_redirect and r.headers.get("location"):
                current = str(httpx.URL(current).join(r.headers["location"]))
                # 303 (and the common 301/302 browser behaviour) downgrade to GET.
                if r.status_code in (301, 302, 303):
                    cur_method = "GET"
                    cur_body = None
                continue
            text = r.text if cur_method != "HEAD" else ""
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
    raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")


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
