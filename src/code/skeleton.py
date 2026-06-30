"""
TinyPyMCP - structural memory: a compact code SKELETON (no execution). For each
Python file: top-level defs/classes with signatures + first docstring line, and
class methods. The point is token economy — an agent understands a repo from this
tiny digest instead of reading full source (typically a large reduction vs LOC).
JS/TS is not skeletonized here (Python ast only); non-py files are listed as paths.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from src.utils.path_guard import ensure_within

_IGNORED = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
            ".pytest_cache", ".idea", ".vscode", "dist", "build"}


def _sig(node: ast.AST) -> str:
    try:
        args = ast.unparse(node.args)  # type: ignore[attr-defined]
    except Exception:
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)  # type: ignore[arg-type]
        except Exception:
            ret = ""
    return f"({args}){ret}"


def _doc1(node: ast.AST) -> str:
    try:
        d = ast.get_docstring(node)
    except Exception:
        d = None
    return d.strip().split("\n", 1)[0] if d else ""


def _file_skeleton(src: str) -> list[str]:
    tree = ast.parse(src)
    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            d = _doc1(node)
            lines.append(f"{kw} {node.name}{_sig(node)}" + (f"  - {d}" if d else ""))
        elif isinstance(node, ast.ClassDef):
            d = _doc1(node)
            lines.append(f"class {node.name}" + (f"  - {d}" if d else ""))
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kw = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                    md = _doc1(m)
                    lines.append(f"  {kw} {m.name}{_sig(m)}" + (f"  - {md}" if md else ""))
    return lines


def skeleton(path: str, recursive: bool = True, max_files: int = 500, max_chars: int = 120_000) -> dict[str, Any]:
    """Compact per-file Python skeleton + a compression ratio vs raw source."""
    root = ensure_within(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    max_files = max(1, min(int(max_files), 5000))

    globber = root.rglob if recursive else root.glob
    files: list[Path] = []
    for p in globber("*.py"):
        if any(part in _IGNORED for part in p.relative_to(root).parts):
            continue
        files.append(p)
        if len(files) >= max_files:
            break
    files.sort()

    parts: list[str] = []
    source_chars = 0
    symbol_count = 0
    for f in files:
        rel = str(f.relative_to(root)).replace("\\", "/")
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        source_chars += len(src)
        try:
            lines = _file_skeleton(src)
        except SyntaxError:
            parts.append(f"## {rel}\n(syntax error)")
            continue
        symbol_count += len(lines)
        body = "\n".join(lines) if lines else "(no top-level defs)"
        parts.append(f"## {rel}\n{body}")

    text = "\n\n".join(parts)
    truncated = len(text) > max_chars
    ratio = round(1 - (len(text) / source_chars), 3) if source_chars else 0.0
    return {
        "path": str(root),
        "files": len(files),
        "symbols": symbol_count,
        "source_chars": source_chars,
        "skeleton_chars": len(text),
        "reduction": ratio,  # 0.97 => skeleton is 97% smaller than raw source
        "skeleton": text[:max_chars],
        "truncated": truncated,
    }
