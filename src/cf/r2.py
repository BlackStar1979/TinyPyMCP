"""
TinyPyMCP - Cloudflare R2 backup-freshness probe (S3 API, AWS SigV4).

Read-only. Lists the offsite restic backup bucket via the R2 S3 endpoint and
reports the newest object's age, object count and total size — the dashboard's
"are the offsite backups current?" signal. Credentials (R2 access key/secret/
endpoint/bucket) come from cf-estate.json on disk, never arguments, never logged.

Config: MCP_CF_ESTATE_CONFIG env, else /secrets/cf-estate.json. The "r2" block:
{ "r2": {"access_key_id","secret_access_key","endpoint","bucket"} }.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_CONFIG = Path(os.environ.get("MCP_CF_ESTATE_CONFIG", "/secrets/cf-estate.json"))
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_MAX_PAGES = 30  # bound the listing (30k objects) so a huge bucket can't hang us


def _r2_config(config_ref: str | None) -> dict[str, Any]:
    path = Path(config_ref) if config_ref else _DEFAULT_CONFIG
    raw = json.loads(path.read_text(encoding="utf-8"))
    r2 = raw.get("r2") or {}
    for k in ("access_key_id", "secret_access_key", "endpoint", "bucket"):
        if not r2.get(k):
            raise ValueError(f"cf-estate.json r2 block missing '{k}'")
    return r2


def _sigv4_get(ak: str, sk: str, endpoint: str, uri: str, query: str) -> httpx.Response:
    host = endpoint.split("://", 1)[1]
    now = _dt.datetime.now(_dt.timezone.utc)
    amz, datestamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canon_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canon_req = f"GET\n{uri}\n{query}\n{canon_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{datestamp}/auto/s3/aws4_request"
    sts = "AWS4-HMAC-SHA256\n" + amz + "\n" + scope + "\n" + hashlib.sha256(canon_req.encode()).hexdigest()
    _h = lambda k, m: hmac.new(k, m.encode(), hashlib.sha256).digest()
    key = _h(_h(_h(_h(("AWS4" + sk).encode(), datestamp), "auto"), "s3"), "aws4_request")
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed_headers}, Signature={sig}"
    return httpx.get(
        f"{endpoint}{uri}?{query}",
        headers={"Authorization": auth, "x-amz-date": amz, "x-amz-content-sha256": payload_hash},
        timeout=20,
    )


def backup_freshness(config_ref: str | None = None) -> dict[str, Any]:
    """List the R2 backup bucket and report freshness. Paginated (capped)."""
    r2 = _r2_config(config_ref)
    ak, sk = r2["access_key_id"], r2["secret_access_key"]
    endpoint, bucket = r2["endpoint"].rstrip("/"), r2["bucket"]
    uri = "/" + bucket

    count, total_bytes, newest = 0, 0, None
    token, pages, truncated_capped = None, 0, False
    while True:
        query = "list-type=2"
        if token:
            from urllib.parse import quote
            query += "&continuation-token=" + quote(token, safe="")
        r = _sigv4_get(ak, sk, endpoint, uri, query)
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "bucket": bucket, "error": r.text[:200]}
        root = ET.fromstring(r.text)
        for o in root.findall(_S3_NS + "Contents"):
            count += 1
            total_bytes += int(o.findtext(_S3_NS + "Size") or 0)
            lm = o.findtext(_S3_NS + "LastModified")
            if lm and (newest is None or lm > newest):
                newest = lm
        pages += 1
        if root.findtext(_S3_NS + "IsTruncated") == "true" and pages < _MAX_PAGES:
            token = root.findtext(_S3_NS + "NextContinuationToken")
            continue
        if root.findtext(_S3_NS + "IsTruncated") == "true":
            truncated_capped = True
        break

    age_hours = None
    if newest:
        try:
            dt = _dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
            age_hours = round((_dt.datetime.now(_dt.timezone.utc) - dt).total_seconds() / 3600, 1)
        except ValueError:
            pass
    return {
        "ok": True,
        "bucket": bucket,
        "object_count": count,
        "total_bytes": total_bytes,
        "newest_modified": newest,
        "age_hours": age_hours,
        "stale": (age_hours is not None and age_hours > 30),  # daily backup + margin
        "truncated_capped": truncated_capped,
    }
