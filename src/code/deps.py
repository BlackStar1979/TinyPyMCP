"""
TinyPyMCP - bounded code dependency analysis (no code execution).

Static import-graph builder for Python and JS/TS, modeled on the production
MCP code_dependencies/code_impact tools. Parses imports with regex (never
imports/executes the target code), resolves LOCAL imports to files in the
scanned set, and counts external modules separately.

Used for "study a foreign repo": understand structure, find hot files
(fan-in/out), and trace what a change to one file impacts — to a given depth.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.utils.path_guard import ensure_within

_IGNORED = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "dist", "build",
}
_PY_EXT = {".py"}
_JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_JS_RESOLVE_EXT = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json"]

# Python
_RE_PY_IMPORT = re.compile(r"^\s*import\s+([\w\.]+(?:\s*,\s*[\w\.]+)*)", re.M)
_RE_PY_FROM = re.compile(r"^\s*from\s+(\.*[\w\.]*)\s+import\s+", re.M)
# Capture the imported NAMES too, so `from pkg import submodule` produces an edge
# to pkg/submodule.py (not just pkg/__init__.py). Single-line imports only.
_RE_PY_FROM_FULL = re.compile(r"^\s*from\s+(\.*[\w\.]*)\s+import\s+(.+)$", re.M)
_RE_NAME = re.compile(r"^\w+$")
# JS/TS
_RE_JS_FROM = re.compile(r"""\bfrom\s+['"]([^'"]+)['"]""")
_RE_JS_REQUIRE = re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)""")
_RE_JS_IMPORT_CALL = re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)""")
_RE_JS_BARE_IMPORT = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.M)


def _iter_files(root: Path, recursive: bool, max_files: int) -> list[Path]:
    out: list[Path] = []
    globber = root.rglob if recursive else root.glob
    for p in globber("*"):
        if not p.is_file():
            continue
        if any(part in _IGNORED for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in _PY_EXT or p.suffix.lower() in _JS_EXT:
            out.append(p)
            if len(out) >= max_files:
                break
    return out


def _raw_imports(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    specs: list[str] = []
    if path.suffix.lower() in _PY_EXT:
        for m in _RE_PY_IMPORT.finditer(text):
            for part in m.group(1).split(","):
                specs.append(part.strip().split(" as ")[0].strip())
        for m in _RE_PY_FROM_FULL.finditer(text):
            mod = m.group(1).strip()
            specs.append(mod)  # the package itself (e.g. resolves to __init__.py)
            names = m.group(2).split("#")[0].strip().strip("()")
            for nm in names.split(","):
                nm = nm.strip().split(" as ")[0].strip()
                if nm and nm != "*" and _RE_NAME.match(nm):
                    sep = "" if mod.endswith(".") else "."
                    specs.append(f"{mod}{sep}{nm}")  # try pkg.submodule -> file
    else:
        for rx in (_RE_JS_FROM, _RE_JS_REQUIRE, _RE_JS_IMPORT_CALL, _RE_JS_BARE_IMPORT):
            specs += [m.group(1) for m in rx.finditer(text)]
    return [s for s in specs if s]


def _resolve_js(spec: str, src: Path, fileset: set[Path]) -> Path | None:
    if not spec.startswith("."):
        return None  # external
    base = (src.parent / spec).resolve()
    candidates = [base] + [base.with_name(base.name + e) for e in _JS_RESOLVE_EXT]
    candidates += [base / ("index" + e) for e in _JS_RESOLVE_EXT]
    for c in candidates:
        if c in fileset:
            return c
    return None


def _resolve_py(spec: str, src: Path, root: Path, fileset: set[Path]) -> Path | None:
    if spec.startswith("."):
        dots = len(spec) - len(spec.lstrip("."))
        rest = spec[dots:]
        anchor = src.parent
        for _ in range(dots - 1):
            anchor = anchor.parent
        rel = rest.replace(".", "/")
        base = (anchor / rel).resolve() if rest else anchor.resolve()
    else:
        base = (root / spec.replace(".", "/")).resolve()
    for c in (base.with_suffix(".py"), base / "__init__.py"):
        if c in fileset:
            return c
    return None


def build_dependency_graph(
    path: str,
    recursive: bool = True,
    max_files: int = 500,
) -> dict[str, Any]:
    """Build a bounded import dependency graph for a directory."""
    root = ensure_within(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    max_files = max(1, min(int(max_files), 5000))

    files = _iter_files(root, recursive, max_files)
    truncated = len(files) >= max_files
    fileset = {f.resolve() for f in files}

    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            return str(p)

    edges: list[tuple[str, str]] = []
    externals: dict[str, int] = {}
    unresolved: list[dict[str, str]] = []

    for f in files:
        fr = f.resolve()
        for spec in _raw_imports(f):
            if f.suffix.lower() in _PY_EXT:
                target = _resolve_py(spec, fr, root, fileset)
                is_local_looking = spec.startswith(".") or (root / spec.replace(".", "/")).with_suffix(".py").exists()
            else:
                target = _resolve_js(spec, fr, fileset)
                is_local_looking = spec.startswith(".")
            if target is not None:
                edges.append((rel(fr), rel(target)))
            elif is_local_looking:
                unresolved.append({"file": rel(fr), "import": spec})
            else:
                externals[spec] = externals.get(spec, 0) + 1

    nodes = sorted(rel(f) for f in files)
    return {
        "path": str(root),
        "recursive": recursive,
        "max_files": max_files,
        "truncated": truncated,
        "nodes": nodes,
        "nodes_count": len(nodes),
        "edges": [{"from": a, "to": b} for a, b in edges],
        "edges_count": len(edges),
        "externals": dict(sorted(externals.items(), key=lambda kv: -kv[1])),
        "unresolved": unresolved,
        "unresolved_count": len(unresolved),
    }


def summarize_graph(graph: dict[str, Any], top_n: int = 20) -> dict[str, Any]:
    """Fan-in / fan-out summary — the 'hot files' matrix."""
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}
    for e in graph["edges"]:
        fan_out[e["from"]] = fan_out.get(e["from"], 0) + 1
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1
    top = lambda d: [{"file": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:top_n]]
    return {
        "top_fan_in": top(fan_in),
        "top_fan_out": top(fan_out),
        "top_external": [{"module": k, "count": v} for k, v in list(graph["externals"].items())[:top_n]],
        "unresolved_count": graph["unresolved_count"],
    }


def analyze_impact(
    graph: dict[str, Any],
    target: str,
    direction: str = "both",
    max_depth: int = 5,
) -> dict[str, Any]:
    """Trace dependents and/or dependencies of one file to a given depth (BFS)."""
    target = target.replace("\\", "/")
    fwd: dict[str, set[str]] = {}   # file -> things it imports
    rev: dict[str, set[str]] = {}   # file -> things that import it
    for e in graph["edges"]:
        fwd.setdefault(e["from"], set()).add(e["to"])
        rev.setdefault(e["to"], set()).add(e["from"])
    if target not in graph["nodes"]:
        return {"target": target, "found": False}

    def bfs(adj: dict[str, set[str]]) -> list[dict[str, Any]]:
        seen = {target: 0}
        frontier = [target]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            nxt = []
            for node in frontier:
                for n in adj.get(node, ()):
                    if n not in seen:
                        seen[n] = depth
                        nxt.append(n)
            frontier = nxt
        return [{"file": k, "depth": v} for k, v in sorted(seen.items(), key=lambda kv: kv[1]) if k != target]

    result: dict[str, Any] = {"target": target, "found": True, "direction": direction, "max_depth": max_depth}
    if direction in ("both", "dependencies"):
        deps = bfs(fwd)
        result["dependencies"] = deps
        result["dependencies_count"] = len(deps)
    if direction in ("both", "dependents"):
        dependents = bfs(rev)
        result["dependents"] = dependents
        result["affected_count"] = len(dependents)
    return result
