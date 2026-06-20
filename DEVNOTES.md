# TinyPyMCP — maintainer notes

Working notes for whoever develops this server. Kept short on purpose.

## What this is

A small MCP server (Python, FastMCP, `mcp` 1.27.x) exposing precise file
editing, project search and a persistent memory layer. Served to remote
connectors (Grok) over Streamable HTTP behind a Cloudflare Tunnel at
`https://tiny-py-mcp.romionologic.dev`.

Run: `MCP_AUTH_TOKEN=<secret> python -m src.server --transport http --port 8765`
(HTTP transport refuses to start without MCP_AUTH_TOKEN; set MCP_AUTH_DISABLE=1
only for trusted localhost dev. The connecting client must send
`Authorization: Bearer <secret>`.)

## SESSION FREEZE — 2026-06-19 (evening)

GREEN. Live baseline: 50 tools, `python tests/smoke.py` = 34/34. Frozen until
tomorrow (token/credit refresh overnight).

Today:
- **#1 Cloudflare automation COMPLETE** — 13 `cf_*` tools (DNS + tunnel + Access),
  tested live, writes behind confirm+audit. "Stand up a VPS service end-to-end"
  is fully programmatic. Token in ~/.romion/cloudflare.json (+ account_id).
- **#2 OAuth 2.1 AS built + PROVEN end-to-end in the mirror** C:\Work\TinyPyMCP-oauth
  (src/oauth/ store+provider+app). Self-contained (no external IdP), operator-login
  gate, PKCE (SDK-verified). Full HTTP flow 8/8 via TestClient
  (metadata→DCR→authorize→operator-login→token→protected MCP 401/200). The hard
  risk is retired.

NEXT (tomorrow, in order):
1. Integrate OAuth as a MODE in the production create_server (alongside bearer;
   MCP_AUTH_MODE=oauth, MCP_OAUTH_OPERATOR_SECRET + issuer from config). Careful —
   touches live auth wiring.
2. Add the OAuth flow to tests/smoke.py.
3. Make reachable (issuer = public tunnel URL; run OAuth mode on 8765).
4. ACCEPTANCE GATE: connect Claude / Codex / ChatGPT-Desktop / Grok (operator).
   Operator setup steps are in **CONNECTORS.md**. Only remaining unknown =
   per-connector spec quirks.
State: mirror has CF+OAuth; live repo has CF only (OAuth integration pending).

## SESSION FREEZE — 2026-06-18 ~20:50 CEST

GREEN. Baseline frozen: 37 tools, `python tests/smoke.py` = 33/33 pass, server
loads clean. This session: took over TinyPyMCP from Grok; hardened (auth
fail-closed + token-log redaction + env sanitization + C:\Work guard); built the
full GPT_MCP hot-path mirror; stood up + verified the bounded VPS deploy channel
(romion-deploy) end-to-end; added the smoke suite; refreshed README.
No work left mid-flight. Nothing touched in mcp-tests (agent-owned) or on the VPS
beyond the deploy channel.

NEXT SESSION (fresh head, in order):
1. P1 hardening — docker≈root containment (rootless/sudo-allowlist) + decide
   TinyPyMCP OS-level sandbox (Option B).
