"""MCP server with read-only tools for looking around a repo.

The reviewer starts this over stdio. It also works on its own with any MCP
client (Claude Desktop, Cursor, ...):

    python -m pr_reviewer.mcp_server /path/to/repo
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

MAX_READ_LINES = 300
MAX_MATCHES = 40
MAX_FILE_BYTES = 1_000_000
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


class RepoTools:
    """Plain-Python implementation, kept separate so it is easy to unit test."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"Repository root does not exist: {self.root}")

    # -- helpers -----------------------------------------------------------
    def _safe_path(self, rel: str) -> Path:
        """Resolve a user/LLM-supplied path and refuse anything outside root."""
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"Path escapes repository root: {rel}")
        return p

    def _iter_files(self, glob: str | None = None):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(self.root).as_posix()
                if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(name, glob):
                    continue
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield rel, full

    # -- tools -------------------------------------------------------------
    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._safe_path(path)
        if not p.is_file():
            return f"Error: file not found: {path}"
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines)" if end < len(lines) else ""
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}{more}"

    def search_code(self, pattern: str, glob: str | None = None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"
        hits: list[str] = []
        for rel, full in self._iter_files(glob):
            try:
                text = full.read_text(errors="strict")
            except (UnicodeDecodeError, OSError):
                continue  # binary file
            for no, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{rel}:{no}: {line.strip()[:200]}")
                    if len(hits) >= MAX_MATCHES:
                        return "\n".join(hits) + f"\n... (stopped at {MAX_MATCHES} matches)"
        return "\n".join(hits) or "No matches."

    def find_definition(self, symbol: str) -> str:
        name = re.escape(symbol)
        pattern = (
            rf"^\s*(async\s+def|def|class)\s+{name}\b"  # Python
            rf"|\b(function|class|interface|type)\s+{name}\b"  # JS/TS
            rf"|\b(const|let|var)\s+{name}\s*="
            rf"|^\s*{name}\s*="  # module-level assignment
        )
        return self.search_code(pattern)

    def list_directory(self, path: str = ".") -> str:
        p = self._safe_path(path)
        if not p.is_dir():
            return f"Error: not a directory: {path}"
        entries = sorted(
            (e.name + ("/" if e.is_dir() else "")
             for e in p.iterdir() if e.name not in IGNORED_DIRS),
            key=str.lower,
        )
        return "\n".join(entries) or "(empty)"

    def run_linter(self, path: str) -> str:
        p = self._safe_path(path)
        if not p.is_file():
            return f"Error: file not found: {path}"
        if p.suffix != ".py":
            return f"No linter configured for '{p.suffix}' files."
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format=json",
             "--select=E9,F,B,S", "--isolated", str(p)],
            capture_output=True, text=True, timeout=60, cwd=self.root, check=False,
        )
        try:
            issues = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return f"Linter failed: {proc.stderr.strip()[:500]}"
        if not issues:
            return "Ruff: no issues found."
        return "\n".join(
            f"{path}:{i['location']['row']}: {i['code']} {i['message']}" for i in issues[:30]
        )


def build_server(root: str | Path) -> FastMCP:
    tools = RepoTools(root)
    mcp = FastMCP("repo-tools", log_level="WARNING")

    @mcp.tool()
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a file from the repository (at the PR's head commit) with line numbers.
        Use it to see full context around a change: imports, the rest of a function, callers."""
        return tools.read_file(path, start_line, end_line)

    @mcp.tool()
    def search_code(pattern: str, glob: str | None = None) -> str:
        """Regex search across the repository. Returns 'path:line: text' matches.
        Optional glob narrows files, e.g. '*.py' or 'src/**'. Use it to find callers/usages."""
        return tools.search_code(pattern, glob)

    @mcp.tool()
    def find_definition(symbol: str) -> str:
        """Find where a function, class or variable is defined in the repository."""
        return tools.find_definition(symbol)

    @mcp.tool()
    def list_directory(path: str = ".") -> str:
        """List files and folders in a repository directory."""
        return tools.list_directory(path)

    @mcp.tool()
    def run_linter(path: str) -> str:
        """Run a static analyser (ruff: pyflakes, bugbear, bandit security rules) on a Python
        file. Useful to confirm undefined names, unused variables or insecure calls."""
        return tools.run_linter(path)

    return mcp


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REVIEW_REPO_ROOT", ".")
    build_server(root).run(transport="stdio")


if __name__ == "__main__":
    main()
