"""Small async wrapper around the parts of the GitHub API we need."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Self

import httpx
import jwt

from .diff_parser import FileDiff, parse_patch

log = logging.getLogger(__name__)

PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a GitHub pull request URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self.token = token
        self._http = httpx.AsyncClient(
            base_url=api_url,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-reviewer-agent",
            },
        )

    @classmethod
    async def for_installation(
        cls, app_id: str, private_key_path: Path, installation_id: int,
        api_url: str = "https://api.github.com",
    ) -> GitHubClient:
        # app JWT -> installation token (valid for an hour)
        token = app_jwt(app_id, Path(private_key_path).read_text())
        async with httpx.AsyncClient(base_url=api_url, timeout=30) as http:
            r = await http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            return cls(r.json()["token"], api_url)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get_pull(self, owner: str, repo: str, number: int) -> dict:
        r = await self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    async def list_files(self, owner: str, repo: str, number: int) -> list[FileDiff]:
        files: list[FileDiff] = []
        for page in range(1, 31):  # the API stops at 3000 files anyway
            r = await self._http.get(
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            for f in batch:
                # `patch` is missing for binary or very large files
                files.append(parse_patch(f["filename"], f.get("patch") or "", f["status"]))
            if len(batch) < 100:
                break
        return files

    async def create_review(
        self, owner: str, repo: str, number: int, commit_id: str, body: str,
        comments: list[dict],
    ) -> dict:
        payload = {"commit_id": commit_id, "body": body, "event": "COMMENT",
                   "comments": comments}
        r = await self._http.post(f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload)
        if r.status_code == 422 and comments:
            # one bad line number and GitHub rejects the whole review,
            # so fall back to putting everything in the body
            log.warning("inline comments rejected (%s); posting summary only", r.text[:300])
            payload["comments"] = []
            payload["body"] = body + "\n\n" + "\n\n".join(
                f"**`{c['path']}:{c['line']}`**\n{c['body']}" for c in comments)
            r = await self._http.post(
                f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload)
        r.raise_for_status()
        return r.json()
