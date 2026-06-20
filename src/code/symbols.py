"""
TinyPyMCP - symbol extraction (functions / classes / methods).

Python: parsed with the stdlib `ast` (accurate, no execution). JS/TS: regex
best-effort. Returns names with line ranges, for getting an overview of a file
when studying a foreign repo. No code execution.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from src.utils.path_guard import ensure_within

_PY_EXT = {".py"}
_JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# JS/TS heuristics (best-effort, no parser).
_JS_PATTERNS = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
    ("arrow", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", re.M)),
]


def _py_symbols(text: str) -> list[dict[str, Any]]:
    tree = ast.parse(text)
    out: list[dict[str, Any]] = []

    def walk(node: ast.AST, parent: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                else:
                    base = "async function" if isinstance(child, ast.AsyncFunctionDef) else "function"
                    kind = "method" if parent else base
                out.append({
                    "name": child.name,
                    "kind": kind,
                    "line": child.lineno,
                    "end_line": getattr(child, "end_lineno", None),
                    "parent": parent,
                })
                walk(child, child.name if isinstance(child, ast.ClassDef) else parent)

    walk(tree, None)
    return out


def _js_symbols(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind, rx in _JS_PATTERNS:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append({"name": m.group(1), "kind": kind, "line": line, "end_line": None, "parent": None})
    out.sort(key=lambda s: s["line"])
    return out


def extract_symbols(file_path: str | Path) -> dict[str, Any]:
    """List the functions/classes defined in one Python or JS/TS file."""
    path = ensure_within(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    ext = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if ext in _PY_EXT:
        try:
            symbols = _py_symbols(text)
            lang = "python"
        except SyntaxError as e:
            return {"path": str(path), "language": "python", "error": f"syntax error: {e}", "symbols": []}
    elif ext in _JS_EXT:
        symbols = _js_symbols(text)
        lang = "javascript/typescript"
    else:
        return {"path": str(path), "language": "unsupported", "symbols": [], "error": f"unsupported extension: {ext}"}

    return {"path": str(path), "language": lang, "count": len(symbols), "symbols": symbols}
