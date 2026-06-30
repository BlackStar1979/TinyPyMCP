"""
TinyPyMCP - architecture digest (no execution). Collapses the file-level import
graph into a PACKAGE-level view (layering) plus per-package size/symbol counts,
entry points, dependency hot files and top externals. Built on deps.py; a compact
"understand this repo's shape" map (fewer tokens than the raw graph).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from src.code.deps import build_dependency_graph

_ENTRY_RE = re.compile(r"(^|/)(server|__main__|main|cli)\.py$", re.I)


def _pkg(rel: str) -> str:
    d = str(Path(rel).parent).replace("\\", "/")
    return d if d not in (".", "") else "(root)"


def architecture(path: str, recursive: bool = True, max_files: int = 1000, top_n: int = 12) -> dict[str, Any]:
    graph = build_dependency_graph(path, recursive, max_files)
    root = Path(graph["path"])

    fan_in: dict[str, int] = {}
    fan_out: dict[str, int] = {}
    for e in graph["edges"]:
        fan_out[e["from"]] = fan_out.get(e["from"], 0) + 1
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1

    packages: dict[str, dict[str, int]] = {}
    for n in graph["nodes"]:
        if not n.endswith(".py"):
            continue
        info = packages.setdefault(_pkg(n), {"modules": 0, "loc": 0, "functions": 0, "classes": 0})
        info["modules"] += 1
        try:
            src = (root / n).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        info["loc"] += src.count("\n") + 1
        try:
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info["functions"] += 1
                elif isinstance(node, ast.ClassDef):
                    info["classes"] += 1
        except SyntaxError:
            pass

    pkg_edges: dict[tuple[str, str], int] = {}
    for e in graph["edges"]:
        a, b = _pkg(e["from"]), _pkg(e["to"])
        if a != b:
            pkg_edges[(a, b)] = pkg_edges.get((a, b), 0) + 1

    entry = sorted({n for n in graph["nodes"] if n.endswith(".py")
                    and (_ENTRY_RE.search(n) or (fan_in.get(n, 0) == 0 and fan_out.get(n, 0) >= 3))})

    def _top(d: dict[str, int], k: int) -> list[dict[str, Any]]:
        return [{"file": x, "count": c} for x, c in sorted(d.items(), key=lambda kv: -kv[1])[:k]]

    return {
        "path": graph["path"],
        "modules": sum(p["modules"] for p in packages.values()),
        "total_loc": sum(p["loc"] for p in packages.values()),
        "packages": {p: packages[p] for p in sorted(packages)},
        "package_dependencies": [{"from": a, "to": b, "imports": c}
                                 for (a, b), c in sorted(pkg_edges.items(), key=lambda kv: -kv[1])],
        "entry_points": entry,
        "hot_files": {"top_fan_in": _top(fan_in, top_n), "top_fan_out": _top(fan_out, top_n)},
        "top_externals": [{"module": k, "count": v} for k, v in list(graph["externals"].items())[:top_n]],
        "edges_count": graph["edges_count"],
        "truncated": graph["truncated"],
    }
