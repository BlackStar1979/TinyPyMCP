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


import time as _t

# Small TTL cache so the 15s dashboard refresh does not hammer slow/rate-limited
# upstreams (Cloudflare API). key -> (expiry_epoch, value).
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn) -> Any:
    now = _t.time()
    rec = _CACHE.get(key)
    if rec and rec[0] > now:
        return rec[1]
    val = fn()
    _CACHE[key] = (now + ttl, val)
    return val


async def collect_estate(
    mcp: Any,
    *,
    include_containers: bool = True,
    include_monitors: bool = True,
    include_channel: bool = True,
    include_cloudflare: bool = True,
    include_r2: bool = True,
    include_ovh: bool = True,
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

    if include_cloudflare:
        try:
            from src.cf import client as _cf

            def _fetch_cf():
                dns = _cf.list_dns()
                tuns = _cf.list_tunnels()
                tlist = tuns.get("result") if isinstance(tuns.get("result"), list) else []
                ingress = {}
                for t in tlist:
                    tid = t.get("id")
                    if not tid:
                        continue
                    cfg = _cf.get_tunnel_config(tid)
                    rules = (((cfg.get("result") or {}).get("config") or {}).get("ingress")) or []
                    ingress[tid] = [r.get("hostname") for r in rules if r.get("hostname")]
                return {"dns": dns, "tunnels": tuns, "ingress": ingress}

            cf = _cached("cf", 120, _fetch_cf)
            dns, tuns, ingress = cf["dns"], cf["tunnels"], cf.get("ingress", {})
            dns_items = dns.get("result") if isinstance(dns.get("result"), list) else []
            tun_items = tuns.get("result") if isinstance(tuns.get("result"), list) else []
            secs["cloudflare"] = {
                "ok": bool(dns.get("ok", True)) and bool(tuns.get("ok", True)),
                "dns_count": len(dns_items),
                "tunnels": [{"name": t.get("name"), "status": t.get("status"),
                             "ingress": ingress.get(t.get("id"), [])} for t in tun_items],
                "cache_ttl_s": 120,
            }
        except Exception as e:
            secs["cloudflare"] = {"ok": False, "error": str(e)}

    if include_r2:
        try:
            from src.cf import r2 as _r2
            bf = dict(_cached("r2_backup", 300, _r2.backup_freshness))
            # surface staleness in the health rollup while keeping the detail
            if bf.get("ok") and bf.get("stale"):
                bf["ok"] = False
            secs["r2_backup"] = bf
        except Exception as e:
            secs["r2_backup"] = {"ok": False, "error": str(e)}

    if include_ovh:
        try:
            import os
            from src import ovh_client as _ovh
            name = os.environ.get("MCP_OVH_VPS_NAME", "vps-2f267042.vps.ovh.net")
            res = _cached("ovh_host", 300, lambda: _ovh.vps_info(service_name=name, config_ref="/secrets/ovh-estate.json"))
            if res.get("ok") is False:
                secs["ovh_host"] = {"ok": False, "error": res.get("error") or res.get("message")}
            else:
                d = res.get("result") or {}
                m = d.get("model") or {}
                secs["ovh_host"] = {
                    "ok": d.get("state") == "running",
                    "state": d.get("state"),
                    "offer": m.get("offer"),
                    "vcore": d.get("vcore"),
                    "memory_mb": d.get("memoryLimit"),
                    "disk_gb": m.get("disk"),
                    "zone": d.get("zone"),
                    "netboot": d.get("netbootMode"),
                }
        except Exception as e:
            secs["ovh_host"] = {"ok": False, "error": str(e)}

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
# sid -> (expiry_epoch, auth_method). method is shown as "logged in via …" and
# will extend to oauth/account when those login paths land.
_SESSIONS: dict[str, tuple[float, str]] = {}


def new_session(method: str = "token") -> str:
    _prune_sessions()
    sid = _secrets.token_urlsafe(32)
    _SESSIONS[sid] = (_time.time() + SESSION_TTL, method)
    return sid


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    rec = _SESSIONS.get(sid)
    if not rec:
        return False
    if rec[0] < _time.time():
        _SESSIONS.pop(sid, None)
        return False
    return True


def session_method(sid: str | None) -> str | None:
    rec = _SESSIONS.get(sid) if sid else None
    return rec[1] if rec else None


def drop_session(sid: str | None) -> None:
    if sid:
        _SESSIONS.pop(sid, None)


def _prune_sessions() -> None:
    now = _time.time()
    for k in [k for k, (exp, _m) in _SESSIONS.items() if exp < now]:
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
<h1>ROMION estate <span id="health" class="pill"></span>
  <form method="post" action="/dashboard/logout" style="display:inline;float:right">
    <button style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:.4rem;padding:.3rem .75rem;cursor:pointer">Logout</button>
  </form>
</h1>
<div class="sub">tiny-py-mcp.romionologic.dev · logged in via <b>__VIA__</b> · refreshed <span id="ts">—</span> · auto 15s</div>
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
    if(s.cloudflare){let b=s.cloudflare.error?('<div class="err">'+s.cloudflare.error+'</div>'):(row('DNS records',s.cloudflare.dns_count)+(s.cloudflare.tunnels||[]).map(t=>row('tunnel '+t.name,t.status+' · '+((t.ingress||[]).length)+' routes')).join(''));h+=card('Cloudflare '+pill(s.cloudflare.ok),b);}
    if(s.r2_backup){let r=s.r2_backup;let b=r.error?('<div class="err">'+r.error+'</div>'):(row('newest',r.age_hours!=null?(r.age_hours+'h ago'):'—')+row('objects',r.object_count)+row('size',(r.total_bytes/1048576).toFixed(1)+' MB')+(r.stale?'<div class="err">STALE (&gt;30h)</div>':''));h+=card('R2 backup '+pill(r.ok),b);}
    if(s.ovh_host){let o=s.ovh_host;let b=o.error?('<div class="err">'+o.error+'</div>'):(row('state',o.state)+row('offer',o.offer)+row('cpu/mem',o.vcore+' vCore / '+(o.memory_mb/1024)+' GB')+row('disk',o.disk_gb+' GB')+row('zone',o.zone));h+=card('OVH host '+pill(o.ok),b);}
    document.getElementById('root').innerHTML=h;
  }catch(e){document.getElementById('root').innerHTML='<div class="card err">'+e+'</div>';}
}
load();setInterval(load,15000);
</script></body></html>"""
