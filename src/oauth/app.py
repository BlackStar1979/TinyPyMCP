"""
TinyPyMCP - wire the OAuth provider into a FastMCP server.

build_oauth_mcp() returns a FastMCP configured as a self-contained OAuth 2.1
authorization server: FastMCP mounts the metadata / DCR / authorize / token /
revoke routes (via the provider + AuthSettings) and protects the MCP endpoint.
We add one custom route, /oauth/operator-login, which is the human gate: the
provider's `authorize` redirects the browser here; on correct operator password
it calls complete_authorization to mint the code and 302s to the client.
"""

from __future__ import annotations

import os

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .provider import RomionOAuthProvider

_LOGIN_FORM = """<!doctype html><html><body style="font-family:sans-serif;max-width:24rem;margin:4rem auto">
<h3>TinyPyMCP — operator authorization</h3>
<form method="post" action="/oauth/operator-login">
  <input type="hidden" name="pid" value="{pid}">
  <p><input type="password" name="password" placeholder="operator secret" style="width:100%;padding:.5rem"></p>
  <button style="padding:.5rem 1rem">Authorize</button>
</form></body></html>"""


def oauth_auth_settings(issuer_url: str) -> AuthSettings:
    """AuthSettings for the self-contained AS (DCR + revocation enabled)."""
    return AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=issuer_url,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
    )


def register_operator_login(mcp, provider: RomionOAuthProvider) -> None:
    """Register the /oauth/operator-login human gate on a FastMCP instance."""
    @mcp.custom_route("/oauth/operator-login", methods=["GET", "POST"])
    async def operator_login(request: Request):
        if request.method == "GET":
            return HTMLResponse(_LOGIN_FORM.format(pid=request.query_params.get("pid", "")))
        form = await request.form()
        try:
            url = provider.complete_authorization(str(form.get("pid", "")), str(form.get("password", "")))
        except PermissionError:
            return PlainTextResponse("invalid operator secret", status_code=401)
        if not url:
            return PlainTextResponse("authorization request expired or invalid", status_code=400)
        return RedirectResponse(url, status_code=302)


def build_oauth_mcp(issuer_url: str, operator_secret: str, port: int = 8765,
                    instructions: str | None = None) -> FastMCP:
    provider = RomionOAuthProvider(issuer_url, operator_secret)
    public_host = os.environ.get("MCP_PUBLIC_HOST", "tiny-py-mcp.romionologic.dev")
    sec = TransportSecuritySettings(
        allowed_hosts=[public_host, "127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}", "testserver"],
        allowed_origins=[f"https://{public_host}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
    )
    mcp = FastMCP(
        name="TinyPyMCP",
        instructions=instructions or "TinyPyMCP (OAuth-protected).",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[],
        ),
        transport_security=sec,
        stateless_http=True,
    )
    if hasattr(mcp, "settings"):
        mcp.settings.streamable_http_path = "/mcp/v5"

    @mcp.custom_route("/oauth/operator-login", methods=["GET", "POST"])
    async def operator_login(request: Request):
        if request.method == "GET":
            return HTMLResponse(_LOGIN_FORM.format(pid=request.query_params.get("pid", "")))
        form = await request.form()
        pid = str(form.get("pid", ""))
        password = str(form.get("password", ""))
        try:
            url = provider.complete_authorization(pid, password)
        except PermissionError:
            return PlainTextResponse("invalid operator secret", status_code=401)
        if not url:
            return PlainTextResponse("authorization request expired or invalid", status_code=400)
        return RedirectResponse(url, status_code=302)

    # mcp.set_provider_hook not needed; provider closure used above.
    mcp._romion_oauth_provider = provider  # handle for tools/tests
    return mcp
