"""
TinyPyMCP - static code audit (no execution): dead-code candidates, unused
imports, and size/complexity hot spots. Built on the existing dependency graph
(deps.build_dependency_graph). CANDIDATES ONLY — dynamic imports, decorator
registration and reflection cause false positives, so verify before removing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from src.code.deps import build_dependency_graph

# Files that are entry points / packaging / tests are never "dead" by fan-in.
_ENTRY_RE = re.compile(
    r"(^|/)(server|__main__|__init__|conftest|setup|main)\.py$"
    r"|(^|/)test_[^/]*\.py$|_test\.py$",
    re.I,
)


def _unused_imports(src: str) -> list[str]:
    """AST-based unused-import detection for one module (conservative)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                name = a.asname or a.name.split(".")[0]
                imported[name] = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                name = a.asname or a.name
                imported[name] = f"from {node.module or '.'} import {a.name}"
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    for el in node.value.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, str):
                            used.add(el.value)
    return [imported[n] for n in sorted(imported) if n not in used]


def audit(path: str, recursive: bool = True, max_files: int = 1000, top_n: int = 20) -> dict[str, Any]:
    """Audit a directory: dead-module candidates (zero fan-in, excluding entry/
    test/package files), unused imports per file, and the largest modules (LOC)."""
    graph = build_dependency_graph(path, recursive, max_files)
    root = Path(graph["path"])

    fan_in: dict[str, int] = {}
    for e in graph["edges"]:
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1

    dead = [n for n in graph["nodes"]
            if n.endswith(".py") and fan_in.get(n, 0) == 0
            and not _ENTRY_RE.search(n)
            and not (n.startswith("tests/") or "/tests/" in n)]  # test files have no fan-in by design

    sizes: list[tuple[str, int]] = []
    unused: dict[str, list[str]] = {}
    total_loc = 0
    for n in graph["nodes"]:
        if not n.endswith(".py"):
            continue
        try:
            src = (root / n).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        loc = src.count("\n") + 1
        total_loc += loc
        sizes.append((n, loc))
        if not n.endswith("__init__.py"):  # __init__ re-exports look "unused"
            u = _unused_imports(src)
            if u:
                unused[n] = u
    sizes.sort(key=lambda x: -x[1])

    return {
        "path": graph["path"],
        "modules": len([n for n in graph["nodes"] if n.endswith(".py")]),
        "total_loc": total_loc,
        "dead_module_candidates": dead,
        "dead_count": len(dead),
        "unused_imports": unused,
        "unused_import_files": len(unused),
        "largest_modules": [{"file": f, "loc": l} for f, l in sizes[:top_n]],
        "note": ("CANDIDATES ONLY — verify before removing. Dynamic imports, "
                 "decorator/registry patterns and reflection cause false positives; "
                 "fan-in is from STATIC imports within the scanned set only."),
    }
