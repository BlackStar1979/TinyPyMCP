# TinyPyMCP

> ⚠️ **OPERATOR-ONLY / AUTHENTICATED ADMIN SERVER.** TinyPyMCP exposes
> `run_command` (allowlisted local code execution) and Cloudflare cloud-admin
> tools. The in-process `path_guard` is **not** an OS security boundary — a
> compromised or prompt-injected authenticated caller can run code with the
> server process's privileges. Run it only behind auth (bearer/OAuth), only for
> a trusted operator, never as an open/multi-tenant public connector. Before any
> exposure, harden at the OS level: low-privilege user, filesystem ACLs, and
> ideally a container (see DEVNOTES "Hardening Tier 2"). Audit: 2026-06-20.

A Python MCP server: a sandboxed workshop for an agent — file read/edit, code
intelligence, search index, local command execution, package lookups, persistent
memory, and a client for bounded VPS channels. All filesystem access is confined
to `C:\Work`. **76 tools.**

For development/maintenance notes and the roadmap, see `DEVNOTES.md`.

---

## Run

**Local clients (Claude Desktop, Cursor) — stdio, no auth:**
```powershell
python -m src.server
```

**Remote / hosted connectors — HTTP (Streamable HTTP), auth required:**
```powershell
# Bearer mode (token from file): bearer_token.json = {"token": "<secret>"}
python -m src.server --auth bearer --token-file C:\Users\mczyz\.romion\bearer_token.json --transport http --port 8765
# OAuth 2.1 mode (recommended for hosted connectors): see below.
```
Auth is selected by `--auth bearer|oauth|none` (default bearer). Secrets come
from `--token-file` / `--secret-file` (JSON, kept outside C:\Work) or env. `none`
runs open — never on a public tunnel.
Endpoint: `http://127.0.0.1:8765/mcp/v5` (public via Cloudflare Tunnel at
`https://tiny-py-mcp.romionologic.dev/mcp/v5`).

The HTTP transport **refuses to start without `MCP_AUTH_TOKEN`** (fail-closed),
because the tunnel exposes tools like `run_command`. For trusted localhost-only
dev you may set `MCP_AUTH_DISABLE=1` — never on port 8765 while the tunnel runs.

