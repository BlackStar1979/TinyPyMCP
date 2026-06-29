"""
TinyPyMCP - estate snapshot + dashboard (the agent-steerable-infra view).

`collect_estate` aggregates a fail-open, machine-readable health snapshot of the
ROMION estate. It backs both the `estate_status` MCP tool and the human-facing
`/dashboard` + `/estate.json` HTTP routes (served on the SAME tunnel as the MCP
endpoint — no new Worker/tunnel/connector). Per-section fail-open: a failing
subsystem is reported inline, never crashing the snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


async def collect_estate(
    mcp: Any,
    *,
    include_containers: bool = True,
    include_monitors: bool = True,
    include_channel: bool = True,
) -> dict[str, Any]:
    """Aggregate the estate snapshot. `mcp` is the FastMCP instance (for the live
    tool count). All sub-aggregations are fault-isolated."""
    snap: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections": {},
    }
    secs = snap["sections"]

    try:
        tools = await mcp.list_tools()
        secs["mcp"] = {"ok": True, "tool_count": len(tools), "endpoint": "/mcp/v5"}
    except Exception as e:  # pragma: no cover - defensive
        secs["mcp"] = {"ok": False, "error": str(e)}

    if include_containers:
        try:
            from src.vps import dockerctl as _dctl
            r = _dctl.docker(["ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"])
            rows = [ln for ln in (r.get("stdout") or "").splitlines() if ln.strip()]
            items = [dict(zip(("name", "status", "image"), ln.split("|", 2))) for ln in rows]
            secs["containers"] = {"ok": bool(r.get("ok")), "count": len(items), "items": items}
        except Exception as e:
            secs["containers"] = {"ok": False, "error": str(e)}

    if include_monitors:
        try:
            from src import kuma_client as _kumac
            ms = _kumac.monitor_status()
            mons = ms.get("monitors", [])
            # uptime-kuma heartbeat status: 1=up, 0=down, 2=pending, 3=maintenance
            up = sum(1 for m in mons if m.get("status") == 1)
            secs["monitors"] = {"ok": True, "count": len(mons), "up": up, "items": mons}
        except Exception as e:
            secs["monitors"] = {"ok": False, "error": str(e)}

    if include_channel:
        try:
            from src.vps.channel import call as _vps_call
            ch = _vps_call("GET", "/v1/status", None, None)
            secs["vps_channel"] = {"ok": ch.get("ok", ch.get("status") == 200), "raw": ch}
        except Exception as e:
            secs["vps_channel"] = {"ok": False, "error": str(e)}

    snap["healthy"] = all(sec.get("ok", True) for sec in secs.values())
    return snap


# ── Human view (self-contained, no external deps) ───────────────────────────

LOGIN_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROMION estate — login</title>
<body style="font-family:system-ui;background:#0b0e14;color:#c9d1d9;max-width:24rem;margin:5rem auto;padding:1rem">
<h2>ROMION estate</h2>
<form method="post" action="/dashboard/login">
  <p><input type="password" name="token" placeholder="dashboard token" autofocus
     style="width:100%;padding:.55rem;background:#11161f;border:1px solid #21262d;border-radius:.4rem;color:#c9d1d9"></p>
  <button style="padding:.5rem 1.1rem;background:#1f6feb;color:#fff;border:0;border-radius:.4rem">Sign in</button>
</form>
<p style="color:#7d8590;font-size:.8rem">Token-gated; sets an HttpOnly session cookie. No secret in the URL.</p>
</body>"""

# ── Session module (in-process; replaces the secret-in-URL key) ─────────────
# The dashboard token authenticates a LOGIN; thereafter a random opaque session
# id in an HttpOnly/Secure/SameSite=Strict cookie authorizes requests. In-process
# store (single long-lived server); fine for an operator-only page.
import secrets as _secrets
import time as _time

SESSION_COOKIE = "romion_dash"
SESSION_TTL = 12 * 3600
_SESSIONS: dict[str, float] = {}


