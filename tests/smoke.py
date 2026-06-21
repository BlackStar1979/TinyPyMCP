"""
TinyPyMCP smoke test — exercises the core tool surface offline.

Run: python tests/smoke.py
Self-contained: uses temp dirs/DB under C:\\Work, no network, cleans up.
Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Route memory + index/workspace to temp BEFORE importing the modules that read
# those env vars at import time.
_TMP = tempfile.mkdtemp(prefix="tinypymcp_smoke_", dir=r"C:\Work")
os.environ["MCP_MEMORY_DB"] = os.path.join(_TMP, "mem.db")
os.environ["MCP_WORKSPACE_ROOT"] = os.path.join(_TMP, "ws")
os.environ["MCP_OAUTH_DB"] = os.path.join(_TMP, "oauth.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import file_ops as fo          # noqa: E402
from src.utils.path_guard import ensure_within  # noqa: E402
from src.exec import runner as rn             # noqa: E402
from src.memory import store as mem           # noqa: E402
from src.code import deps, symbols            # noqa: E402
from src.code import search_index as si       # noqa: E402
from src.vps import channel as ch             # noqa: E402
from src.cf import client as cf              # noqa: E402
from src import ovh_client as ovhc              # noqa: E402
from src import kuma_client as kumac               # noqa: E402
from src.server import create_server          # noqa: E402

_passed = 0
_failed = 0


def check(name, fn):
    global _passed, _failed
    try:
        fn()
        print(f"  PASS  {name}")
        _passed += 1
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failed += 1


def expect_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def main():
    work = os.path.join(_TMP, "proj")
    os.makedirs(work, exist_ok=True)
    f1 = os.path.join(work, "a.py")

    print("path_guard")
    check("inside C:/Work allowed", lambda: ensure_within(work))
    check("outside blocked", lambda: expect_raises(PermissionError, lambda: ensure_within(r"C:\Windows\x")))
    check("traversal blocked", lambda: expect_raises(PermissionError, lambda: ensure_within(r"C:\Work\sub\..\..\Windows\evil")))
    check("oauth db blocked from file tools", lambda: expect_raises(PermissionError, lambda: ensure_within(os.environ["MCP_OAUTH_DB"])))
    check("memory db blocked from file tools", lambda: expect_raises(PermissionError, lambda: ensure_within(os.environ["MCP_MEMORY_DB"])))
    _proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check("project data dir blocked", lambda: expect_raises(PermissionError, lambda: ensure_within(os.path.join(_proj, "data", "probe.db"))))

    print("file ops")
    check("write+read", lambda: (fo.write_file_content(f1, "import os\n\ndef foo():\n    return 1\n"),
                                  ensure_within(f1).is_file()) and None)
    check("append", lambda: assert_true(fo.append_file(f1, "x = 2\n")["bytes_appended"] > 0))
    check("get_info", lambda: assert_true(fo.get_info(f1)["type"] == "file" and fo.get_info(f1)["line_count"] >= 4))
    check("create_file no-clobber", lambda: expect_raises(FileExistsError, lambda: fo.create_file(f1)))
    check("read_file_chunk", lambda: assert_true(fo.read_file_chunk(f1, 0, 6)["content"] == "import"))
    check("find_occurrences", lambda: assert_true(len(fo.find_occurrences(f1, "def foo")) == 1))
    check("safe_replace exact", lambda: assert_true(fo.safe_replace(f1, "return 1", "return 42", line_num=4, occurrence=1)["status"] == "success"))
    check("edit_file_patch apply", lambda: assert_true(fo.edit_file_patch(f1, [{"find": "x = 2", "replace": "x = 3"}])["status"] == "patched"))
    check("edit_file_patch reject (not exactly once)", lambda: expect_raises(ValueError, lambda: fo.edit_file_patch(f1, [{"find": "ZZZ_DOES_NOT_EXIST", "replace": "Y"}])))
    f2 = os.path.join(work, "b.py")
    check("copy_path", lambda: assert_true(os.path.exists(fo.copy_path(f1, f2)["dst"])))
    f3 = os.path.join(work, "c.py")
    check("move_path", lambda: assert_true(os.path.exists(fo.move_path(f2, f3)["dst"])))
    check("copy no-clobber", lambda: expect_raises(FileExistsError, lambda: fo.copy_path(f1, f3)))

    print("soft-delete trio")
    state = {}
    check("delete_path soft", lambda: state.update(trash=fo.delete_path(f3)["trash_path"]) or assert_true(os.path.exists(state["trash"])))
    check("list_trash sees it", lambda: assert_true(any(e["trash_path"] == state["trash"] for e in fo.list_trash()["entries"])))
    check("restore_path", lambda: assert_true(os.path.exists(fo.restore_path(state["trash"], os.path.join(work, "restored.py"))["to"])))

    print("search")
    check("search_codebase", lambda: assert_true(fo.search_codebase(work, "def foo", glob="*.py")["match_count"] >= 1))

    print("runner (allowlist + env sanitization)")
    check("git --version", lambda: assert_true(rn.run_command("git", ["--version"])["exit_code"] == 0))
    check("blocked program", lambda: expect_raises(PermissionError, lambda: rn.run_command("powershell", [])))
    check("env not leaked to child", _check_env_sanitized)

    print("code intelligence")
    check("dependency graph", lambda: assert_true(deps.build_dependency_graph(work)["nodes_count"] >= 1))
    check("code_symbols ast", lambda: assert_true(any(s["name"] == "foo" for s in symbols.extract_symbols(f1)["symbols"])))
    check("build_index", lambda: assert_true(si.build_index(work)["files_indexed"] >= 1))
    check("index_status", lambda: assert_true(si.index_status(work)["indexed"] is True))
    check("search_index + context", lambda: assert_true("context" in si.search_index(work, "foo", context=1)["results"][0]))

    print("memory (SQLite)")
    check("memory_save", lambda: assert_true(mem.save_memory("smoke fact", agent_name="t")["id"]))
    check("memory_search", lambda: assert_true(mem.search_memory("smoke")["results"]))
    check("memory state upsert", lambda: assert_true(mem.set_agent_state("t", current_task="x") and mem.get_agent_state("t")["current_task"] == "x"))
    check("memory tasks", lambda: assert_true(mem.create_task("t1", created_by="t") and len(mem.get_tasks()) >= 1))

    print("vps channel")
    check("missing config -> error", lambda: expect_raises(ch.ChannelConfigError, lambda: ch.call("GET", "/v1/status", config_ref=os.path.join(_TMP, "none.json"))))
    check("named channels resolve (router/deploy) + legacy", _check_vps_channels)

    print("cloudflare client")
    check("cf missing config -> error", lambda: expect_raises(cf.CFConfigError, lambda: cf.verify_token(config_ref=os.path.join(_TMP, "none.json"))))

    print("ovh client")
    check("ovh missing config -> error", lambda: expect_raises(ovhc.OVHConfigError, lambda: ovhc.vps_info(config_ref=os.path.join(_TMP, "none.json"))))

    print("kuma client")
    check("kuma missing config -> error", lambda: expect_raises(kumac.KumaConfigError, lambda: kumac.list_monitors(config_ref=os.path.join(_TMP, "none.json"))))

    print("http_probe SSRF guard")
    from src.net.fetch import _guard_public_url  # noqa: E402
    check("blocks loopback", lambda: expect_raises(PermissionError, lambda: _guard_public_url("http://127.0.0.1/")))
    check("blocks cloud metadata", lambda: expect_raises(PermissionError, lambda: _guard_public_url("http://169.254.169.254/latest/meta-data/")))
    check("blocks private LAN", lambda: expect_raises(PermissionError, lambda: _guard_public_url("http://10.0.0.1/")))
    check("blocks non-http scheme", lambda: expect_raises(ValueError, lambda: _guard_public_url("file:///C:/Windows/x")))
    check("allows public literal ip", lambda: _guard_public_url("http://8.8.8.8/"))

    print("oauth")
    check("oauth flow gates MCP (401 anon, 200 with token)", _check_oauth_flow)
    check("bearer query-token gated (off=401, on=200)", _check_query_token_gate)

    print("server")
    check("create_server loads", lambda: assert_true(len(create_server()._tool_manager.list_tools()) == 64))

    print("tool profiles")
    check("profiles partition + prune the 50-tool surface", _check_profiles)


def assert_true(cond):
    if not cond:
        raise AssertionError("expected truthy")
    return True


def _check_oauth_flow():
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from src.server import create_server as cs

    mcp = cs(auth_mode="oauth", issuer_url="http://localhost:8765", operator_secret="SMOKE", port=8765)
    app = mcp.streamable_http_app()
    v = secrets.token_urlsafe(48)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    ACC = {"Accept": "application/json, text/event-stream"}
    INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "s", "version": "0"}}}
    with TestClient(app, base_url="http://localhost:8765", follow_redirects=False) as c:
        cid = c.post("/register", json={"redirect_uris": ["http://localhost/cb"], "token_endpoint_auth_method": "none",
                     "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()["client_id"]
        az = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": "http://localhost/cb",
                   "code_challenge": ch, "code_challenge_method": "S256", "state": "s", "scope": "mcp"})
        pid = parse_qs(urlparse(az.headers["location"]).query)["pid"][0]
        lg = c.post("/oauth/operator-login", data={"pid": pid, "password": "SMOKE"})
        code = parse_qs(urlparse(lg.headers["location"]).query)["code"][0]
        at = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": "http://localhost/cb",
                    "client_id": cid, "code_verifier": v}).json()["access_token"]
        assert_true(c.post("/mcp/v5", json=INIT, headers=ACC).status_code == 401)
        assert_true(c.post("/mcp/v5", json=INIT, headers={**ACC, "Authorization": f"Bearer {at}"}).status_code == 200)


def _check_query_token_gate():
    import asyncio

    from src.auth_middleware import BearerAuthMiddleware

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def status_for(mw):
        scope = {"type": "http", "headers": [], "query_string": b"token=SEKRET"}
        seen = []

        async def send(m):
            if m["type"] == "http.response.start":
                seen.append(m["status"])

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await mw(scope, receive, send)
        return seen[0]

    off = asyncio.run(status_for(BearerAuthMiddleware(app, "SEKRET", allow_query_token=False)))
    on = asyncio.run(status_for(BearerAuthMiddleware(app, "SEKRET", allow_query_token=True)))
    assert_true(off == 401 and on == 200)


def _check_vps_channels():
    import json as _json
    from src.vps import channel as ch
    multi = os.path.join(_TMP, "vps-multi.json")
    with open(multi, "w", encoding="utf-8") as f:
        _json.dump({"default": "router", "channels": {
            "router": {"base_url": "https://router.x"},
            "deploy": {"base_url": "https://deploy.x", "bearer_token": "t"}}}, f)
    assert_true(ch.load_channel_config(None, multi)["base_url"] == "https://router.x")     # default
    assert_true(ch.load_channel_config("deploy", multi)["base_url"] == "https://deploy.x")  # named
    expect_raises(ch.ChannelConfigError, lambda: ch.load_channel_config("nope", multi))
    single = os.path.join(_TMP, "vps-single.json")
    with open(single, "w", encoding="utf-8") as f:
        _json.dump({"base_url": "https://only.x"}, f)
    assert_true(ch.load_channel_config(None, single)["base_url"] == "https://only.x")       # legacy
    expect_raises(ch.ChannelConfigError, lambda: ch.load_channel_config("deploy", single))


def _check_profiles():
    from src.profiles import READ_ONLY, OPERATOR_ADMIN, CLOUD_ADMIN
    from src.server import create_server as cs
    # disjoint tiers, totalling exactly the registered surface
    assert_true(not (READ_ONLY & OPERATOR_ADMIN))
    assert_true(not (OPERATOR_ADMIN & CLOUD_ADMIN))
    assert_true(not (READ_ONLY & CLOUD_ADMIN))
    union = READ_ONLY | OPERATOR_ADMIN | CLOUD_ADMIN
    assert_true(len(union) == 64)
    live = {t.name for t in cs()._tool_manager.list_tools()}
    assert_true(live == union)  # catches any profile name typo vs live tools
    # pruning to each tier yields the expected surface
    assert_true(len(cs(profiles=["read_only"])._tool_manager.list_tools()) == len(READ_ONLY))
    assert_true(len(cs(profiles=["read_only", "operator_admin"])._tool_manager.list_tools()) == len(READ_ONLY | OPERATOR_ADMIN))
    assert_true(len(cs(profiles=["read_only", "operator_admin", "cloud_admin"])._tool_manager.list_tools()) == 64)


def _check_env_sanitized():
    os.environ["SMOKE_SECRET_TOKEN"] = "LEAKME"
    try:
        out = rn.run_command("python", ["-c", "import os;print('SMOKE_SECRET_TOKEN' in os.environ)"])
        assert_true("False" in out["stdout"])
    finally:
        os.environ.pop("SMOKE_SECRET_TOKEN", None)


if __name__ == "__main__":
    try:
        main()
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
        shutil.rmtree(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".trash"), ignore_errors=True)
    print(f"\nSMOKE: {_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
