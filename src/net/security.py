"""
TinyPyMCP - research/security plane (read-only): CVE + GitHub advisory lookup.

check_cve  -> OSV.dev (free, no key) for a package/ecosystem (+ optional version).
check_github_advisory -> GitHub global advisory DB (via the gh token).

Both SLIM the upstream payload hard (OSV returns huge `versions[]` arrays; we drop
them and keep id/CVE/severity/CWE/summary/fixed/refs). httpx lazy-imported.
"""

from __future__ import annotations

from typing import Any

_OSV_QUERY = "https://api.osv.dev/v1/query"


def _slim_vuln(v: dict) -> dict[str, Any]:
    fixed: list[str] = []
    for a in v.get("affected") or []:
        for r in a.get("ranges") or []:
            for e in r.get("events") or []:
                if e.get("fixed"):
                    fixed.append(e["fixed"])
    dbs = v.get("database_specific") or {}
    sev = dbs.get("severity")
    if not sev:
        sl = v.get("severity") or []
        sev = sl[0].get("score") if sl else None
    cves = [x for x in (v.get("aliases") or []) if str(x).startswith("CVE-")]
    return {
        "id": v.get("id"),
        "cve": cves,
        "severity": sev,
        "cwe": dbs.get("cwe_ids"),
        "summary": v.get("summary") or (v.get("details") or "")[:240],
        "fixed": sorted(set(fixed)),
        "published": v.get("published"),
    }


def check_cve(package: str, ecosystem: str = "PyPI", version: str | None = None,
              limit: int = 25) -> dict[str, Any]:
    """Query OSV.dev for known vulnerabilities of a package (optionally a version)."""
    try:
        import httpx  # lazy
    except ImportError:
        return {"ok": False, "error": "httpx not installed"}
    body: dict[str, Any] = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        body["version"] = version
    try:
        with httpx.Client(timeout=20) as c:
            resp = c.post(_OSV_QUERY, json=body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if resp.status_code >= 400:
        return {"ok": False, "status": resp.status_code, "error": resp.text[:240]}
    vulns = (resp.json() or {}).get("vulns") or []
    return {
        "ok": True, "package": package, "ecosystem": ecosystem, "version": version,
        "count": len(vulns), "vulns": [_slim_vuln(v) for v in vulns[: max(1, int(limit))]],
        "source": "osv.dev",
    }


def check_github_advisory(cve_id: str | None = None, ghsa_id: str | None = None,
                          affects: str | None = None, ecosystem: str | None = None,
                          severity: str | None = None, limit: int = 25,
                          config_ref: str | None = None) -> dict[str, Any]:
    """Query the GitHub global advisory DB (uses the gh token for rate limit)."""
    from src import github_client as ghc
    params: dict[str, Any] = {"per_page": max(1, min(int(limit), 100))}
    for k, val in (("cve_id", cve_id), ("ghsa_id", ghsa_id), ("affects", affects),
                   ("ecosystem", ecosystem), ("severity", severity)):
        if val:
            params[k] = val
    try:
        cfg = ghc._config(config_ref)
    except ghc.GitHubConfigError as e:
        return {"ok": False, "error": str(e)}
    data = ghc._request("GET", "/advisories", cfg, params=params)
    if isinstance(data, dict) and data.get("ok") is False:
        return data
    rows = data if isinstance(data, list) else []
    out = [{
        "ghsa_id": a.get("ghsa_id"), "cve_id": a.get("cve_id"),
        "severity": a.get("severity"), "summary": a.get("summary"),
        "url": a.get("html_url"), "published": a.get("published_at"),
    } for a in rows]
    return {"ok": True, "count": len(out), "advisories": out[: max(1, int(limit))], "source": "github-advisories"}
