"""
TinyPyMCP - tool profiles (bounded role surfaces).

Each profile is a set of tool names. The active server surface is the UNION of
the selected profiles, applied by pruning the registered tools at create_server
time. Profiles let a launch expose only the capability tier it needs:

- read_only      : inspection/search/read of FS + index + memory + CF/VPS status.
- operator_admin : local mutations, exec (run_command), memory writes, vps_request.
- cloud_admin    : Cloudflare (and, later, OVH host) admin mutations.

IMPORTANT: a tool's confirm=true is an accidental-mutation guard, NOT
authorization. The authorization boundary is WHICH PROFILES are enabled at
launch (and, for cloud_admin, the scoped CF/OVH credentials behind them).
"""

from __future__ import annotations

READ_ONLY = {
    "find_phrase_occurrences", "read_file", "read_file_chunk", "list_files",
    "search_codebase", "get_project_structure", "get_info", "list_trash",
    "code_dependencies", "code_impact", "code_symbols", "search_index", "index_status",
    "http_probe", "check_npm_package", "check_pypi_package",
    "memory_get_state", "memory_search", "memory_get_tasks",
    "vps_status", "cf_verify_token", "cf_list_dns", "cf_list_tunnels", "cf_get_tunnel_config",
    "ovh_vps_info", "ovh_snapshot_status", "ovh_automated_backup_status", "ovh_images_available",
    "kuma_list_monitors", "kuma_monitor_status",
    "gh_repo_info", "gh_list_prs", "gh_get_pr",
    "ovh_ai_embeddings", "ovh_ai_chat",
    "vps_fs_list", "vps_fs_stat", "vps_fs_read",
    "sim_validate_job_manifest", "sim_submit_job_dry_run", "sim_experiment_catalog",
    "check_cve", "check_github_advisory",
}

OPERATOR_ADMIN = {
    "safe_replace_in_line", "edit_code_block", "write_file", "create_file", "append_file",
    "copy_path", "move_path", "delete_path", "edit_file_patch", "restore_path",
    "build_index", "run_command", "clone_repo",
    "memory_set_state", "memory_save", "memory_create_task", "memory_reindex",
    "vps_request", "vps_docker",
}

CLOUD_ADMIN = {
    "cf_create_dns_record", "cf_delete_dns_record", "cf_add_tunnel_route", "cf_remove_tunnel_route",
    "cf_create_access_app", "cf_delete_access_app", "cf_add_access_service_policy",
    "cf_create_service_token", "cf_delete_service_token",
    "ovh_create_snapshot", "ovh_revert_snapshot", "ovh_abort_snapshot",
    "ovh_automated_backup_restore", "ovh_reboot",
    "kuma_add_monitor", "kuma_pause_monitor", "kuma_resume_monitor",
    "gh_create_pr", "gh_merge_pr",
}

PROFILES = {"read_only": READ_ONLY, "operator_admin": OPERATOR_ADMIN, "cloud_admin": CLOUD_ADMIN}
ALL_PROFILES = ("read_only", "operator_admin", "cloud_admin")


def resolve_profile_names(value: str | None) -> list[str]:
    """Parse a comma/space-separated profile list into ordered unique names.

    None / empty / 'all' -> every profile. Raises ValueError on unknown names.
    """
    if value is None:
        return list(ALL_PROFILES)
    raw = [p.strip().lower() for p in str(value).replace(",", " ").split() if p.strip()]
    if not raw or "all" in raw:
        return list(ALL_PROFILES)
    out: list[str] = []
    for p in raw:
        if p not in PROFILES:
            raise ValueError(f"unknown profile: {p} (valid: {', '.join(ALL_PROFILES)}, all)")
        if p not in out:
            out.append(p)
    return out


def tools_for_profiles(names) -> set[str]:
    """Union of tool names across the given profile names."""
    active: set[str] = set()
    for n in names:
        active |= PROFILES[n]
    return active
