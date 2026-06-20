'''TinyPyMCP - Core file operations (pure, no MCP dependency).
Provides safe, line-aware, occurrence-aware find and replace.
'''

from __future__ import annotations

import difflib
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .path_guard import ensure_within
from .audit import audit

# Soft-delete trash, under the project (inside C:\Work). Deletes are reversible.
_TRASH = Path(__file__).resolve().parents[2] / ".trash"


class Occurrence(TypedDict):
    line_num: int
    occurrence_in_line: int
    start_col: int
    end_col: int
    full_line: str
    context_before: list[str] | None
    context_after: list[str] | None


def _read_lines(path: Path, encoding: str = "utf-8") -> list[str]:
    try:
        return path.read_text(encoding=encoding).splitlines(keepends=True)
    except UnicodeDecodeError as e:
        raise RuntimeError(f"File is not valid {encoding} text.") from e


# Directories skipped during recursive listing/search/tree to avoid noise and
# binary blobs. Keep in sync across list_files / search_codebase / project_tree.
_IGNORED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "dist", "build", ".egg-info",
}


def list_files(
    dir_path: str | Path,
    pattern: str = "*",
    recursive: bool = False,
    include_dirs: bool = True,
) -> dict[str, Any]:
    """List files (and optionally directories) under a path, glob-filtered."""
    root = ensure_within(dir_path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    globber = root.rglob if recursive else root.glob
    entries: list[dict[str, Any]] = []
    for p in globber(pattern):
        if any(part in _IGNORED_DIRS for part in p.relative_to(root).parts):
            continue
        is_dir = p.is_dir()
        if is_dir and not include_dirs:
            continue
        try:
            size = p.stat().st_size if not is_dir else None
        except OSError:
            size = None
        entries.append({
            "path": str(p),
            "rel_path": str(p.relative_to(root)),
            "type": "dir" if is_dir else "file",
            "size": size,
        })

    entries.sort(key=lambda e: (e["type"] != "dir", e["rel_path"].lower()))
    return {"root": str(root), "count": len(entries), "entries": entries}


def search_codebase(
    root_path: str | Path,
    phrase: str,
    use_regex: bool = False,
    glob: str = "*",
    case_sensitive: bool = True,
    max_results: int = 500,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Recursively search files under root for a phrase. Skips ignored/binary files."""
    root = ensure_within(root_path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(phrase if use_regex else re.escape(phrase), flags)

    matches: list[dict[str, Any]] = []
    files_scanned = 0
    truncated = False

    for p in sorted(root.rglob(glob)):
        if not p.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in p.relative_to(root).parts):
            continue
        try:
            text = p.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue  # skip binary / unreadable
        files_scanned += 1
        for idx, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append({
                    "path": str(p),
                    "rel_path": str(p.relative_to(root)),
                    "line_num": idx,
                    "line": line.rstrip(),
                })
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "root": str(root),
        "phrase": phrase,
        "files_scanned": files_scanned,
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


def write_file_content(
    file_path: str | Path,
    content: str,
    overwrite: bool = True,
    backup_suffix: str = ".bak",
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> dict[str, Any]:
    """Write content to a file atomically, backing up an existing file first."""
    path = ensure_within(file_path)
    existed = path.is_file()
    if existed and not overwrite:
        raise FileExistsError(f"File exists and overwrite=False: {path}")
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if existed:
        backup_path = path.with_suffix(path.suffix + backup_suffix)
        shutil.copy2(path, backup_path)

    tmp_path = path.with_suffix(path.suffix + ".tmp~")
    try:
        tmp_path.write_text(content, encoding=encoding)
        tmp_path.replace(path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to write file safely: {e}") from e

    audit("write_file", {"file": str(path), "action": "overwritten" if existed else "created", "bytes": len(content.encode(encoding))})
    return {
        "status": "success",
        "file": str(path),
        "action": "overwritten" if existed else "created",
        "backup_path": str(backup_path) if backup_path else None,
        "bytes_written": len(content.encode(encoding)),
    }


def create_file(
    file_path: str | Path,
    content: str = "",
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> dict[str, Any]:
    """Create a new file. Fails if it already exists (no clobber)."""
    return write_file_content(
        file_path=file_path,
        content=content,
        overwrite=False,
        encoding=encoding,
        create_parents=create_parents,
    )


def project_tree(
    root_path: str | Path,
    max_depth: int = 4,
    include_files: bool = True,
) -> dict[str, Any]:
    """Build a nested directory tree, skipping ignored dirs."""
    root = ensure_within(root_path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    def build(node: Path, depth: int) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": node.name or str(node), "type": "dir", "children": []}
        if depth >= max_depth:
            entry["truncated"] = True
            return entry
        try:
            children = sorted(node.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return entry
        for child in children:
            if child.is_dir():
                if child.name in _IGNORED_DIRS:
                    continue
                entry["children"].append(build(child, depth + 1))
            elif include_files:
                entry["children"].append({"name": child.name, "type": "file"})
        return entry

    return {"root": str(root), "max_depth": max_depth, "tree": build(root, 0)}


def list_trash(limit: int = 100) -> dict[str, Any]:
    """List soft-deleted items in .trash (newest first). Read-only."""
    if not _TRASH.exists():
        return {"trash_root": str(_TRASH), "count": 0, "entries": []}
    limit = max(1, min(int(limit), 1000))
    entries: list[dict[str, Any]] = []
    for p in sorted(_TRASH.iterdir(), key=lambda x: x.name, reverse=True)[:limit]:
        ts, _, orig = p.name.partition("_")
        entries.append({
            "trash_path": str(p),
            "original_name": orig or p.name,
            "deleted_at_raw": ts,
            "type": "dir" if p.is_dir() else "file",
            "size": p.stat().st_size if p.is_file() else None,
        })
    return {"trash_root": str(_TRASH), "count": len(entries), "entries": entries}


def edit_file_patch(
    file_path: str | Path,
    hunks: list[dict[str, str]],
    dry_run: bool = False,
    backup_suffix: str = ".bak",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Apply several exact-block edits to a file in one atomic operation.

    Each hunk is {"find": <exact text>, "replace": <new text>}. Every `find`
    must occur EXACTLY ONCE in the file (else the whole patch is rejected), and
    hunks must not overlap. Position-based splice (order-independent), backup +
    atomic write. This is the safe alternative to fuzzy unified-diff apply —
    it never guesses, so it cannot silently corrupt the file.
    """
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if not hunks:
        raise ValueError("no hunks provided")

    content = path.read_text(encoding=encoding)
    spans: list[tuple[int, int, str, str]] = []
    for i, h in enumerate(hunks):
        find = h.get("find", "")
        repl = h.get("replace", "")
        if not find:
            raise ValueError(f"hunk {i}: 'find' is empty")
        n = content.count(find)
        if n != 1:
            raise ValueError(f"hunk {i}: 'find' must occur exactly once, found {n}")
        start = content.index(find)
        spans.append((start, start + len(find), find, repl))

    spans.sort()
    for a in range(1, len(spans)):
        if spans[a][0] < spans[a - 1][1]:
            raise ValueError("hunks overlap; cannot apply")

    new_content = content
    for start, end, _find, repl in sorted(spans, reverse=True):
        new_content = new_content[:start] + repl + new_content[end:]

    diff = list(difflib.unified_diff(
        content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=str(path), tofile=str(path),
    ))
    diff_text = "".join(diff) if diff else "(no visible change)"

    backup_path = None
    if not dry_run:
        backup_path = path.with_suffix(path.suffix + backup_suffix)
        shutil.copy2(path, backup_path)
        tmp_path = path.with_suffix(path.suffix + ".tmp~")
        try:
            tmp_path.write_text(new_content, encoding=encoding, newline="")
            tmp_path.replace(path)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to write file safely: {e}") from e
        audit("edit_file_patch", {"file": str(path), "hunks": len(hunks)})

    return {
        "status": "dry_run" if dry_run else "patched",
        "file": str(path),
        "hunks_applied": len(hunks),
        "backup_path": str(backup_path) if backup_path else None,
        "diff": diff_text,
    }


def restore_path(
    trash_path: str | Path,
    restore_to: str | Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Restore a soft-deleted item from .trash back to a destination (inside
    C:\\Work). Companion to delete_path."""
    src = ensure_within(trash_path)
    dst = ensure_within(restore_to)
    if not src.exists():
        raise FileNotFoundError(f"trash item not found: {src}")
    if dst.exists() and not overwrite:
        raise FileExistsError(f"destination exists (overwrite=False): {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and overwrite:
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.move(str(src), str(dst))
    audit("restore_path", {"trash": str(src), "restored_to": str(dst)})
    return {"status": "restored", "from": str(src), "to": str(dst)}


def append_file(
    file_path: str | Path,
    content: str,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> dict[str, Any]:
    """Append text to a file (create it if missing). Additive, no backup."""
    path = ensure_within(file_path)
    existed = path.is_file()
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding=encoding) as f:
        f.write(content)
    audit("append_file", {"file": str(path), "bytes": len(content.encode(encoding)), "created": not existed})
    return {
        "status": "appended",
        "file": str(path),
        "created": not existed,
        "bytes_appended": len(content.encode(encoding)),
        "new_size": path.stat().st_size,
    }


def get_info(path: str | Path) -> dict[str, Any]:
    """Metadata for a path: existence, type, size, mtime, and (for small text
    files) a line count. Read-only."""
    p = ensure_within(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "type": "missing"}
    st = p.stat()
    info: dict[str, Any] = {
        "path": str(p),
        "exists": True,
        "type": "dir" if p.is_dir() else "file",
        "size": st.st_size if p.is_file() else None,
        "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }
    if p.is_file() and st.st_size <= 5_000_000:
        try:
            info["line_count"] = sum(1 for _ in p.open("r", encoding="utf-8", errors="strict"))
            info["is_text"] = True
        except (UnicodeDecodeError, OSError):
            info["is_text"] = False
    return info


def read_file_chunk(
    file_path: str | Path,
    offset: int = 0,
    length: int = 8192,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Read a byte-range chunk of a file via seek (does not load the whole
    file). Decodes with errors='replace' so a multibyte char split at the chunk
    boundary degrades gracefully. For huge files where line ranges aren't enough."""
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    offset = max(0, int(offset))
    length = max(1, min(int(length), 1_000_000))
    total = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(length)
    return {
        "file": str(path),
        "offset": offset,
        "bytes_read": len(raw),
        "total_bytes": total,
        "has_more": offset + len(raw) < total,
        "content": raw.decode(encoding, errors="replace"),
    }


def copy_path(
    src: str | Path,
    dst: str | Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy a file or directory within C:\\Work. Refuses to overwrite unless asked."""
    s = ensure_within(src)
    d = ensure_within(dst)
    if not s.exists():
        raise FileNotFoundError(f"source not found: {s}")
    if d.exists() and not overwrite:
        raise FileExistsError(f"destination exists (overwrite=False): {d}")
    if s.is_dir():
        if d.exists() and overwrite:
            shutil.rmtree(d)
        shutil.copytree(s, d)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    audit("copy_path", {"src": str(s), "dst": str(d)})
    return {"status": "copied", "src": str(s), "dst": str(d), "type": "dir" if d.is_dir() else "file"}


def move_path(
    src: str | Path,
    dst: str | Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Move/rename a file or directory within C:\\Work. Refuses to overwrite unless asked."""
    s = ensure_within(src)
    d = ensure_within(dst)
    if not s.exists():
        raise FileNotFoundError(f"source not found: {s}")
    if d.exists():
        if not overwrite:
            raise FileExistsError(f"destination exists (overwrite=False): {d}")
        if d.is_dir():
            shutil.rmtree(d)
        else:
            d.unlink()
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    audit("move_path", {"src": str(s), "dst": str(d)})
    return {"status": "moved", "src": str(s), "dst": str(d)}


def delete_path(path: str | Path) -> dict[str, Any]:
    """Soft-delete: move the target into the project's .trash (reversible).
    Never hard-deletes."""
    p = ensure_within(path)
    if not p.exists():
        raise FileNotFoundError(f"path not found: {p}")
    _TRASH.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    trash_dst = _TRASH / f"{ts}_{p.name}"
    shutil.move(str(p), str(trash_dst))
    audit("delete_path_soft", {"original": str(p), "trash": str(trash_dst)})
    return {"status": "moved_to_trash", "original": str(p), "trash_path": str(trash_dst)}


def find_occurrences(
    file_path: str | Path,
    phrase: str,
    context_lines: int = 0,
    use_regex: bool = False,
    encoding: str = "utf-8",
) -> list[Occurrence]:
    """Find all occurrences of a phrase in a file.

    Returns list of dicts with line number, occurrence within line,
    column positions, full line, and optional context.
    """
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    lines = _read_lines(path, encoding)
    results: list[Occurrence] = []
    pattern = re.compile(phrase) if use_regex else None

    for idx, line in enumerate(lines):
        line_content = line.rstrip("\n\r")
        matches_in_line: list[tuple[int, int]] = []

        if use_regex and pattern:
            for m in pattern.finditer(line_content):
                matches_in_line.append((m.start(), m.end()))
        else:
            start = 0
            while True:
                pos = line_content.find(phrase, start)
                if pos == -1:
                    break
                matches_in_line.append((pos, pos + len(phrase)))
                start = pos + len(phrase)

        for occ_idx, (start_col, end_col) in enumerate(matches_in_line, 1):
            entry: Occurrence = {
                "line_num": idx + 1,
                "occurrence_in_line": occ_idx,
                "start_col": start_col,
                "end_col": end_col,
                "full_line": line_content,
                "context_before": None,
                "context_after": None,
            }
            if context_lines > 0:
                start_ctx = max(0, idx - context_lines)
                end_ctx = min(len(lines), idx + context_lines + 1)
                entry["context_before"] = [
                    lines[i].rstrip("\n\r") for i in range(start_ctx, idx)
                ]
                entry["context_after"] = [
                    lines[i].rstrip("\n\r") for i in range(idx + 1, end_ctx)
                ]
            results.append(entry)

    return results


def safe_replace(
    file_path: str | Path,
    old_phrase: str,
    new_phrase: str,
    line_num: int,
    occurrence: int,
    dry_run: bool = False,
    use_regex: bool = False,
    backup_suffix: str = ".bak",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Replace exactly one occurrence of a phrase in one specific line.

    Returns dict with status, diff, backup path, etc.
    Never modifies more than the chosen occurrence.
    """
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if line_num < 1:
        raise ValueError("line_num must be >= 1")

    lines = _read_lines(path, encoding)
    if line_num > len(lines):
        raise ValueError(f"Line {line_num} does not exist (file has {len(lines)} lines)")

    target_line = lines[line_num - 1]
    line_content = target_line.rstrip("\n\r")

    # Find all matches in the target line
    if use_regex:
        matches = list(re.finditer(old_phrase, line_content))
        match_positions = [(m.start(), m.end()) for m in matches]
    else:
        match_positions = []
        start = 0
        while True:
            pos = line_content.find(old_phrase, start)
            if pos == -1:
                break
            match_positions.append((pos, pos + len(old_phrase)))
            start = pos + len(old_phrase)

    if not match_positions:
        raise ValueError(f'Phrase "{old_phrase}" not found on line {line_num}')

    if occurrence < 1 or occurrence > len(match_positions):
        raise ValueError(
            f"Occurrence {occurrence} does not exist on line {line_num}. "
            f"Found {len(match_positions)} occurrence(s)."
        )

    match_start, match_end = match_positions[occurrence - 1]

    new_line_content = line_content[:match_start] + new_phrase + line_content[match_end:]

    # Preserve original line ending
    if target_line.endswith("\r\n"):
        line_ending = "\r\n"
    elif target_line.endswith("\r"):
        line_ending = "\r"
    else:
        line_ending = "\n"

    new_target_line = new_line_content + line_ending

    # Backup
    backup_path = path.with_suffix(path.suffix + backup_suffix)
    if not dry_run:
        shutil.copy2(path, backup_path)

    # Build new content
    new_lines = lines.copy()
    new_lines[line_num - 1] = new_target_line
    new_full_content = "".join(new_lines)

    # Diff
    diff = list(
        difflib.unified_diff(
            [line_content],
            [new_line_content],
            fromfile=f"{path.name} (line {line_num}, occ {occurrence})",
            tofile=f"{path.name} (line {line_num}, occ {occurrence})",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff) if diff else "(no visible change)"

    if not dry_run:
        tmp_path = path.with_suffix(path.suffix + ".tmp~")
        try:
            tmp_path.write_text(new_full_content, encoding=encoding, newline="")
            tmp_path.replace(path)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to write file safely: {e}") from e
        audit("safe_replace", {"file": str(path), "line_num": line_num, "occurrence": occurrence})

    return {
        "status": "dry_run" if dry_run else "success",
        "file": str(path),
        "line_num": line_num,
        "occurrence": occurrence,
        "old_phrase": old_phrase,
        "new_phrase": new_phrase,
        "backup_path": str(backup_path) if not dry_run else None,
        "diff": diff_text,
        "matches_found_in_line": len(match_positions),
    }


def edit_code_block(
    file_path: str | Path,
    old_block: str,
    new_block: str,
    dry_run: bool = False,
    backup_suffix: str = ".bak",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Safely replace a block of code that must occur exactly once in the file.

    This is safer than raw string replace for larger edits.
    """
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    content = path.read_text(encoding=encoding)

    if old_block not in content:
        raise ValueError(f"Old block not found in file")

    # Count occurrences
    count = content.count(old_block)
    if count != 1:
        raise ValueError(f"Old block must occur exactly once, found {count} occurrences")

    new_content = content.replace(old_block, new_block, 1)

    # Backup
    backup_path = None
    if not dry_run:
        backup_path = path.with_suffix(path.suffix + backup_suffix)
        shutil.copy2(path, backup_path)

    # Diff
    diff = list(difflib.unified_diff(
        content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
    ))
    diff_text = "".join(diff) if diff else "(no visible change)"

    if not dry_run:
        tmp_path = path.with_suffix(path.suffix + ".tmp~")
        try:
            tmp_path.write_text(new_content, encoding=encoding, newline="")
            tmp_path.replace(path)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to write file safely: {e}") from e
        audit("edit_code_block", {"file": str(path)})

    return {
        "status": "dry_run" if dry_run else "success",
        "file": str(path),
        "old_block_preview": old_block[:100] + "..." if len(old_block) > 100 else old_block,
        "backup_path": str(backup_path) if backup_path else None,
        "diff": diff_text,
        "changed": not dry_run,
    }
