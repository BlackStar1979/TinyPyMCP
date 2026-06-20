# Connecting agents to TinyPyMCP (OAuth 2.1)

How to add TinyPyMCP as an MCP connector using the OAuth login flow.

> STATUS (2026-06-20): OAuth is INTEGRATED into the production server and
> regression-tested. Start it in OAuth mode (cloudflared up):
> `python -m src.server --auth oauth --secret-file C:\Users\mczyz\.romion\oauth_secret.json --transport http --port 8765`
> where oauth_secret.json = {"operator_secret":"<secret>","issuer":"https://tiny-py-mcp.romionologic.dev"}.
> Then add connectors per below. Remaining = the cross-connector acceptance gate.
> Bearer stays available via `--auth bearer --token-file ...`.

## What you'll need
- The server running in OAuth mode, reachable at
  `https://tiny-py-mcp.romionologic.dev/mcp/v5` (tunnel up).
- The **operator secret** you set as `MCP_OAUTH_OPERATOR_SECRET` when starting
  the server. This is what you type on the TinyPyMCP authorization page during
  each connector's login — it's the human gate. Keep it private.

## The flow (same idea for every client)
1. Add a **custom MCP connector / server**, URL = `https://tiny-py-mcp.romionologic.dev/mcp/v5`.
2. Set Authentication to **OAuth** (not "No authorization", not token-in-URL).
   The client discovers our OAuth automatically (metadata + Dynamic Client
   Registration) — **you do NOT paste any client_id/secret**; it self-registers.
3. The client opens a browser to authorize. You'll land on the **TinyPyMCP
   "operator authorization" page** → enter the **operator secret** → submit.
4. You're redirected back and the connector is linked. Done.

No tokens to copy by hand — the OAuth handshake does it all.

## Per-client notes (UI differs; principle is identical)
- **Claude (Desktop / claude.ai):** Settings → Connectors → Add custom connector
  → paste the URL. OAuth is auto-detected; complete the browser login.
- **ChatGPT Desktop:** Settings → Connectors → New → server URL → Authentication
  = **OAuth** (the earlier "does not implement OAuth" error is gone once OAuth
  mode is live). Complete the browser login.
- **Codex:** add the MCP server by URL; choose OAuth; complete login.
- **Grok (grok.com/connectors):** New connector → URL → OAuth → complete login.

## If a connector misbehaves (the acceptance-gate step)
Hosted connectors implement the MCP OAuth spec slightly differently (DCR / PKCE /
redirect handling). If one fails to connect, capture what it shows and we adjust
the server — that's exactly the cross-connector test we planned. The four above
are the acceptance gate: when all connect, OAuth is signed off.

## Revoking access
A connector's access can be cut by deleting its token from the OAuth store
(tooling for this comes with the integration), or by rotating
`MCP_OAUTH_OPERATOR_SECRET` (blocks new authorizations).