2. P1.5 — real MCP OAuth (so hosted connectors don't need token-in-URL).
3. Cloudflare API automation (scoped token) — make deploy fully programmatic.
4. Real payload: clone a foreign JS repo → port → deploy via the channel.
(Full detail below.)

## Current baseline (verified)

- 15 tools: find_phrase_occurrences, safe_replace_in_line, edit_code_block,
  read_file, list_files, search_codebase, write_file, create_file,
  get_project_structure, memory_{get_state,set_state,save,search,create_task,get_tasks}.
- Transport: `streamable-http`, `stateless_http=True`, `json_response=False`.
- Endpoint path: `/mcp/v5` (`streamable_http_path` / `sse_path`).
- Filesystem scope: all tool paths confined to `C:\Work` via
  `src/utils/path_guard.py::ensure_within` (read + write). Outside → PermissionError.
- Memory: SQLite at `data/memory.db` (created on first write; not committed).
  Keyword search only, no embeddings yet.

## Tests

`python tests/smoke.py` — offline regression over the whole tool surface (33
checks: path_guard, file ops incl. patch/soft-delete/restore/list_trash, runner
+ env sanitization, code-intel, index, memory, channel config, create_server
tool count). Self-contained (temp dirs/DB under C:\Work, cleans up). Run before
shipping; update the create_server tool-count assertion when adding tools.

## Hard rules (these have broken before — do not regress)

1. **Do not bump the endpoint path casually.** The connector URL in Grok and
   `streamable_http_path` here MUST match exactly. Every version bump that
   touched only one side caused 404 on every tool call. Path is now pinned to
   `/mcp/v5`; leave it. Bump only with a matching connector update, deliberately.
2. **Keep `transport_security`.** Without the allowed_hosts/origins the tunnel
   gets 421 "Invalid Host header". It has been accidentally stripped before.
3. **Keep every file tool routed through `ensure_within`.** Any new tool that
   touches the filesystem must call it, or the C:\Work sandbox is bypassed.
4. **Prefer point edits over whole-file rewrites of `server.py`.** Rewrites are
   how rules 1–3 got broken.

## Mission

Make this server CAPABLE of driving (via an agent) the full pipeline: fetch a
foreign JS repo → port to Python → stand up a container on a VPS → move the
ported repo there → set up DB/router/tunnel as needed → test. The server
provides the tools; the agent orchestrates.

Three new capability domains beyond the current local file workshop:
A. Process execution (local) — run allowlisted binaries (git/node/npm/python/
   pytest/docker), cwd confined to a workspace, timeout + output caps.
B. Network — clone repos, fetch archives, HTTP probe, npm/pypi lookups.
C. Remote/VPS — SSH exec, file transfer, remote docker/compose, on a scoped
   deploy credential. Secrets server-side only, never as tool args.

## Roadmap (staged; each shippable independently)

- [x] **Stage 0 — Auth.** Bearer token gate on the HTTP endpoint (pure ASGI
      middleware, `src/auth_middleware.py`), fail-closed without MCP_AUTH_TOKEN.
      Prerequisite: exec/SSH on an unauthenticated public tunnel = RCE.
- [x] **Stage 1 — Acquire + local exec.** `src/exec/runner.py`. Tools:
      `run_command` (allowlist: git/node/npm/npx/python/pip/pytest/ruff/uv; no
      shell, args as list; cwd confined to C:\Work; timeout+output caps) and
      `clone_repo` (git clone into workspace, network allowed, no clobber).
      Workspace root: MCP_WORKSPACE_ROOT, else C:\Work\TinyPyMCP\workspaces.
      Covers mission steps 1, 2, 6a. 17 tools total now.
      HARDENING (harvested from C:\Work\mcp\core\process_tools_safe.js): child
      env is an ALLOWLIST (SAFE_ENV_KEYS) — never the full parent env, so a
      child can't read MCP_AUTH_TOKEN/secrets. Bare program names only (no
      path), arg count/length limits. Still TODO: audit logging of runs (prod
      does process_start/process_finish).
- [x] **Stage 2 — Network helpers.** `src/net/fetch.py`. Tools: `http_probe`
      (any URL, follows redirects, capped body — test endpoints/fetch pages),
      `check_npm_package`, `check_pypi_package` (registry lookups for porting).
      Network unrestricted by policy; timeout + body caps only. 20 tools total.
