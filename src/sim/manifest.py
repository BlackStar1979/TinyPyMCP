"""
TinyPyMCP - SIM/compute job governance, stage 1 (validation + dry-run planning).

MCP as the TYPED GOVERNANCE INTERFACE, never the compute engine (compute-plane
ADR). Pure, stateless, dependency-free: validate a job manifest against the v1
schema and produce a dry-run plan. NOTHING is persisted or executed here — the
heavy ROMIONCORE/SIM/engine compute belongs to a separate future plane.

Spec: C:\\Work\\www\\sim-job-governance-spec.md (+ the compute-plane ADR).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

EXPERIMENT_TYPES = {"fusion", "phc", "sim", "analysis"}
RESOURCE_PROFILES = {"cpu-small", "cpu-large", "gpu-small", "gpu-large", "npu"}
_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
_REQUIRED = ["job_id", "project_id", "experiment_type", "engine_version",
             "parameter_set", "resource_profile", "created_at"]
_ALLOWED = set(_REQUIRED) | {"input_artifact_refs", "expected_outputs", "validation_policy"}


def validate(manifest: Any) -> dict[str, Any]:
    """Validate a job manifest against romion.sim.job_manifest.v1. Pure; no I/O."""
    if not isinstance(manifest, dict):
        return {"ok": False, "errors": ["manifest must be a JSON object"]}
    errors: list[str] = []
    for k in _REQUIRED:
        if k not in manifest:
            errors.append(f"missing required field: {k}")
    for k in manifest:
        if k not in _ALLOWED:
            errors.append(f"unknown field: {k}")
    jid = manifest.get("job_id")
    if jid is not None and (not isinstance(jid, str) or not _JOB_ID_RE.match(jid)):
        errors.append("job_id must match ^[a-z0-9][a-z0-9-]{7,63}$")
    et = manifest.get("experiment_type")
    if et is not None and et not in EXPERIMENT_TYPES:
        errors.append(f"experiment_type must be one of {sorted(EXPERIMENT_TYPES)}")
    rp = manifest.get("resource_profile")
    if rp is not None and rp not in RESOURCE_PROFILES:
        errors.append(f"resource_profile must be one of {sorted(RESOURCE_PROFILES)}")
    if "parameter_set" in manifest and not isinstance(manifest["parameter_set"], dict):
        errors.append("parameter_set must be an object")
    for listf in ("input_artifact_refs", "expected_outputs"):
        if listf in manifest:
            v = manifest[listf]
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                errors.append(f"{listf} must be a list of strings")
    for sf in ("project_id", "engine_version", "validation_policy"):
        v = manifest.get(sf)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            errors.append(f"{sf} must be a non-empty string")
    ca = manifest.get("created_at")
    if ca is not None:
        try:
            datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
        except ValueError:
            errors.append("created_at must be ISO-8601")
    return {"ok": not errors, "errors": errors}


def plan_dry_run(manifest: Any) -> dict[str, Any]:
    """Validate + produce a would-be execution plan. Persists/executes NOTHING."""
    v = validate(manifest)
    if not v["ok"]:
        return {"ok": False, "errors": v["errors"], "note": "manifest invalid; no plan produced"}
    rp = manifest["resource_profile"]
    heavy = rp.startswith("gpu") or rp == "npu"
    return {
        "ok": True,
        "dry_run": True,
        "plan": {
            "job_id": manifest["job_id"],
            "experiment_type": manifest["experiment_type"],
            "engine_version": manifest["engine_version"],
            "resource_profile": rp,
            "target_compute_plane": (
                "separate future GPU/NPU compute plane (host deferred per ADR)"
                if heavy else
                "small CPU job — control-plane may host ONLY trivial cpu-small work"
            ),
            "inputs": manifest.get("input_artifact_refs", []),
            "expected_outputs": manifest.get("expected_outputs", []),
            "would_persist": False,
            "would_execute": False,
            "blocked_until_real_submit_layer": [
                "job registry", "validation rules", "audit trail",
                "artifact handling", "resource limits", "human approval", "rollback plan",
            ],
        },
        "note": "dry-run only — nothing persisted or executed (compute-plane ADR, stage 1)",
    }


def catalog() -> dict[str, Any]:
    """Static catalog of what the governance layer currently understands."""
    return {
        "manifest_schema_id": "romion.sim.job_manifest.v1",
        "experiment_types": sorted(EXPERIMENT_TYPES),
        "resource_profiles": sorted(RESOURCE_PROFILES),
        "stage": "1 (read-only / dry-run; no submit/exec)",
        "not_yet": ["sim_submit_job", "sim_cancel_job", "sim_archive_job", "any run/exec tool"],
    }
