"""Get the PR's code onto disk so the tools can read it."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, secret: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                          check=False)
    if proc.returncode != 0:
        msg = proc.stderr.strip()
        if secret:
            msg = msg.replace(secret, "***")
        raise GitError(f"git {args[0]} failed: {msg[:500]}")
    return proc.stdout


def checkout_pr(owner: str, repo: str, number: int, token: str | None, dest: Path) -> Path:
    """Shallow fetch of refs/pull/<n>/head into dest.

    pull/<n>/head lives on the base repo, so this also works for PRs from forks.
    The token goes in a header instead of the URL so it doesn't end up in
    .git/config or in error output.
    """
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], dest)
    _git(["remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"], dest)
    fetch = ["fetch", "-q", "--depth", "1", "origin", f"pull/{number}/head"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        fetch = ["-c", f"http.extraheader=Authorization: Basic {basic}", *fetch]
    _git(fetch, dest, secret=token)
    _git(["checkout", "-q", "FETCH_HEAD"], dest)
    return dest


def local_diff(repo: Path, base: str) -> str:
    # three dots = only the changes made on this branch, same as a PR would show
    return _git(["diff", "--no-color", "--no-ext-diff", f"{base}...HEAD"], repo)


def head_title(repo: Path) -> str:
    return _git(["log", "-1", "--format=%s"], repo).strip()
