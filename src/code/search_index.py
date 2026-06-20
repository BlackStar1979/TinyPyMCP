"""
TinyPyMCP - persistent search index (build_index / search_index).

An inverted index (token -> file/line) persisted in SQLite so repeated lookups
over a large repo are instant instead of re-grepping every time. Modeled on the
production MCP build_index/search_index. No code execution.

Index files live under <workspace>/.index/<hash>.db, one per indexed root.
Search returns lines containing ALL query tokens (AND), reading the matched
lines from disk on demand (the index stores only locations, not full text).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.exec.runner import WORKSPACE_ROOT
from src.utils.path_guard import ensure_within

_INDEX_DIR = WORKSPACE_ROOT / ".index"
_IGNORED = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "dist", "build", ".index",
}
_TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".md",
    ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".html", ".css",
    ".sh", ".go", ".rs", ".java", ".rb", ".php", ".sql",
}
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
MAX_FILES = 5000
MAX_LINE_LEN = 1000


def _index_path(root: Path) -> Path:
    h = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:16]
    return _INDEX_DIR / f"{h}.db"


def _tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN.finditer(text)}


def build_index(path: str, max_files: int = MAX_FILES) -> dict[str, Any]:
    """Build/refresh the inverted index for a directory tree."""
    root = ensure_within(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")
    max_files = max(1, min(int(max_files), MAX_FILES))

    _INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _index_path(root)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL);
        CREATE TABLE postings (token TEXT NOT NULL, file_id INTEGER NOT NULL, line_no INTEGER NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )

    start = time.monotonic()
    n_files = n_lines = n_postings = 0
    truncated = False

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _IGNORED for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in _TEXT_EXT:
            continue
        if n_files >= max_files:
            truncated = True
            break
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        cur = conn.execute("INSERT INTO files (path) VALUES (?)", (rel,))
        file_id = cur.lastrowid
        n_files += 1
        rows = []
        for i, line in enumerate(text.splitlines(), 1):
            n_lines += 1
            for tok in _tokenize(line[:MAX_LINE_LEN]):
                rows.append((tok, file_id, i))
        if rows:
            conn.executemany("INSERT INTO postings (token, file_id, line_no) VALUES (?,?,?)", rows)
            n_postings += len(rows)

    conn.execute("CREATE INDEX idx_postings_token ON postings(token)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('root', ?)", (str(root),))
    conn.commit()
    conn.close()

    return {
        "path": str(root),
        "index_path": str(db_path),
        "files_indexed": n_files,
        "lines_indexed": n_lines,
        "postings": n_postings,
        "truncated": truncated,
        "index_bytes": db_path.stat().st_size,
        "elapsed_s": round(time.monotonic() - start, 3),
    }


def index_status(path: str) -> dict[str, Any]:
    """Report whether an index exists for a directory and its stats — without
    rebuilding."""
    root = ensure_within(path)
    db_path = _index_path(root)
    if not db_path.exists():
        return {"path": str(root), "indexed": False}
    conn = sqlite3.connect(db_path)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        postings = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    finally:
        conn.close()
    st = db_path.stat()
    return {
        "path": str(root),
        "indexed": True,
        "index_path": str(db_path),
        "files": files,
        "postings": postings,
        "index_bytes": st.st_size,
        "built_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }


def search_index(path: str, query: str, limit: int = 50, context: int = 0) -> dict[str, Any]:
    """Search a previously built index for lines containing ALL query tokens.
    With context>0, each hit also carries that many lines before/after."""
    root = ensure_within(path)
    db_path = _index_path(root)
    if not db_path.exists():
        return {"path": str(root), "indexed": False, "message": "No index; run build_index first."}

    tokens = sorted(_tokenize(query))
    if not tokens:
        raise ValueError("query has no indexable tokens")
    limit = max(1, min(int(limit), 500))
    context = max(0, min(int(context), 20))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Intersect postings: lines (file_id, line_no) where every token appears.
    sets = []
    for tok in tokens:
        rows = conn.execute(
            "SELECT file_id, line_no FROM postings WHERE token = ?", (tok,)
        ).fetchall()
        sets.append({(r["file_id"], r["line_no"]) for r in rows})
    hits = set.intersection(*sets) if sets else set()

    file_paths = {r["id"]: r["path"] for r in conn.execute("SELECT id, path FROM files")}
    conn.close()

    ordered = sorted(hits, key=lambda x: (file_paths.get(x[0], ""), x[1]))[:limit]
    results = []
    line_cache: dict[int, list[str]] = {}
    for file_id, line_no in ordered:
        rel = file_paths.get(file_id, "")
        if file_id not in line_cache:
            try:
                line_cache[file_id] = (root / rel).read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                line_cache[file_id] = []
        lines = line_cache[file_id]
        text = lines[line_no - 1].strip()[:MAX_LINE_LEN] if 0 < line_no <= len(lines) else ""
        hit = {"file": rel, "line_no": line_no, "line": text}
        if context > 0 and lines:
            start = max(0, line_no - 1 - context)
            end = min(len(lines), line_no + context)
            hit["context"] = [
                {"line_no": i + 1, "line": lines[i][:MAX_LINE_LEN]}
                for i in range(start, end)
            ]
        results.append(hit)

    return {
        "path": str(root),
        "indexed": True,
        "query_tokens": tokens,
        "match_count": len(hits),
        "returned": len(results),
        "results": results,
    }
