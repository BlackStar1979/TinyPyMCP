"""
TinyPyMCP - structural change detection between two git revisions (no exec of
the target code). Lists changed files and, per changed Python file, the
function/class symbols added/removed (AST-level), so a reviewer/agent sees the
real surface of a change rather than a raw line diff. Path-guarded; git only.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path
from typing import Any

from src.utils.path_guard import ensure_within


def _git(root: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git not available in this environment")
    return subprocess.run([git, "-C", str(root), *args], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _symbols(src: str) -> set[str]:
    """Qualified def names: 'func', 'Class', 'Class.method'."""
    out: set[str] = set()
    if not src:
        return out
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(prefix + child.name)
            elif isinstance(child, ast.ClassDef):
                out.add(prefix + child.name)
                walk(child, prefix + child.name + ".")

    walk(tree)
    return out


def detect_changes(path: str, base: str = "HEAD~1", head: str = "HEAD", max_files: int = 200) -> dict[str, Any]:
    """Diff two git revisions structurally: changed files + per-file symbol deltas."""
    root = ensure_within(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    max_files = max(1, min(int(max_files), 2000))

    chk = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if chk.returncode != 0:
        return {"ok": False, "error": "not a git work tree: " + (chk.stderr.strip()[:200])}
    diff = _git(root, ["diff", "--name-status", base, head])
    if diff.returncode != 0:
        return {"ok": False, "error": diff.stderr.strip()[:200] or f"git diff {base}..{head} failed"}

    files: list[dict[str, str]] = []
    sym_changes: dict[str, dict[str, list[str]]] = {}
    for line in diff.stdout.splitlines()[:max_files]:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]  # A/M/D/R/C
        rel = parts[-1].replace("\\", "/")
        files.append({"status": status, "path": rel})
        if not rel.endswith(".py"):
            continue
        base_src = "" if status == "A" else _git(root, ["show", f"{base}:{rel}"]).stdout
        head_src = "" if status == "D" else _git(root, ["show", f"{head}:{rel}"]).stdout
        b, h = _symbols(base_src), _symbols(head_src)
        added, removed = sorted(h - b), sorted(b - h)
        if added or removed:
            sym_changes[rel] = {"added": added, "removed": removed}

    by_status: dict[str, int] = {}
    for f in files:
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1
    return {
        "ok": True, "base": base, "head": head,
        "files_changed": len(files), "by_status": by_status,
        "files": files, "symbol_changes": sym_changes,
    }