### How clients authenticate
- **Header clients:** send `Authorization: Bearer <secret>`.
- **ChatGPT connector** (can't send custom headers): set the URL to
  `https://tiny-py-mcp.romionologic.dev/mcp/v5?token=<secret>` and Authentication
  to **"No authorization"**. The token in the URL is redacted from logs.

## Environment variables
| Var | Purpose |
|---|---|
| `MCP_AUTH_TOKEN` | Bearer/query token for HTTP. Required unless `MCP_AUTH_DISABLE=1`. |
| `MCP_AUTH_DISABLE=1` | Run HTTP without auth (trusted local only). |
| `MCP_PUBLIC_HOST` | Public host for the DNS-rebinding allowlist (default tiny-py-mcp.romionologic.dev). |
| `MCP_MEMORY_DB` | SQLite memory path (default `data/memory.db`). |
| `MCP_WORKSPACE_ROOT` | Where `clone_repo`/indexes live (default `workspaces/`). |
| `MCP_AUDIT_LOG` | Audit JSONL path (default `logs/.tinypymcp-audit.jsonl`). |
| `MCP_VPS_CONFIG` | VPS-channel creds JSON (default `~/.romion/vps-channel.json`, kept OUTSIDE C:\Work). |
| `MCP_AUTH_MODE` | `bearer` (default) or `oauth`. Selects HTTP auth. |
| `MCP_OAUTH_OPERATOR_SECRET` | OAuth mode: the secret you type on the authorization page (the human gate). Required for oauth mode. |
| `MCP_OAUTH_ISSUER` | OAuth mode: public issuer URL (default `https://<MCP_PUBLIC_HOST>`). |
| `MCP_OAUTH_DB` | OAuth store path (default `data/oauth.db`). |
| `MCP_EXEC_ALLOWLIST` | Comma/space-separated exec allowlist override (e.g. `"git"` drops interpreters). Default: git/node/npm/python/pytest/... |
| `MCP_CF_PROTECTED` | Override the protected-CF-host denylist (delete/remove of these needs force=true). |
| `MCP_RATE_LIMIT_PER_MIN` | Per-IP request cap (0 = off, default). Anti-runaway; set generous to not break connector bursts. |

### OAuth 2.1 mode (standard, for hosted connectors)
```powershell
python -m src.server --auth oauth --secret-file C:\Users\mczyz\.romion\oauth_secret.json --transport http --port 8765
```
`oauth_secret.json` (keep OUTSIDE C:\Work):
```json
{ "operator_secret": "<the secret you type at login>", "issuer": "https://tiny-py-mcp.romionologic.dev" }
```
The server becomes its own OAuth authorization server (DCR + PKCE; no external
IdP). Connectors discover it automatically; during each connector's login you
enter the operator secret on the TinyPyMCP page. See **CONNECTORS.md**.

(Env-var form also works: `MCP_AUTH_MODE=oauth`, `MCP_OAUTH_OPERATOR_SECRET`,
`MCP_OAUTH_ISSUER`. Flags take precedence.)

## Tools (76)
Grouped by domain. The authorization boundary is which **profiles** are enabled
at launch (read_only / operator_admin / cloud_admin — see `src/profiles.py`);
`confirm=true` on a tool is an accidental-mutation guard, not authorization.
- **Read/inspect:** read_file, read_file_chunk, get_info, list_files, get_project_structure, find_phrase_occurrences, search_codebase
- **Edit/write:** write_file, create_file, append_file, safe_replace_in_line, edit_code_block, edit_file_patch
- **Manage:** copy_path, move_path, delete_path (soft → .trash), restore_path, list_trash
- **Code intelligence:** code_dependencies, code_impact, code_symbols, build_index, search_index, index_status
- **Exec/acquire:** run_command (allowlisted), clone_repo
- **Network:** http_probe, check_npm_package, check_pypi_package
- **Memory (SQLite + sqlite-vec):** memory_get_state, memory_set_state, memory_save, memory_search (semantic KNN via bge-m3 embeddings, lexical fallback), memory_reindex, memory_create_task, memory_get_tasks
- **VPS channel:** vps_status, vps_request
- **VPS filesystem (read-only, whole host)** via the `/hostfs` ro bind-mount, NOT path_guard-confined: vps_fs_list, vps_fs_stat, vps_fs_read. Read any path on the VPS. Secret-file bytes are withheld unless `MCP_FS_SECRET_MODE=allow` (an air-gapped, no-egress instance).
- **VPS docker (host control)** via the mounted `docker.sock` (uid 10001 in host group `docker`): vps_docker. Read subcommands (ps/logs/inspect/...) ungated; mutating ones (run/exec/rm/stop/restart/build/compose/...) require `confirm=true` and are audited.
- **Cloudflare admin** (token from ~/.romion/cloudflare.json; writes need confirm=true): cf_verify_token, cf_list_dns, cf_create_dns_record, cf_delete_dns_record, cf_list_tunnels, cf_get_tunnel_config, cf_add_tunnel_route, cf_remove_tunnel_route, cf_create_access_app, cf_delete_access_app, cf_add_access_service_policy, cf_create_service_token, cf_delete_service_token
- **OVH host** (consumer key from ~/.romion/ovh.json; writes need confirm=true): ovh_vps_info, ovh_snapshot_status, ovh_automated_backup_status, ovh_images_available, ovh_create_snapshot, ovh_revert_snapshot, ovh_abort_snapshot, ovh_automated_backup_restore, ovh_reboot
- **OVH AI Endpoints** (OpenAI-compatible Bearer key from ~/.romion/ovh-ai.json; read-only, clean-IP VPS path; key never surfaced): ovh_ai_embeddings, ovh_ai_chat
- **Uptime Kuma** (socket.io from ~/.romion/kuma.json; writes need confirm=true): kuma_list_monitors, kuma_monitor_status, kuma_add_monitor, kuma_pause_monitor, kuma_resume_monitor
- **GitHub** (token from ~/.romion/github.json; PR mutations need confirm=true): gh_repo_info, gh_list_prs, gh_get_pr, gh_create_pr, gh_merge_pr

## Security model
- **Sandbox:** every `C:\Work` file tool resolves paths through `path_guard.ensure_within`
  — confined to `C:\Work` (`..`/symlink escapes blocked). Network is unrestricted.
- **Whole-VPS read plane** (`vps_fs_*`) is deliberately NOT confined: it reads the
  host root via a read-only bind-mount. Read-only is the boundary (no write/exec).
  Secret-file CONTENTS are withheld per `MCP_FS_SECRET_MODE` (default `redact`);
  an air-gapped instance with no internet egress can set `allow` to read/manage
  secrets safely.
- **Exec:** `run_command` runs only allowlisted binaries (git/node/npm/python/
  pytest/...), no shell, child env stripped to a safe allowlist (secrets don't
  leak), timeout + output caps, cwd confined to `C:\Work`.
- **Auth:** bearer header or `?token=` query, fail-closed; redacted from logs.
- **Deletes are soft** (move to `.trash`, reversible via `restore_path`).
- **Secrets** (VPS channel tokens) live OUTSIDE C:\Work so the agent's own file
  tools can't read them.
- All mutations and command runs are recorded in the audit log.

## Tests
```powershell
python tests/smoke.py        # 49 offline checks over the whole tool surface
```

## VPS deploy channel
Heavy deploy (docker on the VPS) is handled by the separate `romion-deploy`
service (`C:\Work\romion-deploy`), reached via `vps_request`. See its README.
