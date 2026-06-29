"""
TinyPyMCP Server

Dwa tryby:
- stdio (domyślny) – do Claude Desktop, Cursor itp.
- sse            – do grok.com/connectors przez Cloudflare Tunnel

Uruchomienie w trybie SSE (port 8765, ścieżka /mcp):
    python -m src.server --transport sse --port 8765
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field


class _EngineVersionPrompt(BaseModel):
    """Elicitation schema (primitive fields only, per spec) for a missing
    required manifest field."""
    engine_version: str

from src.utils.file_ops import (
    append_file as append_file_op,
    copy_path as copy_path_op,
    create_file as create_file_op,
    delete_path as delete_path_op,
    edit_file_patch as edit_file_patch_op,
    find_occurrences,
    get_info as get_info_op,
    list_trash as list_trash_op,
    read_file_chunk as read_file_chunk_op,
    restore_path as restore_path_op,
    list_files as list_files_op,
    move_path as move_path_op,
    project_tree,
    safe_replace,
    search_codebase as search_codebase_op,
    edit_code_block as edit_code_block_op,
    write_file_content,
)
from src.utils.path_guard import ensure_within
from src.memory import store as mem
from src.exec.runner import (
    ALLOWED_PROGRAMS,
    clone_repo_async as clone_repo_op,
    run_command_async as run_command_op,
)
from src.net.fetch import check_npm_package as check_npm_op, check_pypi_package as check_pypi_op, http_probe as http_probe_op
from src.code.deps import analyze_impact, build_dependency_graph, summarize_graph
from src.code.search_index import build_index as build_index_op, index_status as index_status_op, search_index as search_index_op
from src.code.symbols import extract_symbols as extract_symbols_op
from src.vps.channel import call as vps_call_op, call_async as vps_call_async
from src.cf import client as cf
from src.utils.audit import audit


def create_server(stateless: bool = True, port: int = 8765, auth_mode: str = "bearer",
                  issuer_url: str | None = None, operator_secret: str | None = None,
                  profiles: "list[str] | None" = None) -> FastMCP:
    # Public hosts/origins served behind the Cloudflare Tunnel. Local entries
    # are derived from the bound port (no longer hardcoded to 8765).
    public_host = os.environ.get("MCP_PUBLIC_HOST", "tiny-py-mcp.romionologic.dev")
    security = TransportSecuritySettings(
        allowed_hosts=[public_host, "127.0.0.1", f"127.0.0.1:{port}", "localhost", f"localhost:{port}"],
        allowed_origins=[f"https://{public_host}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
    )

    # OAuth mode: self-contained authorization server (FastMCP mounts the OAuth
    # routes + protects the endpoint). Bearer mode leaves these empty.
    auth_kwargs: dict = {}
    _oauth_provider = None
    if auth_mode == "oauth":
        from src.oauth.provider import RomionOAuthProvider
        from src.oauth.app import oauth_auth_settings
        issuer_url = issuer_url or f"https://{public_host}"
        _oauth_provider = RomionOAuthProvider(issuer_url, operator_secret or "")
        auth_kwargs = {"auth_server_provider": _oauth_provider, "auth": oauth_auth_settings(issuer_url)}

    mcp = FastMCP(
        name="TinyPyMCP",
        instructions=(
            "TinyPyMCP: precise file editor + project search + persistent memory.\n\n"
            "FILE SCOPE: all paths must be ABSOLUTE Windows paths. Reads are allowed "
            "anywhere under C:\\Work; writes/creates are allowed anywhere under C:\\Work "
            "too, but NOT outside it (a PermissionError is returned otherwise).\n\n"
            "EDITING WORKFLOW: to change existing text, call find_phrase_occurrences "
            "first to locate the exact line and occurrence, then safe_replace_in_line "
            "with that line_num/occurrence (use dry_run=true to preview). Use write_file "
            "only to replace whole-file content; create_file for brand-new files.\n\n"
            "SEARCH: search_codebase scans the whole tree for a phrase; "
            "find_phrase_occurrences targets one known file.\n\n"
            "MEMORY: memory_save stores a fact; memory_search retrieves the most "
            "relevant facts (call it before assuming you forgot something). Use "
            "memory_set_state/get_state for your current session/task, and "
            "memory_create_task/get_tasks for a shared to-do list."
        ),
        transport_security=security,
        stateless_http=stateless,
        **auth_kwargs,
    )

    if hasattr(mcp, "settings"):
        mcp.settings.streamable_http_path = "/mcp/v5"
        try:
            mcp.settings.json_response = False
            mcp.settings.log_level = "info"
        except Exception:
            pass

    # Roadmap (aligned to the sessionless MCP direction — SEP-2567/2575, RC 2026-07-28):
    # the transport is already STATELESS (stateless_http=True, no Mcp-Session-Id), and
    # cross-call state is carried by the memory layer as explicit application-level
    # handles (memory_set_state's session_id) — exactly the explicit-state-handle
    # pattern that replaces transport sessions. So we deliberately do NOT build an
    # "advanced HTTP session lifecycle"; that is the thing the spec is removing.
    #
    # Open items, in priority order:
    # - Active cancellation for long tools (run_command/clone_repo/build_index/
    #   vps_request): honor client disconnect -> propagate cancel; keep timeout as the
    #   safety net. (Mirrors the mcp-tests D6 decision.)
    # - Optional Sampling handler (sampling/createMessage) + progress notifications,
    #   only if/when a client needs them. SSE transport is legacy/deprecated; prod runs
    #   streamable-http.

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Find phrase occurrences",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def find_phrase_occurrences(
        file_path: Annotated[str, Field(description="Absolute path to the file to search (must be inside C:\\Work).")],
        phrase: Annotated[str, Field(description="Text to find. Treated as a regex when use_regex=true.", min_length=1)],
        context_lines: Annotated[int, Field(description="How many lines of context to return before and after each match.", ge=0)] = 0,
        use_regex: Annotated[bool, Field(description="Interpret 'phrase' as a regular expression instead of literal text.")] = False,
        encoding: Annotated[str, Field(description="Text encoding to read the file with.")] = "utf-8",
    ) -> list[dict[str, Any]]:
        """Locate every occurrence of a phrase in one file. Returns line number,
        occurrence-within-line, column span and the full line for each hit. Use
        this BEFORE safe_replace_in_line to get the exact line_num/occurrence."""
        results = find_occurrences(
            file_path=Path(file_path),
            phrase=phrase,
            context_lines=context_lines,
            use_regex=use_regex,
            encoding=encoding,
        )
        return [dict(r) for r in results]

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Replace one occurrence in a line",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def safe_replace_in_line(
        file_path: Annotated[str, Field(description="Absolute path to the file to edit (must be inside C:\\Work).")],
        old_phrase: Annotated[str, Field(description="Existing text to replace, on the target line.", min_length=1)],
        new_phrase: Annotated[str, Field(description="Replacement text.")],
        line_num: Annotated[int, Field(description="1-based line number to edit (from find_phrase_occurrences).", ge=1)],
        occurrence: Annotated[int, Field(description="Which occurrence on that line to replace, 1-based.", ge=1)] = 1,
        dry_run: Annotated[bool, Field(description="Preview the diff without writing. Recommended first.")] = False,
        use_regex: Annotated[bool, Field(description="Interpret 'old_phrase' as a regular expression.")] = False,
        backup_suffix: Annotated[str, Field(description="Suffix for the backup copy made before writing.")] = ".bak",
        encoding: Annotated[str, Field(description="Text encoding of the file.")] = "utf-8",
    ) -> dict[str, Any]:
        """Replace exactly ONE occurrence on ONE line — never touches other lines
        or occurrences. Backs up the file to <name><backup_suffix> first. Run with
        dry_run=true to inspect the diff before committing."""
        return safe_replace(
            file_path=Path(file_path),
            old_phrase=old_phrase,
            new_phrase=new_phrase,
            line_num=line_num,
            occurrence=occurrence,
            dry_run=dry_run,
            use_regex=use_regex,
            backup_suffix=backup_suffix,
            encoding=encoding,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Edit code block",
            readOnlyHint=False,
            idempotentHint=False,
            destructiveHint=True,
            openWorldHint=False,
        )
    )
    def edit_code_block(
        file_path: Annotated[str, Field(description="Absolute path to the file to edit (must be inside C:\\Work).")],
        old_block: Annotated[str, Field(description="Exact block of code that must occur exactly once.", min_length=1)],
        new_block: Annotated[str, Field(description="New block of code to replace the old one with.")],
        dry_run: Annotated[bool, Field(description="Preview the diff without writing. Recommended first.")] = False,
        backup_suffix: Annotated[str, Field(description="Suffix for the backup copy made before writing.")] = ".bak",
        encoding: Annotated[str, Field(description="Text encoding of the file.")] = "utf-8",
    ) -> dict[str, Any]:
        """Replace a block of code that occurs exactly once in the file.

        Safer than raw string replace for larger, multi-line edits.
        Validates that the old block exists exactly once before replacing.
        """
        return edit_code_block_op(
            file_path=Path(file_path),
            old_block=old_block,
            new_block=new_block,
            dry_run=dry_run,
            backup_suffix=backup_suffix,
            encoding=encoding,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Read file (line range)",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def read_file(
        file_path: Annotated[str, Field(description="Absolute path to the file to read (must be inside C:\\Work).")],
        start_line: Annotated[int | None, Field(description="1-based first line to return. Omit to start at the top.")] = None,
        end_line: Annotated[int | None, Field(description="1-based last line to return (inclusive). Omit to read to EOF.")] = None,
        context_lines: Annotated[int, Field(description="Extra lines to include before start_line and after end_line.", ge=0)] = 0,
        encoding: Annotated[str, Field(description="Text encoding to read the file with.")] = "utf-8",
    ) -> dict[str, Any]:
        """Read a file, optionally limited to a line range with surrounding
        context. Returns the lines with their 1-based line numbers and the total
        line count."""
        path = ensure_within(file_path)
        if not path.exists():
            return {"error": f"Plik nie istnieje: {file_path}"}

        try:
            with open(path, "r", encoding=encoding) as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": f"Nie można odczytać pliku: {e}"}

        total_lines = len(lines)

        # Domyślnie cały plik
        start = 0 if start_line is None else max(0, start_line - 1)
        end = total_lines if end_line is None else min(total_lines, end_line)

        # Dodaj kontekst
        if context_lines > 0:
            start = max(0, start - context_lines)
            end = min(total_lines, end + context_lines)

        selected_lines = []
        for i in range(start, end):
            selected_lines.append({
                "line_num": i + 1,
                "content": lines[i].rstrip("\n\r")
            })

        return {
            "file_path": str(path),
            "total_lines": total_lines,
            "start_line": start + 1,
            "end_line": end,
            "lines": selected_lines
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List files",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def list_files(
        dir_path: Annotated[str, Field(description="Absolute directory to list (must be inside C:\\Work).")],
        pattern: Annotated[str, Field(description="Glob filter, e.g. '*.py' or 'test_*'. Default '*' = everything.")] = "*",
        recursive: Annotated[bool, Field(description="Recurse into subdirectories.")] = False,
        include_dirs: Annotated[bool, Field(description="Include directories in the result, not just files.")] = True,
    ) -> dict[str, Any]:
        """List files and directories under a path, glob-filtered. Skips noise
        dirs (.git, __pycache__, node_modules, .venv, …). Each entry has its
        absolute path, path relative to dir_path, type and size."""
        return list_files_op(dir_path, pattern, recursive, include_dirs)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search codebase",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def search_codebase(
        root_path: Annotated[str, Field(description="Absolute root directory to search recursively (must be inside C:\\Work).")],
        phrase: Annotated[str, Field(description="Text to search for. Treated as a regex when use_regex=true.", min_length=1)],
        use_regex: Annotated[bool, Field(description="Interpret 'phrase' as a regular expression.")] = False,
        glob: Annotated[str, Field(description="Only scan files matching this glob, e.g. '*.py'. Default '*' = all text files.")] = "*",
        case_sensitive: Annotated[bool, Field(description="Match case exactly.")] = True,
        max_results: Annotated[int, Field(description="Stop after this many matches; result flags 'truncated' if hit.", ge=1, le=5000)] = 500,
        encoding: Annotated[str, Field(description="Text encoding used to read files.")] = "utf-8",
    ) -> dict[str, Any]:
        """Recursively grep the project for a phrase across many files. Skips
        noise dirs and binary/unreadable files. Returns file path, line number
        and the matching line for each hit. Use find_phrase_occurrences instead
        when you already know the single file."""
        return search_codebase_op(
            root_path, phrase, use_regex, glob, case_sensitive, max_results, encoding
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Write file (whole content)",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def write_file(
        file_path: Annotated[str, Field(description="Absolute path to write. Must be inside C:\\Work (writes elsewhere are rejected).")],
        content: Annotated[str, Field(description="Full new file content. Replaces the file entirely.")],
        overwrite: Annotated[bool, Field(description="Allow overwriting an existing file. If false, an existing file causes an error.")] = True,
        backup_suffix: Annotated[str, Field(description="Suffix for the backup of the previous content (only made if the file existed).")] = ".bak",
        encoding: Annotated[str, Field(description="Text encoding to write with.")] = "utf-8",
    ) -> dict[str, Any]:
        """Write whole-file content atomically (temp file + replace). If the file
        existed it is backed up to <name><backup_suffix> first. For small edits to
        existing files prefer safe_replace_in_line instead of rewriting everything."""
        return write_file_content(
            file_path, content, overwrite, backup_suffix, encoding
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Create new file",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False,
        )
    )
    def create_file(
        file_path: Annotated[str, Field(description="Absolute path for the new file. Must be inside C:\\Work.")],
        content: Annotated[str, Field(description="Initial file content. Defaults to empty.")] = "",
        encoding: Annotated[str, Field(description="Text encoding to write with.")] = "utf-8",
    ) -> dict[str, Any]:
        """Create a brand-new file. Fails if the path already exists (never
        clobbers). Parent directories are created as needed. To replace an
        existing file use write_file."""
        return create_file_op(file_path, content, encoding)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get project structure",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def get_project_structure(
        root_path: Annotated[str, Field(description="Absolute root directory to map (must be inside C:\\Work).")],
        max_depth: Annotated[int, Field(description="How many directory levels deep to descend.", ge=1, le=20)] = 4,
        include_files: Annotated[bool, Field(description="Include files in the tree, not just directories.")] = True,
    ) -> dict[str, Any]:
        """Return a nested directory tree (folders and optionally files), skipping
        noise dirs. Good for getting an overview before diving in with list_files
        or search_codebase."""
        return project_tree(root_path, max_depth, include_files)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Append to file",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False,
        )
    )
    def append_file(
        file_path: Annotated[str, Field(description="Absolute path (inside C:\\Work). Created if missing.")],
        content: Annotated[str, Field(description="Text to append to the end of the file.")],
        encoding: Annotated[str, Field(description="Text encoding.")] = "utf-8",
    ) -> dict[str, Any]:
        """Append text to a file (creates it if missing). Additive — does not
        rewrite or back up existing content."""
        return append_file_op(file_path, content, encoding)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get path info",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def get_info(
        path: Annotated[str, Field(description="Absolute path to inspect (inside C:\\Work).")],
    ) -> dict[str, Any]:
        """Return metadata for a path: exists, type (file/dir/missing), size,
        modified time, and (for small text files) line count."""
        return get_info_op(path)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Read file chunk",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def read_file_chunk(
        file_path: Annotated[str, Field(description="Absolute path to read (inside C:\\Work).")],
        offset: Annotated[int, Field(description="Byte offset to start at.", ge=0)] = 0,
        length: Annotated[int, Field(description="Bytes to read from offset.", ge=1, le=1_000_000)] = 8192,
        encoding: Annotated[str, Field(description="Text encoding (decoded with errors='replace').")] = "utf-8",
    ) -> dict[str, Any]:
        """Read a byte-range chunk of a file via seek (doesn't load the whole
        file). Returns content + has_more for paging through large files where
        read_file's line ranges aren't enough."""
        return read_file_chunk_op(file_path, offset, length, encoding)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Copy path",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def copy_path(
        src: Annotated[str, Field(description="Source file/dir, absolute, inside C:\\Work.")],
        dst: Annotated[str, Field(description="Destination, absolute, inside C:\\Work.")],
        overwrite: Annotated[bool, Field(description="Allow overwriting an existing destination.")] = False,
    ) -> dict[str, Any]:
        """Copy a file or directory within C:\\Work. Refuses to overwrite unless
        overwrite=true."""
        return copy_path_op(src, dst, overwrite)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Move path",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def move_path(
        src: Annotated[str, Field(description="Source file/dir, absolute, inside C:\\Work.")],
        dst: Annotated[str, Field(description="Destination, absolute, inside C:\\Work.")],
        overwrite: Annotated[bool, Field(description="Allow overwriting an existing destination.")] = False,
    ) -> dict[str, Any]:
        """Move/rename a file or directory within C:\\Work. Refuses to overwrite
        unless overwrite=true."""
        return move_path_op(src, dst, overwrite)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Delete path (soft)",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def delete_path(
        path: Annotated[str, Field(description="Absolute path to delete (inside C:\\Work).")],
    ) -> dict[str, Any]:
        """Soft-delete: move the target into the project's .trash (reversible).
        Never hard-deletes. Returns the trash path for restore."""
        return delete_path_op(path)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Edit file (multi-hunk patch)",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False,
        )
    )
    def edit_file_patch(
        file_path: Annotated[str, Field(description="Absolute path to the file to edit (inside C:\\Work).")],
        hunks: Annotated[list[dict[str, str]], Field(description="List of {\"find\": exact text, \"replace\": new text}. Each 'find' must occur exactly once; hunks must not overlap.")],
        dry_run: Annotated[bool, Field(description="Preview the diff without writing. Recommended first.")] = False,
        backup_suffix: Annotated[str, Field(description="Suffix for the backup made before writing.")] = ".bak",
        encoding: Annotated[str, Field(description="Text encoding.")] = "utf-8",
    ) -> dict[str, Any]:
        """Apply several exact-block edits atomically (all-or-nothing). Each
        hunk's 'find' must match exactly once — no fuzzy matching, so it never
        silently corrupts the file. Backs up first; dry_run shows the diff."""
        return edit_file_patch_op(file_path, hunks, dry_run, backup_suffix, encoding)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Restore from trash",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False,
        )
    )
    def restore_path(
        trash_path: Annotated[str, Field(description="The trash_path returned by delete_path (inside C:\\Work\\TinyPyMCP\\.trash).")],
        restore_to: Annotated[str, Field(description="Absolute destination to restore to (inside C:\\Work).")],
        overwrite: Annotated[bool, Field(description="Allow overwriting an existing destination.")] = False,
    ) -> dict[str, Any]:
        """Restore a soft-deleted item from .trash to a destination. Companion to
        delete_path."""
        return restore_path_op(trash_path, restore_to, overwrite)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List trash",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def list_trash(
        limit: Annotated[int, Field(description="Max items to return (newest first).", ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """List soft-deleted items in .trash with their trash paths, so they can
        be restored with restore_path. Read-only."""
        return list_trash_op(limit)

    # ── Persistent memory (SQLite, no embeddings yet) ───────────────────────

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: get agent state",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_get_state(
        agent_name: Annotated[str, Field(description="Agent identifier whose state to fetch, e.g. 'grok'.", min_length=1, max_length=64)],
    ) -> dict[str, Any]:
        """Get the persisted working state for an agent: session_id, current_task
        and a free-form context object. Returns state=null if nothing was stored
        yet for that agent."""
        state = mem.get_agent_state(agent_name)
        return state if state is not None else {"agent_name": agent_name, "state": None}

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: set agent state",
            readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_set_state(
        agent_name: Annotated[str, Field(description="Agent identifier whose state to update, e.g. 'grok'.", min_length=1, max_length=64)],
        session_id: Annotated[str | None, Field(description="Current session id. Omit to leave unchanged.")] = None,
        current_task: Annotated[str | None, Field(description="Short description of what the agent is doing now. Omit to leave unchanged.")] = None,
        context: Annotated[dict[str, Any] | None, Field(description="Arbitrary JSON metadata for the session. Omit to leave unchanged; pass {} to clear.")] = None,
    ) -> dict[str, Any]:
        """Upsert an agent's working state. Only the fields you pass are changed;
        omitted fields keep their previous value (merge, not overwrite). Use this
        to remember where you are across calls/sessions."""
        return mem.set_agent_state(agent_name, session_id, current_task, context)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: save entry",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_save(
        content: Annotated[str, Field(description="The fact/insight to remember, in plain language.", min_length=3, max_length=4096)],
        agent_name: Annotated[str, Field(description="Who is saving this. Omit for a shared/global memory.", max_length=64)] = "",
        type: Annotated[Literal["fact", "experience", "conclusion", "error"], Field(description="Classification of the memory.")] = "fact",
        category: Annotated[str, Field(description="Optional topic label for filtering/search, e.g. 'mcp', 'infra'.", max_length=64)] = "",
    ) -> dict[str, Any]:
        """Persist one memory entry to the database (appended, never overwrites
        existing memories). Returns the stored entry including its generated id."""
        return mem.save_memory(content, agent_name, type, category)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: search",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_search(
        query: Annotated[str, Field(description="What you're looking for. Semantically ranked (bge-m3) when available, else keyword overlap.", min_length=2, max_length=512)],
        agent_name: Annotated[str, Field(description="Restrict to one agent's memories. Omit to search all.", max_length=64)] = "",
        top_k: Annotated[int, Field(description="Maximum number of results to return.", ge=1, le=20)] = 5,
        min_score: Annotated[float, Field(description="Minimum relevance score (0-1: cosine for semantic, token-overlap for lexical).", ge=0.0, le=1.0)] = 0.1,
    ) -> dict[str, Any]:
        """Retrieve the most relevant saved memories for a query. Semantic KNN via
        sqlite-vec + OVH bge-m3 embeddings when available (result mode='semantic'),
        with keyword token-overlap fallback (mode='lexical'). Call this before
        assuming you don't know something. Returns scored results + how many searched."""
        return mem.search_memory(query, agent_name, top_k, min_score)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: reindex embeddings",
            readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_reindex(
        agent_name: Annotated[str, Field(description="Restrict backfill to one agent. Omit for all.", max_length=64)] = "",
        limit: Annotated[int, Field(description="Max memories to embed this call.", ge=1, le=5000)] = 1000,
    ) -> dict[str, Any]:
        """Backfill sqlite-vec embeddings for memories that lack them (e.g. saved
        while the embedding provider was off). Idempotent. Returns counts."""
        return mem.reindex_embeddings(agent_name, limit)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: create task",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_create_task(
        title: Annotated[str, Field(description="Short task title.", min_length=3, max_length=256)],
        created_by: Annotated[str, Field(description="Agent creating the task.", max_length=64)] = "",
        assigned_to: Annotated[str, Field(description="Target agent. Omit to leave the task unassigned (visible to all).", max_length=64)] = "",
        description: Annotated[str, Field(description="Full task details.", max_length=2048)] = "",
        priority: Annotated[int, Field(description="Priority 1 (low) to 10 (critical). Higher sorts first.", ge=1, le=10)] = 5,
    ) -> dict[str, Any]:
        """Add a task to the shared, persisted to-do list. Returns the stored task
        including its generated id and status='pending'."""
        return mem.create_task(title, created_by, assigned_to, description, priority)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Memory: get tasks",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def memory_get_tasks(
        assigned_to: Annotated[str, Field(description="Filter to one assignee. Unassigned tasks are always included. Omit for all.", max_length=64)] = "",
        status: Annotated[Literal["pending", "in_progress", "done", "cancelled"], Field(description="Which task status to list.")] = "pending",
        limit: Annotated[int, Field(description="Maximum number of tasks to return.", ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        """List tasks of a given status, highest priority first then newest. With
        assigned_to set, returns that agent's tasks plus any unassigned ones."""
        return mem.get_tasks(assigned_to, status, limit)

    # ── Local execution (Stage 1: allowlisted, workspace-confined) ──────────

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Run command",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    async def run_command(
        program: Annotated[str, Field(description=f"Program to run. Allowlisted only: {sorted(ALLOWED_PROGRAMS)}. No shell, no pipes.")],
        args: Annotated[list[str], Field(description="Arguments as a list, e.g. ['install'] or ['-m','pytest','-q']. Not a shell string.")] = [],
        cwd: Annotated[str | None, Field(description="Working directory (absolute, inside C:\\Work). Defaults to the workspace root.")] = None,
        timeout: Annotated[int, Field(description="Seconds before the process is killed.", ge=1, le=600)] = 120,
    ) -> dict[str, Any]:
        """Run one allowlisted dev program (git/node/npm/python/pytest/...) with
        the given args. No shell: pass args as a list, not a command string.
        Returns exit_code, stdout, stderr, whether it timed out, and duration.
        Used for installing deps, running tests, building, etc. Long runs are
        actively cancellable: a client disconnect kills the child process."""
        return await run_command_op(program, args, cwd, timeout)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Clone git repo",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True,
        )
    )
    async def clone_repo(
        repo_url: Annotated[str, Field(description="Git URL to clone, e.g. https://github.com/owner/name.git", min_length=4)],
        dest_name: Annotated[str | None, Field(description="Folder name under the workspace. Defaults to the repo name. No path separators.")] = None,
        depth: Annotated[int, Field(description="Shallow clone depth. 0 = full history.", ge=0, le=1000)] = 1,
    ) -> dict[str, Any]:
        """Clone a git repository into the workspace (under C:\\Work). Network
        access is allowed. Never overwrites an existing folder. Returns the
        destination path and the git output. Actively cancellable on disconnect."""
        return await clone_repo_op(repo_url, dest_name, depth)

    # ── Network helpers (Stage 2) ───────────────────────────────────────────

    @mcp.tool(
        annotations=ToolAnnotations(
            title="HTTP probe",
            readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True,
        )
    )
    def http_probe(
        url: Annotated[str, Field(description="Full URL to request, including scheme.", min_length=4)],
        method: Annotated[Literal["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"], Field(description="HTTP method.")] = "GET",
        headers: Annotated[dict[str, str] | None, Field(description="Optional request headers.")] = None,
        body: Annotated[str | None, Field(description="Optional request body (for POST/PUT/PATCH).")] = None,
        timeout: Annotated[int, Field(description="Seconds before the request times out.", ge=1, le=120)] = 20,
    ) -> dict[str, Any]:
        """Make an HTTP request to any URL and return status, headers and a
        capped body. Use to test endpoints (e.g. verify a deployed service) or
        fetch a page. Follows redirects."""
        return http_probe_op(url, method, headers, body, timeout)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Check npm package",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def check_npm_package(
        name: Annotated[str, Field(description="npm package name, e.g. 'express' or '@scope/pkg'.", min_length=1)],
    ) -> dict[str, Any]:
        """Look up an npm package: latest version, description, license and its
        dependencies. Useful when porting JS to Python to understand what a
        dependency does."""
        return check_npm_op(name)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Check PyPI package",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def check_pypi_package(
        name: Annotated[str, Field(description="PyPI package name, e.g. 'requests'.", min_length=1)],
    ) -> dict[str, Any]:
        """Look up a PyPI package: latest version, summary, license, required
        Python and dependencies. Useful for finding the Python equivalent of a
        JS package when porting."""
        return check_pypi_op(name)

    # ── Code intelligence (Stage: study a foreign repo, no execution) ───────

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Code dependencies",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def code_dependencies(
        path: Annotated[str, Field(description="Absolute directory to analyze (inside C:\\Work).")],
        recursive: Annotated[bool, Field(description="Recurse into subdirectories.")] = True,
        max_files: Annotated[int, Field(description="Cap on files scanned.", ge=1, le=5000)] = 500,
        top_n: Annotated[int, Field(description="How many hot files/externals to list in the summary.", ge=1, le=100)] = 20,
        include_graph: Annotated[bool, Field(description="Also return the full node/edge list (large). Default summary only.")] = False,
    ) -> dict[str, Any]:
        """Build a static import dependency graph for a Python/JS/TS repo WITHOUT
        executing it. Returns counts, fan-in/fan-out hot files, top external
        deps and unresolved imports. Set include_graph=true for the full graph.
        Use to understand a foreign repo's structure before porting."""
        graph = build_dependency_graph(path, recursive, max_files)
        out = {
            "path": graph["path"],
            "nodes_count": graph["nodes_count"],
            "edges_count": graph["edges_count"],
            "externals_count": len(graph["externals"]),
            "unresolved_count": graph["unresolved_count"],
            "truncated": graph["truncated"],
            "summary": summarize_graph(graph, top_n),
        }
        if include_graph:
            out["nodes"] = graph["nodes"]
            out["edges"] = graph["edges"]
            out["externals"] = graph["externals"]
        return out

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Code impact",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def code_impact(
        path: Annotated[str, Field(description="Absolute repo root to analyze (inside C:\\Work).")],
        target: Annotated[str, Field(description="File to trace, as a path relative to 'path', e.g. 'src/server.py'.")],
        direction: Annotated[Literal["both", "dependents", "dependencies"], Field(description="dependents = who imports target; dependencies = what target imports.")] = "both",
        max_depth: Annotated[int, Field(description="How many import hops to follow.", ge=1, le=20)] = 5,
        recursive: Annotated[bool, Field(description="Recurse into subdirectories.")] = True,
        max_files: Annotated[int, Field(description="Cap on files scanned.", ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        """Trace the import-impact of one file to a given depth: what it depends
        on, and what depends on it. Use before changing/porting a file to see
        the blast radius."""
        graph = build_dependency_graph(path, recursive, max_files)
        return analyze_impact(graph, target, direction, max_depth)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Build search index",
            readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def build_index(
        path: Annotated[str, Field(description="Absolute directory to index (inside C:\\Work).")],
        max_files: Annotated[int, Field(description="Cap on files indexed.", ge=1, le=5000)] = 5000,
    ) -> dict[str, Any]:
        """Build a persistent inverted index (token -> file/line) for a repo, so
        later searches are instant. Rebuilds if one already exists. Run this once
        after cloning a big repo, then use search_index for fast lookups."""
        return build_index_op(path, max_files)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search index",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def search_index(
        path: Annotated[str, Field(description="Absolute directory previously passed to build_index.")],
        query: Annotated[str, Field(description="Terms to find. Returns lines containing ALL terms (AND).", min_length=1)],
        limit: Annotated[int, Field(description="Max results to return.", ge=1, le=500)] = 50,
        context: Annotated[int, Field(description="Lines of context before/after each hit (0 = none).", ge=0, le=20)] = 0,
    ) -> dict[str, Any]:
        """Fast lookup against a prebuilt index: returns file/line/text for lines
        containing all query terms. With context>0, each hit also includes that
        many surrounding lines. Run build_index first. Much faster than
        search_codebase for repeated queries on a large repo."""
        return search_index_op(path, query, limit, context)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Code symbols",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def code_symbols(
        file_path: Annotated[str, Field(description="Absolute path to a .py or JS/TS file (inside C:\\Work).")],
    ) -> dict[str, Any]:
        """List the functions, classes and methods defined in one file, with line
        ranges. Python is parsed accurately (ast); JS/TS is best-effort. No
        execution. Use to map a file's structure when studying a foreign repo."""
        return extract_symbols_op(file_path)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Index status",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        )
    )
    def index_status(
        path: Annotated[str, Field(description="Absolute directory to check (inside C:\\Work).")],
    ) -> dict[str, Any]:
        """Report whether a search index exists for a directory and its stats
        (files, postings, size, built-at) without rebuilding it."""
        return index_status_op(path)

    # ── VPS bounded-channel client (credentials read from disk, never args) ──

    @mcp.tool(
        annotations=ToolAnnotations(
            title="VPS channel status",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def vps_status(
        channel: Annotated[str | None, Field(description="Channel name (e.g. 'router' or 'deploy'); omit for the configured default.")] = None,
    ) -> dict[str, Any]:
        """GET /v1/status on the configured bounded VPS channel. Credentials
        (Cloudflare Access service token / bearer) are read from the config file
        on disk, never passed here. Use to confirm the channel is reachable.
        The config path is fixed server-side (MCP_VPS_CONFIG / default), not a
        tool argument, to avoid a confused-deputy / file-existence oracle."""
        return vps_call_op("GET", "/v1/status", None, channel)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="VPS channel request",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    async def vps_request(
        method: Annotated[Literal["GET", "POST", "PUT", "DELETE", "PATCH"], Field(description="HTTP method.")],
        path: Annotated[str, Field(description="Path on the channel, e.g. '/v1/exec/run' or '/v1/compose/demo/up'. Appended to the configured base_url.", min_length=1)],
        body: Annotated[dict[str, Any] | None, Field(description="Optional JSON body.")] = None,
        channel: Annotated[str | None, Field(description="Channel name: 'router' (default) or 'deploy' (release plane). Omit for the configured default.")] = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the configured bounded VPS channel
        (router or deploy). The path is appended to the channel's base_url, so
        only that one host is reachable. Credentials come from the config file on
        disk — never from arguments. The config path is fixed server-side, not a
        tool argument. The server enforces what operations exist. Actively
        cancellable on client disconnect."""
        return await vps_call_async(method, path, body, channel)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Estate status",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    async def estate_status(
        include_containers: Annotated[bool, Field(description="Include host container list (docker ps).")] = True,
        include_monitors: Annotated[bool, Field(description="Include Uptime Kuma monitor health.")] = True,
        include_channel: Annotated[bool, Field(description="Include the bounded VPS channel /v1/status probe.")] = True,
    ) -> dict[str, Any]:
        """Machine-readable health snapshot of the ROMION estate — the data layer
        behind the estate dashboard. Aggregates the live MCP tool count, host
        containers, Uptime Kuma monitors and the bounded VPS channel. Fail-open
        per section: a failing subsystem is reported as an error inside its
        section, never crashing the snapshot. Read-only."""
        from src.estate import collect_estate
        return await collect_estate(
            mcp,
            include_containers=include_containers,
            include_monitors=include_monitors,
            include_channel=include_channel,
        )

    # ── Cloudflare admin (token from ~/.romion/cloudflare.json, never an arg) ──

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: verify token",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def cf_verify_token() -> dict[str, Any]:
        """Verify the Cloudflare API token is live and active. Read-only."""
        return cf.verify_token()

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: list DNS",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def cf_list_dns(
        zone_name: Annotated[str | None, Field(description="Zone to list. Omit for the configured zone.")] = None,
    ) -> dict[str, Any]:
        """List DNS records for the zone (type/name/content/proxied + id). Read-only."""
        return cf.list_dns(name=zone_name)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: create DNS record",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_create_dns_record(
        rec_type: Annotated[str, Field(description="Record type, e.g. A, CNAME, TXT.")],
        name: Annotated[str, Field(description="Full record name, e.g. foo.romionologic.dev.")],
        content: Annotated[str, Field(description="Record content (IP, target, text).")],
        ttl: Annotated[int, Field(description="TTL seconds (1 = auto).", ge=1, le=86400)] = 60,
        proxied: Annotated[bool, Field(description="Route through Cloudflare proxy.")] = False,
        confirm: Annotated[bool, Field(description="Must be true to apply. Otherwise returns a dry-run preview.")] = False,
    ) -> dict[str, Any]:
        """Create a DNS record. Guarded: without confirm=true it only previews
        (dry-run). Applied writes are audited."""
        if not confirm:
            return {"dry_run": True, "would_create": {"type": rec_type, "name": name, "content": content, "ttl": ttl, "proxied": proxied}, "note": "set confirm=true to apply"}
        audit("cf_create_dns", {"type": rec_type, "name": name})
        return cf.create_dns_record(rec_type, name, content, ttl, proxied)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: delete DNS record",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_delete_dns_record(
        record_id: Annotated[str, Field(description="DNS record id (from cf_list_dns).")],
        confirm: Annotated[bool, Field(description="Must be true to apply. Otherwise returns a dry-run preview.")] = False,
        force: Annotated[bool, Field(description="Override the protected-resource guard (production records). Use with care.")] = False,
    ) -> dict[str, Any]:
        """Delete a DNS record by id. Guarded: without confirm=true it only
        previews; protected production records also need force=true. Audited."""
        if not confirm:
            return {"dry_run": True, "would_delete": record_id, "note": "set confirm=true to apply"}
        audit("cf_delete_dns", {"record_id": record_id, "force": force})
        return cf.delete_dns_record(record_id, force=force)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: list tunnels",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def cf_list_tunnels() -> dict[str, Any]:
        """List the account's Cloudflare Tunnels (id/name/status). Read-only."""
        return cf.list_tunnels()

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: get tunnel config",
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True,
        )
    )
    def cf_get_tunnel_config(
        tunnel_id: Annotated[str, Field(description="Tunnel id (from cf_list_tunnels).")],
    ) -> dict[str, Any]:
        """Get a tunnel's config incl. the ingress (public-hostname) rules. Read-only."""
        return cf.get_tunnel_config(tunnel_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: add tunnel route",
            readOnlyHint=False, idempotentHint=True, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_add_tunnel_route(
        tunnel_id: Annotated[str, Field(description="Tunnel id.")],
        hostname: Annotated[str, Field(description="Public hostname, e.g. svc.romionologic.dev.")],
        service: Annotated[str, Field(description="Origin service, e.g. http://my-container:8091.")],
        confirm: Annotated[bool, Field(description="Must be true to apply. Otherwise a dry-run preview.")] = False,
    ) -> dict[str, Any]:
        """Add/replace a public-hostname route on a tunnel (GET→modify→PUT,
        preserves all other routes). Guarded: confirm=true to apply; audited.
        Pair with cf_create_dns_record for the CNAME."""
        if not confirm:
            return {"dry_run": True, "would_add": {"tunnel_id": tunnel_id, "hostname": hostname, "service": service}, "note": "set confirm=true to apply"}
        audit("cf_add_tunnel_route", {"tunnel_id": tunnel_id, "hostname": hostname, "service": service})
        return cf.add_tunnel_route(tunnel_id, hostname, service)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: remove tunnel route",
            readOnlyHint=False, idempotentHint=True, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_remove_tunnel_route(
        tunnel_id: Annotated[str, Field(description="Tunnel id.")],
        hostname: Annotated[str, Field(description="Public hostname to remove.")],
        confirm: Annotated[bool, Field(description="Must be true to apply. Otherwise a dry-run preview.")] = False,
        force: Annotated[bool, Field(description="Override the protected-host guard (router/deploy/etc.). Use with care.")] = False,
    ) -> dict[str, Any]:
        """Remove a public-hostname route from a tunnel (preserves all others).
        Guarded: confirm=true to apply; protected hosts also need force=true. Audited."""
        if not confirm:
            return {"dry_run": True, "would_remove": {"tunnel_id": tunnel_id, "hostname": hostname}, "note": "set confirm=true to apply"}
        audit("cf_remove_tunnel_route", {"tunnel_id": tunnel_id, "hostname": hostname, "force": force})
        return cf.remove_tunnel_route(tunnel_id, hostname, force=force)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: create Access app",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_create_access_app(
        name: Annotated[str, Field(description="App name.")],
        domain: Annotated[str, Field(description="Hostname to protect, e.g. svc.romionologic.dev.")],
        confirm: Annotated[bool, Field(description="Must be true to apply. Otherwise a dry-run preview.")] = False,
    ) -> dict[str, Any]:
        """Create a self-hosted Access application for a hostname. Pair with
        cf_add_access_service_policy to gate it by a service token. Guarded."""
        if not confirm:
            return {"dry_run": True, "would_create_app": {"name": name, "domain": domain}, "note": "set confirm=true to apply"}
        audit("cf_create_access_app", {"name": name, "domain": domain})
        return cf.create_access_app(name, domain)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: delete Access app",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_delete_access_app(
        app_id: Annotated[str, Field(description="Access application id.")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
        force: Annotated[bool, Field(description="Override the protected-resource guard (production Access apps). Use with care.")] = False,
    ) -> dict[str, Any]:
        """Delete an Access application. Guarded + audited; protected production
        apps also need force=true."""
        if not confirm:
            return {"dry_run": True, "would_delete_app": app_id, "note": "set confirm=true to apply"}
        audit("cf_delete_access_app", {"app_id": app_id, "force": force})
        return cf.delete_access_app(app_id, force=force)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: add Access service policy",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_add_access_service_policy(
        app_id: Annotated[str, Field(description="Access application id.")],
        name: Annotated[str, Field(description="Policy name.")],
        token_id: Annotated[str, Field(description="Service token id to allow (non_identity / Service Auth).")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Add a Service-Auth policy (decision=non_identity) allowing only the
        given service token — same shape as the dashboard's 'Service Auth'. Guarded."""
        if not confirm:
            return {"dry_run": True, "would_add_policy": {"app_id": app_id, "name": name, "token_id": token_id}, "note": "set confirm=true to apply"}
        audit("cf_add_access_policy", {"app_id": app_id, "name": name})
        return cf.add_access_service_policy(app_id, name, token_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: create service token",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_create_service_token(
        name: Annotated[str, Field(description="Service token name.")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Create an Access service token. WARNING: the client_secret is returned
        ONCE and never again — store it securely (a config file outside C:\\Work),
        never echo it. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would_create_token": name, "note": "set confirm=true to apply; secret returned once"}
        audit("cf_create_service_token", {"name": name})
        return cf.create_service_token(name)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Cloudflare: delete service token",
            readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True,
        )
    )
    def cf_delete_service_token(
        token_id: Annotated[str, Field(description="Service token id.")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Delete an Access service token. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would_delete_token": token_id, "note": "set confirm=true to apply"}
        audit("cf_delete_service_token", {"token_id": token_id})
        return cf.delete_service_token(token_id)

    # --- OVHcloud host layer (scoped consumer key; read in read_only, mutations in cloud_admin) ---
    from src import ovh_client as ovhc

    @mcp.tool(annotations=ToolAnnotations(title="OVH VPS info", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def ovh_vps_info() -> dict[str, Any]:
        """Read VPS host info (model/state/zone) via the OVH API. Credentials are read from
        a config file on disk (never arguments); the consumer-key access rules are the hard bound."""
        return ovhc.vps_info()

    @mcp.tool(annotations=ToolAnnotations(title="OVH snapshot status", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def ovh_snapshot_status() -> dict[str, Any]:
        """Current VPS snapshot (ok:false / not-found if none exists). Read-only."""
        return ovhc.snapshot_status()

    @mcp.tool(annotations=ToolAnnotations(title="OVH automated backup status", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def ovh_automated_backup_status() -> dict[str, Any]:
        """Automated-backup settings/state for the VPS. Read-only."""
        return ovhc.automated_backup_status()

    @mcp.tool(annotations=ToolAnnotations(title="OVH available images", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def ovh_images_available() -> dict[str, Any]:
        """List OS image ids available for the VPS (for reference; reinstall/rebuild stay operator-only). Read-only."""
        return ovhc.images_available()

    @mcp.tool(annotations=ToolAnnotations(title="OVH create snapshot", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def ovh_create_snapshot(
        description: Annotated[str, Field(description="Optional snapshot label.")] = "",
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Create the VPS snapshot (single-snapshot model) — the deploy safety-net. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "createSnapshot", "note": "set confirm=true to apply"}
        audit("ovh_create_snapshot", {"description": description})
        return ovhc.create_snapshot(description or None)

    @mcp.tool(annotations=ToolAnnotations(title="OVH revert snapshot", readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True))
    def ovh_revert_snapshot(
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Revert the VPS to its snapshot — rolls back the WHOLE host disk (data written after the
        snapshot is lost). Use only for infra rollback, not after DB writes. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "snapshot/revert", "warn": "reverts the entire host disk; data written after the snapshot is lost", "note": "set confirm=true to apply"}
        audit("ovh_revert_snapshot", {})
        return ovhc.revert_snapshot()

    @mcp.tool(annotations=ToolAnnotations(title="OVH abort snapshot", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def ovh_abort_snapshot(
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Abort an in-progress snapshot/automated-backup operation. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "abortSnapshot", "note": "set confirm=true to apply"}
        audit("ovh_abort_snapshot", {})
        return ovhc.abort_snapshot()

    @mcp.tool(annotations=ToolAnnotations(title="OVH automated-backup restore", readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True))
    def ovh_automated_backup_restore(
        restore_point: Annotated[str, Field(description="Restore point id (from the automated backup restorePoints list).")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Restore the VPS from an automated-backup restore point. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "automatedBackup/restore", "restore_point": restore_point, "note": "set confirm=true to apply"}
        audit("ovh_automated_backup_restore", {"restore_point": restore_point})
        return ovhc.automated_backup_restore(restore_point)

    @mcp.tool(annotations=ToolAnnotations(title="OVH reboot VPS", readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=True))
    def ovh_reboot(
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Reboot the VPS host (disruptive — affects every service on it). Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "reboot", "warn": "reboots the whole host", "note": "set confirm=true to apply"}
        audit("ovh_reboot", {})
        return ovhc.reboot()

    # --- Uptime Kuma (status.romionologic.dev; read in read_only, manage in cloud_admin) ---
    from src import kuma_client as kumac

    @mcp.tool(annotations=ToolAnnotations(title="Kuma list monitors", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def kuma_list_monitors() -> dict[str, Any]:
        """List Uptime Kuma monitors (id/name/url/type/active). Read-only."""
        return kumac.list_monitors()

    @mcp.tool(annotations=ToolAnnotations(title="Kuma monitor status", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def kuma_monitor_status() -> dict[str, Any]:
        """Latest heartbeat status per monitor (0=down, 1=up, 2=pending, 3=maintenance). Read-only."""
        return kumac.monitor_status()

    @mcp.tool(annotations=ToolAnnotations(title="Kuma add monitor", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def kuma_add_monitor(
        name: Annotated[str, Field(description="Monitor name.")],
        url: Annotated[str, Field(description="URL to monitor.")],
        interval: Annotated[int, Field(description="Check interval (seconds).")] = 60,
        accepted_statuscodes: Annotated["list[str] | None", Field(description="Accepted HTTP status ranges, e.g. ['200-299'] or ['401']. Default 200-299.")] = None,
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Add an HTTP monitor to Uptime Kuma. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "add_monitor", "name": name, "url": url, "note": "set confirm=true to apply"}
        audit("kuma_add_monitor", {"name": name, "url": url})
        return kumac.add_monitor(name, url, interval=interval, accepted_statuscodes=accepted_statuscodes)

    @mcp.tool(annotations=ToolAnnotations(title="Kuma pause monitor", readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def kuma_pause_monitor(
        monitor_id: Annotated[int, Field(description="Monitor id (from kuma_list_monitors).")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Pause a Kuma monitor. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "pause_monitor", "monitor_id": monitor_id, "note": "set confirm=true to apply"}
        audit("kuma_pause_monitor", {"monitor_id": monitor_id})
        return kumac.pause_monitor(monitor_id)

    @mcp.tool(annotations=ToolAnnotations(title="Kuma resume monitor", readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def kuma_resume_monitor(
        monitor_id: Annotated[int, Field(description="Monitor id (from kuma_list_monitors).")],
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Resume a paused Kuma monitor. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "resume_monitor", "monitor_id": monitor_id, "note": "set confirm=true to apply"}
        audit("kuma_resume_monitor", {"monitor_id": monitor_id})
        return kumac.resume_monitor(monitor_id)

    # --- GitHub (REST API via token; reads in read_only, PR mutations in cloud_admin) ---
    from src import github_client as ghc

    @mcp.tool(annotations=ToolAnnotations(title="GitHub repo info", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def gh_repo_info(
        owner: Annotated["str | None", Field(description="Repo owner. Omit to use the config default.")] = None,
        repo: Annotated["str | None", Field(description="Repo name. Omit to use the config default.")] = None,
    ) -> dict[str, Any]:
        """Get repo info (full_name/default_branch/private/url/description). Read-only."""
        return ghc.repo_info(owner, repo)

    @mcp.tool(annotations=ToolAnnotations(title="GitHub list PRs", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def gh_list_prs(
        state: Annotated[str, Field(description="open | closed | all.")] = "open",
        owner: Annotated["str | None", Field(description="Repo owner. Omit for config default.")] = None,
        repo: Annotated["str | None", Field(description="Repo name. Omit for config default.")] = None,
    ) -> dict[str, Any]:
        """List pull requests (number/title/state/head/base/url). Read-only."""
        return ghc.list_prs(owner, repo, state=state)

    @mcp.tool(annotations=ToolAnnotations(title="GitHub get PR", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def gh_get_pr(
        number: Annotated[int, Field(description="PR number.")],
        owner: Annotated["str | None", Field(description="Repo owner. Omit for config default.")] = None,
        repo: Annotated["str | None", Field(description="Repo name. Omit for config default.")] = None,
    ) -> dict[str, Any]:
        """Get one pull request. Read-only."""
        return ghc.get_pr(number, owner, repo)

    @mcp.tool(annotations=ToolAnnotations(title="GitHub create PR", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def gh_create_pr(
        title: Annotated[str, Field(description="PR title.")],
        head: Annotated[str, Field(description="Source branch (the branch with changes).")],
        base: Annotated[str, Field(description="Target branch.")] = "main",
        body: Annotated[str, Field(description="PR description (Markdown).")] = "",
        owner: Annotated["str | None", Field(description="Repo owner. Omit for config default.")] = None,
        repo: Annotated["str | None", Field(description="Repo name. Omit for config default.")] = None,
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Open a pull request (head -> base). Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "create_pr", "title": title, "head": head, "base": base, "note": "set confirm=true to apply"}
        audit("gh_create_pr", {"title": title, "head": head, "base": base, "repo": repo})
        return ghc.create_pr(title, head, base=base, body=body, owner=owner, repo=repo)

    @mcp.tool(annotations=ToolAnnotations(title="GitHub merge PR", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def gh_merge_pr(
        number: Annotated[int, Field(description="PR number to merge.")],
        method: Annotated[str, Field(description="merge | squash | rebase.")] = "squash",
        owner: Annotated["str | None", Field(description="Repo owner. Omit for config default.")] = None,
        repo: Annotated["str | None", Field(description="Repo name. Omit for config default.")] = None,
        confirm: Annotated[bool, Field(description="Must be true to apply.")] = False,
    ) -> dict[str, Any]:
        """Merge a pull request. Guarded + audited."""
        if not confirm:
            return {"dry_run": True, "would": "merge_pr", "number": number, "method": method, "note": "set confirm=true to apply"}
        audit("gh_merge_pr", {"number": number, "method": method, "repo": repo})
        return ghc.merge_pr(number, method=method, owner=owner, repo=repo)

    # --- OVH AI Endpoints (OpenAI-compatible; Bearer key from /secrets; read_only) ---
    # VPS clean-IP direct path; key separate from the MVLTT llm-agent-router.
    from src import ovh_ai_client as oai

    @mcp.tool(annotations=ToolAnnotations(title="OVH AI embeddings", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def ovh_ai_embeddings(
        text: Annotated[str, Field(description="Text to embed. (bge-m3 = 1024-dim, multilingual, 8192 tokens.)")],
        model: Annotated[str, Field(description="Embedding model.")] = "bge-m3",
    ) -> dict[str, Any]:
        """Return the embedding vector(s) for the given text via OVH AI Endpoints. Read-only."""
        return oai.embeddings(text, model=model)

    @mcp.tool(annotations=ToolAnnotations(title="OVH AI chat", readOnlyHint=True, idempotentHint=False, destructiveHint=False, openWorldHint=True))
    def ovh_ai_chat(
        prompt: Annotated[str, Field(description="User prompt (single-turn).")],
        system: Annotated["str | None", Field(description="Optional system instruction.")] = None,
        model: Annotated[str, Field(description="Chat model, e.g. gpt-oss-20b.")] = "gpt-oss-20b",
        max_tokens: Annotated[int, Field(description="Max output tokens.", ge=1, le=8192)] = 512,
        temperature: Annotated["float | None", Field(description="Sampling temperature (omit for model default).")] = None,
    ) -> dict[str, Any]:
        """Single chat completion via OVH AI Endpoints (clean-IP VPS path). Non-mutating."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return oai.chat(messages, model=model, max_tokens=max_tokens, temperature=temperature)

    # --- Whole-VPS read-only filesystem (host bind-mount; NOT path_guard-confined) ---
    # Read any file/dir on the VPS. Read-only; secret-file bytes withheld unless
    # MCP_FS_SECRET_MODE=allow (air-gapped instance). See src/vps/hostfs.py.
    from src.vps import hostfs as hfs

    @mcp.tool(annotations=ToolAnnotations(title="VPS list dir", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def vps_fs_list(
        path: Annotated[str, Field(description="Absolute VPS path (e.g. /home/deploy, /etc). Default root.")] = "/",
        limit: Annotated[int, Field(description="Max entries.", ge=1, le=2000)] = 200,
    ) -> dict[str, Any]:
        """List any directory on the whole VPS filesystem (metadata only). Read-only."""
        return hfs.fs_list(path, limit=limit)

    @mcp.tool(annotations=ToolAnnotations(title="VPS stat path", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def vps_fs_stat(
        path: Annotated[str, Field(description="Absolute VPS path to stat.")],
    ) -> dict[str, Any]:
        """Stat any path on the VPS (type, size, mode, owner, mtime). Read-only."""
        return hfs.fs_stat(path)

    @mcp.tool(annotations=ToolAnnotations(title="VPS read file", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def vps_fs_read(
        path: Annotated[str, Field(description="Absolute VPS path to read.")],
        max_bytes: Annotated[int, Field(description="Max bytes to return.", ge=1, le=1048576)] = 65536,
        offset: Annotated[int, Field(description="Byte offset to start at.", ge=0)] = 0,
    ) -> dict[str, Any]:
        """Read any file on the VPS. Secret-file bytes are withheld in redact mode. Read-only."""
        return hfs.fs_read(path, max_bytes=max_bytes, offset=offset)

    # --- Host docker control (via mounted docker.sock; reads ungated, mutations confirm+audit) ---
    from src.vps import dockerctl as dctl

    @mcp.tool(annotations=ToolAnnotations(title="VPS docker", readOnlyHint=False, idempotentHint=False, destructiveHint=True, openWorldHint=False))
    def vps_docker(
        args: Annotated[list[str], Field(description="docker CLI args as a list, e.g. ['ps','-a'] or ['logs','--tail','100','tinypymcp']. NOT a shell string.")],
        confirm: Annotated[bool, Field(description="Must be true for mutating subcommands (run/exec/rm/stop/restart/build/compose/...).")] = False,
    ) -> dict[str, Any]:
        """Run a docker CLI command on the VPS host (mounted docker.sock). Reads ungated; mutations confirm-guarded + audited."""
        return dctl.docker(args, confirm=confirm)

    # --- SIM/compute job governance, stage 1 (read-only / dry-run; MCP as typed
    # governance interface, never the compute engine — see compute-plane ADR). ---
    from src.sim import manifest as simmf

    @mcp.tool(annotations=ToolAnnotations(title="SIM: validate job manifest", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def sim_validate_job_manifest(
        manifest: Annotated[dict, Field(description="Job manifest object (romion.sim.job_manifest.v1).")],
    ) -> dict[str, Any]:
        """Validate a SIM/compute job manifest against the v1 schema. Pure, no state."""
        return simmf.validate(manifest)

    @mcp.tool(annotations=ToolAnnotations(title="SIM: dry-run plan", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def sim_submit_job_dry_run(
        manifest: Annotated[dict, Field(description="Job manifest to validate + plan. NOTHING is persisted or executed.")],
    ) -> dict[str, Any]:
        """Validate a manifest and return the would-be execution plan. Persists/executes nothing (ADR stage 1)."""
        return simmf.plan_dry_run(manifest)

    @mcp.tool(annotations=ToolAnnotations(title="SIM: experiment catalog", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def sim_experiment_catalog() -> dict[str, Any]:
        """List the experiment types / resource profiles / schema the governance layer understands."""
        return simmf.catalog()

    # --- SIM job registry, stage 2 (persist + state + audit; STILL NO execution).
    # submit only records `pending_approval`; approval/compute deferred (ADR). ---
    from src.sim import registry as simreg

    @mcp.tool(annotations=ToolAnnotations(title="SIM: submit job (registry)", readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=False))
    async def sim_submit_job(
        manifest: Annotated[dict, Field(description="Job manifest (romion.sim.job_manifest.v1). Validated then persisted as pending_approval.")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Validate a manifest and persist it to the job registry as
        `pending_approval`. NOTHING is executed — heavy compute belongs to a
        separate future plane and approval is a deferred human step (ADR). Use
        sim_submit_job_dry_run first to see the plan. If the required
        `engine_version` is missing, the server elicits it from the client
        (SEP-1034/1036/1330); clients without elicitation degrade to the normal
        validation error."""
        m = dict(manifest)
        if not m.get("engine_version"):
            try:
                r = await ctx.elicit(
                    message="Manifest is missing the required 'engine_version'. Provide it:",
                    schema=_EngineVersionPrompt,
                )
                if r.action == "accept" and r.data:
                    m["engine_version"] = r.data.engine_version
                elif r.action == "decline":
                    return {"ok": False, "errors": ["engine_version is required (elicitation declined)"]}
                elif r.action == "cancel":
                    return {"ok": False, "errors": ["submission cancelled during elicitation"]}
            except Exception:
                # client doesn't support elicitation -> fall through to normal validation
                pass
        return simreg.submit(m, actor="agent")

    @mcp.tool(annotations=ToolAnnotations(title="SIM: job status", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def sim_job_status(
        job_id: Annotated[str, Field(description="Job id to read.")],
    ) -> dict[str, Any]:
        """Read a registered job's state, manifest and full audit trail."""
        return simreg.get(job_id)

    @mcp.tool(annotations=ToolAnnotations(title="SIM: list jobs", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False))
    def sim_list_jobs(
        state: Annotated["str | None", Field(description="Optional state filter (e.g. pending_approval).")] = None,
        limit: Annotated[int, Field(description="Max jobs (newest first).", ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """List registered jobs (newest first), optionally filtered by state."""
        return simreg.list_jobs(state, limit)

    # --- Research / security plane (read-only): CVE + GitHub advisory lookup. ---
    from src.net import security as sec

    @mcp.tool(annotations=ToolAnnotations(title="Check CVEs (OSV)", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def check_cve(
        package: Annotated[str, Field(description="Package name, e.g. 'requests'.")],
        ecosystem: Annotated[str, Field(description="OSV ecosystem: PyPI | npm | Go | crates.io | Maven | ...")] = "PyPI",
        version: Annotated["str | None", Field(description="Optional version to filter to vulns affecting it.")] = None,
        limit: Annotated[int, Field(description="Max vulns to return (slimmed).", ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Look up known vulnerabilities for a package via OSV.dev (id/CVE/severity/CWE/fixed). Read-only."""
        return sec.check_cve(package, ecosystem=ecosystem, version=version, limit=limit)

    @mcp.tool(annotations=ToolAnnotations(title="Check GitHub advisories", readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=True))
    def check_github_advisory(
        cve_id: Annotated["str | None", Field(description="Filter by CVE id, e.g. CVE-2023-32681.")] = None,
        ghsa_id: Annotated["str | None", Field(description="Filter by GHSA id.")] = None,
        affects: Annotated["str | None", Field(description="Package name affected, e.g. 'requests'.")] = None,
        ecosystem: Annotated["str | None", Field(description="pip | npm | go | rust | maven | ...")] = None,
        severity: Annotated["str | None", Field(description="low | medium | high | critical.")] = None,
        limit: Annotated[int, Field(description="Max advisories.", ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Query the GitHub global advisory database (slimmed). Read-only."""
        return sec.check_github_advisory(cve_id=cve_id, ghsa_id=ghsa_id, affects=affects, ecosystem=ecosystem, severity=severity, limit=limit)

    if _oauth_provider is not None:
        from src.oauth.app import register_operator_login
        register_operator_login(mcp, _oauth_provider)

    # Estate dashboard on the EXISTING tunnel (no new Worker/tunnel/connector).
    # Session-based: a token authenticates LOGIN, then an HttpOnly cookie
    # authorizes requests — NO secret in the URL. Custom routes survive the
    # profile prune below. Hardening TODO: front with Cloudflare Access.
    import secrets as _secrets
    from starlette.responses import (
        HTMLResponse as _HTMLResp,
        JSONResponse as _JSONResp,
        PlainTextResponse as _PlainResp,
        RedirectResponse as _RedirResp,
    )
    from src.estate import (
        collect_estate as _collect_estate,
        DASHBOARD_HTML as _DASH_HTML,
        LOGIN_HTML as _DASH_LOGIN,
        SESSION_COOKIE as _DASH_COOKIE,
        SESSION_TTL as _DASH_TTL,
        new_session as _new_session,
        session_valid as _session_valid,
        session_method as _session_method,
        drop_session as _drop_session,
    )

    # Prefer a DEDICATED MCP_DASHBOARD_TOKEN over the OAuth operator secret (so the
    # login credential is decoupled from OAuth). Fail CLOSED if neither is set.
    _dash_token = os.environ.get("MCP_DASHBOARD_TOKEN", "").strip() or (operator_secret or "")

    def _bearer_ok(request) -> bool:
        # Programmatic access (agent) via Authorization: Bearer <dash token>.
        if not _dash_token:
            return False
        auth = request.headers.get("authorization", "")
        return auth.lower().startswith("bearer ") and _secrets.compare_digest(auth[7:], _dash_token)

    def _session_ok(request) -> bool:
        return _session_valid(request.cookies.get(_DASH_COOKIE))

    @mcp.custom_route("/dashboard/login", methods=["GET", "POST"])
    async def _dash_login(request):
        if request.method == "GET":
            return _HTMLResp(_DASH_LOGIN)
        form = await request.form()
        token = str(form.get("token", ""))
        if not _dash_token or not _secrets.compare_digest(token, _dash_token):
            return _HTMLResp(_DASH_LOGIN, status_code=401)
        resp = _RedirResp("/dashboard", status_code=303)
        resp.set_cookie(_DASH_COOKIE, _new_session(), max_age=_DASH_TTL,
                        httponly=True, secure=True, samesite="strict", path="/")
        return resp

    @mcp.custom_route("/dashboard/logout", methods=["POST"])
    async def _dash_logout(request):
        _drop_session(request.cookies.get(_DASH_COOKIE))
        resp = _RedirResp("/dashboard/login", status_code=303)
        resp.delete_cookie(_DASH_COOKIE, path="/")
        return resp

    @mcp.custom_route("/estate.json", methods=["GET"])
    async def _estate_json(request):
        if not (_session_ok(request) or _bearer_ok(request)):
            return _PlainResp("unauthorized", status_code=401)
        return _JSONResp(await _collect_estate(mcp))

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def _estate_dashboard(request):
        if not _session_ok(request):
            return _HTMLResp(_DASH_LOGIN, status_code=401)
        via = _session_method(request.cookies.get(_DASH_COOKIE)) or "session"
        return _HTMLResp(_DASH_HTML.replace("__VIA__", via))

    # SIM human-approval — session-gated (operator in the browser). NOT exposed as
    # an agent tool and NOT Bearer-approvable, so the agent cannot self-approve a
    # job it submitted: the pending_approval -> approved gate is a human action.
    @mcp.custom_route("/sim.json", methods=["GET"])
    async def _sim_json(request):
        if not (_session_ok(request) or _bearer_ok(request)):
            return _PlainResp("unauthorized", status_code=401)
        from src.sim import registry as _reg
        state = request.query_params.get("state")
        return _JSONResp(_reg.list_jobs(state or None))

    @mcp.custom_route("/sim/jobs/{job_id}/approve", methods=["POST"])
    async def _sim_approve(request):
        if not _session_ok(request):
            return _PlainResp("unauthorized", status_code=401)
        from src.sim import registry as _reg
        form = await request.form()
        return _JSONResp(_reg.approve(request.path_params["job_id"], actor="operator",
                                      reason=str(form.get("reason", "")) or None))

    @mcp.custom_route("/sim/jobs/{job_id}/reject", methods=["POST"])
    async def _sim_reject(request):
        if not _session_ok(request):
            return _PlainResp("unauthorized", status_code=401)
        from src.sim import registry as _reg
        form = await request.form()
        return _JSONResp(_reg.reject(request.path_params["job_id"], actor="operator",
                                     reason=str(form.get("reason", "")) or None))

    # Profile gate: prune the registered surface to the union of selected
    # profiles. None = expose all (default). The active profiles ARE the
    # authorization boundary for which capability tiers this instance offers.
    if profiles is not None:
        from src.profiles import tools_for_profiles
        active = tools_for_profiles(profiles)
        tm = mcp._tool_manager
        for name in list(tm._tools.keys()):
            if name not in active:
                tm.remove_tool(name)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="TinyPyMCP - Precise file editor")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http", "streamable-http"],
        default="stdio",
        help="Transport mode (stdio, sse, http/streamable-http)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE mode",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for SSE mode (default: 8765)",
    )
    parser.add_argument(
        "--auth",
        choices=["bearer", "oauth", "none"],
        default=None,
        help="HTTP auth mode (overrides MCP_AUTH_MODE; default bearer).",
    )
    parser.add_argument(
        "--secret-file",
        default=None,
        help='OAuth mode: JSON file with {"operator_secret": "...", "issuer": "..."} (issuer optional).',
    )
    parser.add_argument(
        "--token-file",
        default=None,
        help='Bearer mode: JSON file with {"token": "..."}.',
    )
    parser.add_argument(
        "--allow-query-token",
        action="store_true",
        help="Bearer mode: also accept ?token= in the URL (for connectors that can't send headers, e.g. ChatGPT). Off by default — the token can leak into proxy/browser logs.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Comma-separated tool profiles to expose: read_only, operator_admin, cloud_admin, or all (default all). Flag > MCP_PROFILES env.",
    )
    args = parser.parse_args()

    import json as _json

    def _read_json(path):
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)

    # auth mode: --auth flag > MCP_AUTH_MODE env > bearer
    auth_mode = (args.auth or os.environ.get("MCP_AUTH_MODE", "bearer")).strip().lower()
    is_http = args.transport in ("sse", "http", "streamable-http")

    # secrets: --*-file flag takes precedence over env (file lives outside C:\Work)
    operator_secret = os.environ.get("MCP_OAUTH_OPERATOR_SECRET", "").strip()
    issuer = os.environ.get("MCP_OAUTH_ISSUER", "").strip() or None
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    try:
        if args.secret_file:
            d = _read_json(args.secret_file)
            operator_secret = str(d.get("operator_secret") or operator_secret).strip()
            issuer = d.get("issuer") or issuer
        if args.token_file:
            d = _read_json(args.token_file)
            token = str(d.get("token") or token).strip()
    except (OSError, ValueError) as e:
        print(f"[TinyPyMCP] REFUSING TO START: cannot read auth file: {e}")
        raise SystemExit(2)

    if is_http and auth_mode == "oauth" and not operator_secret:
        print("[TinyPyMCP] REFUSING TO START: oauth mode but no operator secret (--secret-file or MCP_OAUTH_OPERATOR_SECRET).")
        raise SystemExit(2)

    from src.profiles import resolve_profile_names
    try:
        active_profiles = resolve_profile_names(args.profile or os.environ.get("MCP_PROFILES"))
    except ValueError as e:
        print(f"[TinyPyMCP] REFUSING TO START: {e}")
        raise SystemExit(2)

    mcp = create_server(
        port=args.port,
        auth_mode="oauth" if (is_http and auth_mode == "oauth") else "bearer",
        issuer_url=issuer,
        operator_secret=operator_secret,
        profiles=active_profiles,
    )
    print(f"[TinyPyMCP] Active tool profiles: {', '.join(active_profiles)}")

    # Report exposed tools at startup so it's obvious what the connector sees.
    try:
        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        print(f"[TinyPyMCP] Exposing {len(tool_names)} tool(s): {', '.join(tool_names)}")
    except Exception as e:
        print(f"[TinyPyMCP] Could not enumerate tools: {e}")

    if args.transport == "stdio":
        print("[TinyPyMCP] Starting in stdio mode")
        try:
            mcp.run(transport="stdio")
        except KeyboardInterrupt:
            print("[TinyPyMCP] Stopped")

    elif args.transport in ("sse", "http", "streamable-http"):
        import uvicorn

        host = args.host
        port = args.port
        # Standalone SSE transport removed (deprecated; SSE is only the per-request
        # reply stream inside Streamable HTTP — see the transport assessment in www).
        # 'sse'/'http' are kept as back-compat aliases that now run streamable-http.
        if args.transport == "sse":
            print("[TinyPyMCP] note: --transport sse is deprecated; running streamable-http")
        transport = "streamable-http"
        path = getattr(mcp.settings, "streamable_http_path", "/mcp/v5")
        app = mcp.streamable_http_app()

        if auth_mode == "oauth":
            # FastMCP already mounted the OAuth routes + endpoint protection
            # (auth_server_provider + AuthSettings). No middleware to add.
            print(f"[TinyPyMCP] OAuth 2.1 auth ENABLED on {path} (self-contained AS; issuer={issuer or 'default public host'})")
        elif auth_mode == "none" or os.environ.get("MCP_AUTH_DISABLE", "").strip() == "1":
            print(f"[TinyPyMCP] WARNING: auth DISABLED — endpoint is open ({path})")
        else:  # bearer
            if not token:
                print("[TinyPyMCP] REFUSING TO START: no bearer token.")
                print("[TinyPyMCP] Use --token-file <json>, MCP_AUTH_TOKEN=<secret>, --auth oauth, or --auth none.")
                raise SystemExit(2)
            allow_qt = args.allow_query_token or os.environ.get("MCP_ALLOW_QUERY_TOKEN", "").strip().lower() in ("1", "true", "yes", "on")
            from src.auth_middleware import BearerAuthMiddleware
            app.add_middleware(BearerAuthMiddleware, token=token, allow_query_token=allow_qt)
            print(f"[TinyPyMCP] Bearer auth ENABLED on {path}" + (" (+ ?token= query accepted)" if allow_qt else " (header-only)"))

        rl = int(os.environ.get("MCP_RATE_LIMIT_PER_MIN", "0") or 0)
        if rl > 0:
            from src.utils.rate_limit import RateLimitMiddleware
            app.add_middleware(RateLimitMiddleware, per_min=rl)
            print(f"[TinyPyMCP] Rate limit: {rl} req/min per IP")

        print(f"[TinyPyMCP] Starting {transport} server on http://{host}:{port}{path}")
        from src.utils.log_redaction import build_redacting_log_config
        try:
            uvicorn.run(app, host=host, port=port, log_level="info",
                        log_config=build_redacting_log_config())
        except KeyboardInterrupt:
            print("[TinyPyMCP] Stopped")
    else:
        print(f"Nieznany transport: {args.transport}")


if __name__ == "__main__":
    main()