- [x] **Stage 3 — Remote/VPS deploy channel.** LIVE + verified end-to-end.
      Separate project C:\Work\romion-deploy: a bounded FastAPI "deploy channel"
      (typed compose up/down/restart/ps/logs over an allowlist; no shell). On the
      VPS it runs as the `deploy` user as a CONTAINER `romion-deploy` on network
      romion_llm_llm_edge (docker.sock mounted — reviewed exception; risk ≈
      deploy-in-docker-group). Exposed at https://deploy.romionologic.dev via the
      existing cloudflared tunnel (route -> http://romion-deploy:8091) behind
      Cloudflare Access Service Auth (dedicated service token "romion-deploy",
      app policy SERVICE AUTH) + app bearer DEPLOY_AUTH_TOKEN. TinyPyMCP client
      (vps_status/vps_request) reaches it with creds from ~/.romion/
      deploy-channel.json. Verified: external GET /v1/status -> 200.
      REMAINING (operational, not capability): register a real stack in the
      channel's app/stacks.json (name->dir under /home/deploy/apps) + restart
      the container, then `vps_request POST /v1/compose/<stack>/up` deploys it.
      TEMPLATE (from C:\Work\mcp\core\remote_site\shared_runtime.js): config by
      reference — a JSON config path holding host/port/username/privateKeyPath/
      roots/limits; the SSH KEY stays on disk, referenced by path, NEVER a tool
      arg. A `with_sftp(config_ref, fn)` opens/uses/always-closes the connection
      with bounded roots + strict path normalization (no traversal/absolute).
      Reimplement that pattern in Python (paramiko or asyncssh). Existing VPS
      bounded executor `router.romionologic.dev` (/v1/exec, /v1/files) may be
      called instead of raw SSH for in-/workspace work; deploy steps (docker/
      nginx/tunnel) exceed the bounded worker and need SSH-as-ubuntu or a new
      bounded deploy capability — decide then.
- When TinyPyMCP runs on the VPS in a container behind the tunnel, switch/augment
  auth to trust Cloudflare Access (`cf-access-jwt-assertion` header), like
  C:\Work\mcp\core\auth.js, instead of/alongside the app-layer bearer.

Security model: on the machine, bound by task scope (allowlist + workspace
confinement, evolving toward per-job policies). On the network, unrestricted.

## Cross-cutting additions (not numbered mission stages)

- [x] **Audit log.** `src/utils/audit.py` → JSONL at logs/.tinypymcp-audit.jsonl
      (MCP_AUDIT_LOG to override). `run_command`/`clone_repo` emit
      process_start/process_finish (sizes/flags only, never full output).
- [x] **Code intelligence.** `src/code/deps.py`. Tools `code_dependencies`
      (static import graph for Py/JS/TS, NO execution; counts + fan-in/out hot
      files + top externals + unresolved; full graph on include_graph=true) and
      `code_impact` (trace dependents/dependencies of one file to max_depth).
      Harvested from C:\Work\mcp\core\code. Root-sensitive: point `path` at the
      import root (repo root), not a subdir, or absolute imports won't resolve.
      Tested on the repo itself and on _repos_with_code_samples fixtures.
      22 tools total.
- [x] **Search index.** `src/code/search_index.py`. Tools `build_index`
      (persistent SQLite inverted index token->file/line under
      <workspace>/.index/<hash>.db) and `search_index` (AND-of-tokens, returns
      file/line/text). Tested on ECC-main fixture: 3000 files / 624k lines
      indexed in ~23s, then queries in ~18ms. 24 tools total. NOTE: index is
      heavy (~60MB for 3000 files — postings store every token occurrence);
      future optimization: dedupe (token,file_id) or cap postings per token.

- [x] **VPS channel client (Stage 3, client half).** `src/vps/channel.py`. Tools
      `vps_status` and `vps_request` call a bounded VPS channel (router or the
      new deploy channel) behind Cloudflare Access Service Auth. Credentials live
      in a JSON config on disk (base_url + cf_access_client_id/secret + optional
      bearer), read here — NEVER tool args/logs. Request path is appended to the
      configured base_url, so only that one host is reachable. 26 tools.
      SECURITY INVARIANT: the channel config MUST live OUTSIDE C:\Work (default
      ~/.romion/vps-channel.json), because path_guard lets the agent's file tools
      read anything under C:\Work — a secrets file there would leak the token.
      Server half = C:\Work\romion-deploy (separate project).

## Hardening backlog (next phase — do BEFORE any real foreign-code/deploy run)

P1 — real risk:
- **docker access ≈ root.** Both the deploy container's mounted docker.sock and
  the `deploy` user's docker-group membership are root-equivalent. Decide
  containment: rootless docker, OR a sudo-allowlist of exact compose commands,
  OR accept + add tight monitoring/alerting. Biggest item; whole point of the
  bounded channel is undermined without addressing this.
- **TinyPyMCP sandbox is in-process only** (path_guard). A new tool with raw
  open()/os/subprocess bypasses it. OS-level confinement (dedicated low-priv
  user + NTFS ACL, "Option B") was deferred by operator choice to observe.
  Revisit before running unattended or once TinyPyMCP itself moves to the VPS.

P1 — real risk (FIXED 2026-06-18):
- ~~Token-in-URL leaks into access logs.~~ FIXED. `src/utils/log_redaction.py`
  (TokenRedactionFilter) scrubs `?token=` → `token=[REDACTED]` in uvicorn access
  records; wired onto the "access" handler via build_redacting_log_config()
  passed to uvicorn.run(log_config=...). Verified: a uvicorn-style access record
  with a token in args comes out redacted, raw token gone. Parity with GPT_MCP's
  perf-log redaction. (GPT_MCP's old plaintext token entries are a DEAD token —
  rotated immediately when added; current token is redacted there too.)

P1.5 — auth for hosted connectors:
- ChatGPT's MCP connector supports ONLY: (a) "no auth" + token in the URL, or
  (b) full OAuth 2.1. It does NOT work with Cloudflare Access (operator tried
  for days — confirmed dead end). So:
  - DONE: auth_middleware now also accepts `?token=<token>` query param (not
    just the Authorization header). ChatGPT config = URL
    https://tiny-py-mcp.romionologic.dev/mcp/v5?token=<MCP_AUTH_TOKEN> + auth
    set to "No authorization". Tradeoff: token in URL is logged by proxies;
    rotate if a log leaks. Header path stays for programmatic clients.
  - IN PROGRESS (2026-06-19): real MCP OAuth 2.1 being built in a MIRROR at
    C:\Work\TinyPyMCP-oauth (live repo stays the untouched reference for rollback).
    Path: SELF-CONTAINED AS (no external IdP) — implement the SDK's
    OAuthAuthorizationServerProvider (9 methods) + AuthSettings, FastMCP mounts
    create_auth_routes (metadata/.well-known, DCR /register, /authorize, /token,
    /revoke) and protects the endpoint via the provider's token verify. CF Access
    ruled out (ChatGPT incompat, confirmed). Operator-login gate at /authorize
    (single operator, secret from config) so the flow isn't open.
    DONE: src/oauth/store.py (SQLite) + src/oauth/provider.py
    (RomionOAuthProvider — 9 methods + complete_authorization gate). Unit-tested
    end-to-end in isolation: register/get_client, authorize→login-url+pending,
    operator-secret gate (bad rejected), code mint→load→exchange→token, single-use
    code, access-token validate, refresh (rotates), revoke. SDK verifies PKCE.
    DONE: src/oauth/app.py (build_oauth_mcp — wires provider+AuthSettings into
    FastMCP, mounts metadata/DCR/authorize/token/revoke, + /oauth/operator-login
    route). FULL HTTP FLOW PROVEN via Starlette TestClient (8/8): metadata→DCR(201)
    →authorize(302→login)→bad-secret(401)→login(302+code)→token(200,PKCE verified
    by SDK)→MCP no-token(401)→MCP with-Bearer(200). The hard risk (a working AS)
    is retired.
    INTEGRATED into production (2026-06-20): create_server(auth_mode="oauth", ...)
    + main() MCP_AUTH_MODE=oauth (fail-closed without MCP_OAUTH_OPERATOR_SECRET);
    src/oauth/ promoted from mirror; bearer mode untouched. Smoke now 35/35 incl.
    the full OAuth flow (401 anon / 200 with token via TestClient). Run via CLI
    flags (secrets from file, flags > env): `--auth oauth|bearer|none`,
    `--secret-file <json{operator_secret,issuer}>`, `--token-file <json{token}>`.
    e.g. `python -m src.server --auth oauth --secret-file
    C:\Users\mczyz\.romion\oauth_secret.json --transport http --port 8765`.
    ACCEPTANCE GATE (operator, live OAuth on tunnel):
    - [x] ChatGPT Desktop (2026-06-20) — PASS. Full OAuth flow (DCR→authorize→
      operator-login→token), all 50 tools listed + REAL execution confirmed
      (cf_verify_token 200, list_files, get_info returned data). Confirm-guard
      blocked an accidental cf_create_service_token (no args/confirm → validation
      error, nothing created). The hardest connector works.
      Notes (harmless): tools appear after the connector's list refresh; ChatGPT
      probes /.well-known/openid-configuration (404) then falls back to our
      oauth-authorization-server metadata (200) — we're OAuth, not OIDC; fine.
    - [x] Claude (2026-06-20) — PASS. Clean handshake; tools live IN Claude Code
      this session; real execution from Claude's side confirmed (cf_verify_token
      200, list_files returned data). Claude also probes ListResources/ListPrompts
      (we return empty 200). 2/4.
    - [x] Grok (2026-06-20) — PASS. Clean OAuth handshake + real execution
      (CallToolRequest; cf_verify_token hit api.cloudflare.com 200, list_files/
      get_info returned data). Poetic: the connector that started the whole saga
      (cache/confabulation) now works flawlessly over OAuth. 3/4.
    - [x] Codex (2026-06-20) — PASS. OAuth + real execution; uses a LOOPBACK
      redirect (http://127.0.0.1:<port>/callback/...) which our AS handles.
    **GATE 4/4 COMPLETE — #2 OAuth 2.1 DONE.** All four hosted connectors connect
    via our self-contained AS and execute tools. No external IdP. Bearer + OAuth
    + none all selectable via --auth.
    Steps in CONNECTORS.md.

P2 — hardening:
- **Mutation discipline (idea from GPT's preference for gpt-mcp).** TinyPyMCP's
  mutating tools (write_file/edit_code_block/safe_replace, and deploy ops) have
  only .bak backups — no workflow state machine, no pre-change snapshot, no
  dry-run/validate gate. gpt-mcp has "workflow/state/snapshot discipline" so GPT
  routes risky mutations there. Give TinyPyMCP a lightweight equivalent: a
  snapshot-before-mutate + an operational state file + a dry-run/validate step,
  so its mutations are reversible and governed. NOTE: this is operational state,
  distinct from the SQLite long-term memory.
- Deploy channel: add audit logging to the compose runner (parity with the
  local runner); add rate limiting (router has it, channel doesn't); run the
  container as non-root (docker-group GID), read-only rootfs where the socket
  allows, drop caps.
- Secret rotation cadence: CF Access service tokens, DEPLOY_AUTH_TOKEN, the
  TinyPyMCP bearer. Document how to rotate each without downtime.
- When TinyPyMCP → VPS container: switch auth to trust CF Access
  (cf-access-jwt-assertion) behind the tunnel; stop exposing a raw bearer.

P3 — cleanups / foot-guns:
- ~~allowed_hosts hardcodes :8765~~ FIXED: create_server(port=...) derives local
  host entries from the bound port; main passes args.port.
- search index size (~60MB/3k files) — dedupe (token,file_id) postings.
- Optional: typed deploy tools in TinyPyMCP (deploy_up/ps/down) over the generic
  vps_request, for agent ergonomics. Server boundary already holds either way.
- Normalize the odd 0707 dir perms left by scp under /home/deploy/romion-deploy.

## Mirror plan (from GPT_MCP usage analysis — C:\Work\mcp\.mcp_audit.log, 31k events)

Goal: TinyPyMCP as a STABLE fallback for the hot dev path when GPT_MCP (server_tools.js)
is flaky. Already mirrored: run_command, write_file, read_file(+lines), build_index,
search_index, list_files, get_project_structure (~70% of hot usage).
Gaps (by GPT_MCP usage count), all through ensure_within + audit:
- DONE 2026-06-18: append_file (958), get_info (139), copy_path (106),
  move_path (34), delete_path soft-delete to .trash (25). 31 tools now.
- DONE 2026-06-18: code_symbols (73); search_index `context` param
  (= search_index_context); index_status (158); edit_file_patch (901, SAFE
  variant — multi-hunk exact-replace, each find must match once, atomic+backup+
  dry-run, NOT fuzzy diff so it can't corrupt); plus restore_path + list_trash
  to complete the soft-delete trio; read_file_chunk (36, byte-range seek read).
  **Mirror COMPLETE. 37 tools.**
NOT a simple mirror (these are the P2 mutation-discipline item, not tools to copy):
deploy_decision_guard, change_workflow_simulator, project_truth_audit, tool_usage_snapshot,
the deploy_* pipeline. The stc_safe_* search/fetch surface is connector doc-search, not dev.
(.mcp_pref.log does not exist — audit log is the only usage source.)

## Cloudflare API automation — IN PROGRESS (2026-06-19)

- DONE: `src/cf/client.py` + tools cf_verify_token, cf_list_dns (read),
  cf_create_dns_record / cf_delete_dns_record (write, behind confirm=true +
  audit). Verified live: token active, zone romionologic.dev (b00a..), 13 DNS
  records; create+delete TXT round-trip OK. Token at ~/.romion/cloudflare.json
  (PC for now; re-home to VPS w/ secret hardening at migration). 41 tools.
  Scoped token perms: Tunnel/Access(Apps+ServiceTokens)/DNS Edit, zone-scoped,
  no IP filter yet (calls originate from PC; lock to VPS IP after migration).
- DONE: tunnel automation — cf_list_tunnels, cf_get_tunnel_config (read),
  cf_add_tunnel_route / cf_remove_tunnel_route (GET→modify→PUT, preserves all
  other ingress rules + config keys, confirm+audit). Tested live on the deploy
  tunnel with 1:1 ingress restore. account_id must be in the CF config (token
  can't list /accounts). 45 tools.
- DONE: Access automation — cf_create_access_app/delete, cf_add_access_service_policy
  (decision=non_identity, confirmed against the live working romion-deploy app),
  cf_create_service_token/delete. Tested live (create app+token+policy → verify
  non_identity+token include → delete). NOTE: cf_create_service_token returns the
  client_secret ONCE in its response — handle carefully (store in a config outside
  C:\Work, never echo). 50 tools.
- **#1 CLOUDFLARE AUTOMATION COMPLETE** — "stand up a VPS service end-to-end"
  (service token + Access app + policy + tunnel route + DNS CNAME) is now fully
  programmatic via cf_* tools. No dashboard needed. Last night's manual clicking
  is replaced.

## R2 / restic backup management — DECIDED (2026-06-19), queued

Manage backups ON THE VPS (creds + restic password already live there per
vps.md: romion-r2-backup.timer / romion-backup.sh). Bounded endpoint (extend
romion-deploy or a sibling), reusing the existing restic R2 config — do NOT copy
creds to the PC, do NOT use the 22-perm Workers token. Standard restic ops:
read-open (snapshots list, stats, `restic check`); prune/forget/restore behind
confirm + audit. NEVER raw-delete R2 objects (corrupts the restic repo) — always
via restic. R2 has two cred models: CF API token (R2 Storage perm) = bucket
level; S3 Access Key/Secret = object level (what restic uses). Build step 1:
surface the VPS restic config read-only (repo URL + where creds/password live),
no secrets in chat, so the endpoint reuses them.

## Post-hardening task (queued)

- **Automate Cloudflare config via the CF API.** Turn tonight's manual dashboard
  steps into tool calls: create/update tunnel public-hostname routes, Access
  apps + policies, Access service tokens, and DNS (CNAME) records. Goal: stand
  up a new VPS service end-to-end (container via deploy channel + route + Access
  + token + DNS) fully programmatically, no dashboard clicking.
  - Use a NARROWLY-scoped CF API token: Edit on only Cloudflare Tunnel + Access
    (Apps/Policies + Service Tokens) + DNS, scoped to the romionologic.dev zone/
    account; everything else ungranted = denied. Add Client IP filter (VPS IP
    57.129.71.150, since the call originates there) + a TTL. Token lives on the
    VPS in a file outside any agent-readable sandbox, config-by-ref, never an arg.
  - CF API docs are fetchable (WebFetch) for current endpoints/permission names.

## Ops / resilience backlog (operator request 2026-06-18)

1. **Auto-restart the server if it dies** (once TinyPyMCP runs on the VPS).
   Likely no custom code: run it as a container with `restart: unless-stopped`
   (like romion-deploy / romion-llm-*) or a systemd unit with
   `Restart=on-failure`. The supervisor IS the "automat" — decide container vs
   systemd when we containerize TinyPyMCP for the VPS.
2. **Trigger: change the exposed tool list WITHOUT restarting.** Needs the MCP
   `tools.listChanged` capability + a runtime tool registry the server can
   enable/disable + emitting `notifications/tools/list_changed` so clients
   refetch. This is exactly the session_toolset / list_changed pattern on the
   workbench (session_toolset_plan, list_changed_notification_bus — currently
   dry-run there). Real feature work; wire the emitter, then flip the capability.
3. **Trigger: re-stand the cloudflared tunnel if it dies**, and MOVE it off the
   operator's PC onto the VPS (so it doesn't depend on the PC being on). On the
   VPS: cloudflared as a container with `restart: unless-stopped` (romion-llm-
   cloudflared already does this) + a tunnel public-hostname route for
   tiny-py-mcp → the TinyPyMCP container. Ties to #1: once both are VPS
   containers on the tunnel network, Docker handles restarts and the PC
   dependency disappears.

## Hardening pass — 2026-06-20

TIER 1 DONE (code-level, smoke 35/35):
- A. Every mutation now audited (added write_file/safe_replace/edit_code_block;
  file-mgmt + CF writes already were).
- B. Exec allowlist configurable via MCP_EXEC_ALLOWLIST (lock-down profile, e.g.
  "git" drops interpreters).
- C. CF critical-resource guards: delete_dns / remove_tunnel_route /
  delete_access_app refuse protected production hosts (router/deploy/apex/etc.,
  MCP_CF_PROTECTED-configurable) unless force=true. Verified live: blocked
  without deleting.
- D. Per-IP rate limit middleware (MCP_RATE_LIMIT_PER_MIN, default OFF to not
  break connector bursts; opt-in + tune).

TIER 2 — the REAL boundary, needs the VPS move (NOT yet done):
- **Exec confinement.** run_command python/node = arbitrary code at the server
  user's privilege. Code guards (B) limit WHICH binary but not what an interpreter
  does. Only a non-root container with a bounded FS (the planned VPS move) truly
  confines it. This is why "thorough hardening" converges on containerizing
  TinyPyMCP on the VPS.
- docker≈root containment (rootless / sudo-allowlist) on the deploy channel.
- Secret hardening: ~/.romion plaintext on PC → VPS perms 600 / systemd/Docker
  secrets; rotation cadence (CF token, operator secret, deploy token).

TIER 3: search index posting dedupe (size).

## Known foot-guns

- ~~`transport_security` allowed_hosts hardcodes `:8765`~~ FIXED 2026-06-18:
  create_server(port=...) builds local host entries from the bound port.

## Open items

- `src/server.py.bak` — leftover backup from a full rewrite; remove once happy.
- `edit_code_block` writes with universal-newline translation (\r\n -> \n on
  write). Cosmetic; matches existing safe_replace behavior. Revisit if it bites.