def new_session() -> str:
    _prune_sessions()
    sid = _secrets.token_urlsafe(32)
    _SESSIONS[sid] = _time.time() + SESSION_TTL
    return sid


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    exp = _SESSIONS.get(sid)
    if not exp:
        return False
    if exp < _time.time():
        _SESSIONS.pop(sid, None)
        return False
    return True


def drop_session(sid: str | None) -> None:
    if sid:
        _SESSIONS.pop(sid, None)


def _prune_sessions() -> None:
    now = _time.time()
    for k in [k for k, v in _SESSIONS.items() if v < now]:
        _SESSIONS.pop(k, None)

DASHBOARD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROMION estate</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;background:#0b0e14;color:#c9d1d9;margin:0;padding:1.25rem;max-width:60rem;margin:0 auto}
  h1{font-size:1.25rem;margin:0 0 .25rem}
  .sub{color:#7d8590;font-size:.8rem;margin-bottom:1rem}
  .pill{display:inline-block;padding:.15rem .55rem;border-radius:1rem;font-size:.75rem;font-weight:600}
  .ok{background:#132d1c;color:#3fb950}.bad{background:#3d1416;color:#f85149}
  .grid{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))}
  .card{background:#11161f;border:1px solid #21262d;border-radius:.6rem;padding:.85rem}
  .card h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:#7d8590;margin:0 0 .6rem}
  .row{display:flex;justify-content:space-between;gap:.5rem;padding:.2rem 0;font-size:.85rem;border-top:1px solid #1b212b}
  .row:first-of-type{border-top:0}
  .name{color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .meta{color:#7d8590;font-size:.78rem;text-align:right;white-space:nowrap}
  .big{font-size:1.6rem;font-weight:700}
  .err{color:#f85149;font-size:.8rem}
  a{color:#58a6ff}
</style></head><body>
<h1>ROMION estate <span id="health" class="pill"></span></h1>
<div class="sub">tiny-py-mcp.romionologic.dev · refreshed <span id="ts">—</span> · auto every 15s</div>
<div id="root" class="grid"></div>
<script>
function pill(ok){return '<span class="pill '+(ok?'ok':'bad')+'">'+(ok?'OK':'FAIL')+'</span>';}
function card(title,body){return '<div class="card"><h2>'+title+'</h2>'+body+'</div>';}
function row(name,meta){return '<div class="row"><span class="name">'+name+'</span><span class="meta">'+meta+'</span></div>';}
async function load(){
  try{
    const r=await fetch('/estate.json',{cache:'no-store',credentials:'same-origin'});
    if(r.status===401){location.href='/dashboard/login';return;}
    if(!r.ok){document.getElementById('root').innerHTML='<div class="card err">/estate.json '+r.status+'</div>';return;}
    const d=await r.json();const s=d.sections||{};
    document.getElementById('health').className='pill '+(d.healthy?'ok':'bad');
    document.getElementById('health').textContent=d.healthy?'healthy':'degraded';
    document.getElementById('ts').textContent=new Date(d.generated_at).toLocaleTimeString();
    let h='';
    if(s.mcp)h+=card('MCP '+pill(s.mcp.ok),'<div class="big">'+(s.mcp.tool_count??'—')+'</div><div class="meta">tools · '+(s.mcp.endpoint||'')+'</div>');
    if(s.monitors)h+=card('Monitors '+pill(s.monitors.ok),s.monitors.error?('<div class="err">'+s.monitors.error+'</div>'):('<div class="big">'+s.monitors.up+'/'+s.monitors.count+'</div><div class="meta">up</div>'));
    if(s.vps_channel)h+=card('VPS channel '+pill(s.vps_channel.ok),'<div class="meta">'+(s.vps_channel.error||'/v1/status reachable')+'</div>');
    if(s.containers){let b=s.containers.error?('<div class="err">'+s.containers.error+'</div>'):(s.containers.items||[]).map(c=>row(c.name,c.status)).join('');h+=card('Containers ('+(s.containers.count??0)+') '+pill(s.containers.ok),b);}
    document.getElementById('root').innerHTML=h;
  }catch(e){document.getElementById('root').innerHTML='<div class="card err">'+e+'</div>';}
}
load();setInterval(load,15000);
</script></body></html>"""
