"""
TinyPyMCP - self-contained OAuth 2.1 authorization-server provider.

Implements the mcp SDK's OAuthAuthorizationServerProvider on top of the SQLite
store. The SDK's routes handle HTTP, DCR, metadata discovery and PKCE
verification; this provider supplies client/code/token persistence and issuance.

Human gate: `authorize` does NOT mint a code — it parks a pending authorization
and redirects the browser to an operator-login page. Only after the operator
authenticates (complete_authorization) is a code minted and the browser sent to
the client's redirect_uri. So the flow is never open to anonymous callers.

Config: issuer_url (public base) + operator_secret (login gate).
"""

from __future__ import annotations

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import store

ACCESS_TTL = 3600
CODE_TTL = 600
REFRESH_TTL = 30 * 86400


class RomionOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self, issuer_url: str, operator_secret: str):
        self.issuer = issuer_url.rstrip("/")
        self._secret = operator_secret

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        d = store.get_client(client_id)
        return OAuthClientInformationFull(**d) if d else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        store.put_client(client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pid = secrets.token_urlsafe(24)
        store.put_pending(pid, {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "state": params.state,
            "resource": params.resource,
        }, ttl=CODE_TTL)
        return f"{self.issuer}/oauth/operator-login?pid={pid}"

    def complete_authorization(self, pid: str, operator_secret: str) -> str | None:
        """Called by the login route AFTER the operator authenticates. Mints the
        auth code and returns the client redirect URL. Raises on bad secret;
        returns None if the pending request is missing/expired."""
        if not secrets.compare_digest(operator_secret, self._secret):
            raise PermissionError("invalid operator secret")
        pend = store.pop_pending(pid)
        if not pend:
            return None
        code = secrets.token_urlsafe(32)
        store.put_code(code, pend["client_id"], {
            "code": code,
            "client_id": pend["client_id"],
            "scopes": pend["scopes"],
            "code_challenge": pend["code_challenge"],
            "redirect_uri": pend["redirect_uri"],
            "redirect_uri_provided_explicitly": pend["redirect_uri_provided_explicitly"],
            "resource": pend.get("resource"),
            "expires_at": int(time.time() + CODE_TTL),
        }, ttl=CODE_TTL)
        from urllib.parse import urlencode
        redirect = pend["redirect_uri"]
        params = {"code": code}
        if pend.get("state"):
            params["state"] = pend["state"]
        sep = "&" if "?" in redirect else "?"
        return f"{redirect}{sep}{urlencode(params)}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        d = store.get_code(authorization_code)
        if not d or d["client_id"] != client.client_id:
            return None
        return AuthorizationCode(**d)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        store.delete_code(authorization_code.code)
        return self._issue(client.client_id, list(authorization_code.scopes or []), authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        d = store.get_refresh(refresh_token)
        if not d or d["client_id"] != client.client_id:
            return None
        return RefreshToken(**d)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        store.delete_token(refresh_token.token)
        return self._issue(client.client_id, list(scopes or refresh_token.scopes or []), None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        d = store.get_access(token)
        return AccessToken(**d) if d else None

    async def revoke_token(self, token) -> None:
        store.delete_token(token.token)

    def _issue(self, client_id: str, scopes: list[str], resource) -> OAuthToken:
        at = secrets.token_urlsafe(32)
        rt = secrets.token_urlsafe(32)
        store.put_access(at, client_id, {
            "token": at, "client_id": client_id, "scopes": scopes,
            "expires_at": int(time.time() + ACCESS_TTL), "resource": resource,
            "subject": "operator", "claims": {},
        }, ttl=ACCESS_TTL)
        store.put_refresh(rt, client_id, {
            "token": rt, "client_id": client_id, "scopes": scopes,
            "expires_at": int(time.time() + REFRESH_TTL), "subject": "operator",
        }, ttl=REFRESH_TTL)
        return OAuthToken(
            access_token=at, token_type="bearer", expires_in=ACCESS_TTL,
            scope=" ".join(scopes) if scopes else None, refresh_token=rt,
        )
