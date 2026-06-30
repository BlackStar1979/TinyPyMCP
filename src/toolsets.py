"""
TinyPyMCP - dynamic toolsets (hotplug). The server registers ALL tools, then
exposes only CORE + the meta-tools by default and lets the agent ROTATE groups
in/out at runtime (enable_toolset/disable_toolset), emitting
notifications/tools/list-changed so the connector refreshes live. This keeps the
advertised surface small instead of a fixed 96-tool 'workshop' (operator D9).

GROUPS are functional (by capability), orthogonal to the role PROFILES (which
remain the auth boundary — you can only enable tools the active profiles allow,
because pruning happens within the already-profile-filtered registry).
"""

from __future__ import annotations

# Always exposed: file ops + memory + exec + the toolset meta-tools.
CORE = {
    # filesystem
    "find_phrase_occurrences", "read_file", "read_file_chunk", "list_files", "list_trash",
    "search_codebase", "get_project_structure", "get_info",
    "safe_replace_in_line", "edit_code_block", "write_file", "create_file", "append_file",
    "copy_path", "move_path", "delete_path", "edit_file_patch", "restore_path",
    # memory
    "memory_get_state", "memory_set_state", "memory_search", "memory_save", "memory_save_adr",
    "memory_list_adrs", "memory_create_task", "memory_get_tasks", "memory_reindex",
    # exec
    "run_command", "clone_repo",
    # toolset meta-tools (must always be present so the agent can rotate)
    "list_toolsets", "enable_toolset", "disable_toolset",
}

# Enable-on-demand groups.
GROUPS = {
    "code": {"build_index", "index_status", "search_index", "code_dependencies", "code_impact",
             "code_symbols"},
    "net": {"http_probe", "check_npm_package", "check_pypi_package", "check_cve",
            "check_github_advisory", "fetch_docs", "download_docs"},
    "estate": {"estate_status"},
    "vps": {"vps_status", "vps_request", "vps_docker", "vps_fs_list", "vps_fs_read", "vps_fs_stat"},
    "cf": {"cf_verify_token", "cf_list_dns", "cf_list_tunnels", "cf_get_tunnel_config",
           "cf_create_dns_record", "cf_delete_dns_record", "cf_add_tunnel_route", "cf_remove_tunnel_route",
           "cf_create_access_app", "cf_delete_access_app", "cf_add_access_service_policy",
           "cf_create_service_token", "cf_delete_service_token"},
    "ovh": {"ovh_vps_info", "ovh_snapshot_status", "ovh_automated_backup_status", "ovh_images_available",
            "ovh_create_snapshot", "ovh_revert_snapshot", "ovh_abort_snapshot",
            "ovh_automated_backup_restore", "ovh_reboot", "ovh_ai_chat", "ovh_ai_embeddings"},
    "kuma": {"kuma_list_monitors", "kuma_monitor_status", "kuma_add_monitor",
             "kuma_pause_monitor", "kuma_resume_monitor"},
    "gh": {"gh_repo_info", "gh_list_prs", "gh_get_pr", "gh_create_pr", "gh_merge_pr"},
    "sim": {"sim_validate_job_manifest", "sim_submit_job_dry_run", "sim_experiment_catalog",
            "sim_submit_job", "sim_job_status", "sim_list_jobs",
            "sim_register_artifact", "sim_list_artifacts", "sim_fetch_artifact_summary"},
}

GROUP_NAMES = tuple(GROUPS.keys())
