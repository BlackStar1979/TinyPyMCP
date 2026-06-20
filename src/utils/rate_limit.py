"""
TinyPyMCP - per-IP rate limit (pure ASGI middleware).

Anti-runaway guard: caps requests per client IP in a 60s sliding window.
Disabled by default (MCP_RATE_LIMIT_PER_MIN=0) so it never breaks a connector's
normal burst until the operator opts in and tunes a generous value.
"""

from __future__ import annotations

import time
from collections import deque


class RateLimitMiddleware:
    def __init__(self, app, per_min: int):
        self.app = app
        self.per_min = per_min
        self._hits: dict[str, deque] = {}

    async def __call__(self, scope, receive, send):
        if self.per_min <= 0 or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        ip = (scope.get("client") or ("?", 0))[0]
        now = time.time()
        dq = self._hits.setdefault(ip, deque())
        cutoff = now - 60
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= self.per_min:
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"retry-after", b"60"), (b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"rate_limited"}'})
            return
        dq.append(now)
        await self.app(scope, receive, send)
