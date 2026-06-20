"""
TinyPyMCP - Bearer token auth (pure ASGI middleware).

Protects the HTTP endpoint. Pure ASGI (not BaseHTTPMiddleware) so it never
buffers the streamable-http SSE responses. Token comes from MCP_AUTH_TOKEN.

This is the app-layer gate. A Cloudflare Access layer can be added on top of
the tunnel independently; this guarantees the server itself refuses
unauthenticated calls regardless of how the request arrives.
"""

from __future__ import annotations

import hmac
from urllib.parse import parse_qs


class BearerAuthMiddleware:
    """Accepts the token two ways:

    1. `Authorization: Bearer <token>` header — for programmatic / agent-SDK
       clients (preferred).
    2. `?token=<token>` query string — for hosted connectors (ChatGPT) that
       can't send custom headers and only support "no auth + token in URL".
       Tradeoff: a token in the URL may be logged by proxies (Cloudflare,
       uvicorn access log). Accepted because it's the only ChatGPT-compatible
       path short of full OAuth. Rotate the token if a log is exposed.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._token = token
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode("latin-1")
        ok = hmac.compare_digest(provided, self._expected)

        if not ok:
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            qtok = (qs.get("token") or [""])[0]
            ok = hmac.compare_digest(qtok, self._token)

        if not ok:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="TinyPyMCP"'),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized"}',
            })
            return

        await self.app(scope, receive, send)
