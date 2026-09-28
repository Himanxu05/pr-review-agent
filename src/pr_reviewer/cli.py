"""
pr-review local  --repo . --base main
pr-review github https://github.com/owner/repo/pull/7 [--post]
pr-review serve  --port 8000
pr-review mcp    /path/to/repo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import get_settings
from .github_client import GitHubClient, parse_pr_url
from .llm import ConfigError
from .reviewer import review_github_pr, review_local, to_markdown
from .workspace import GitError


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="pr-review", description="AI code-review agent")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    loc = sub.add_parser("local", help="review the current branch against a base branch")
    loc.add_argument("--repo", type=Path, default=Path("."))
    loc.add_argument("--base", default="main")
    loc.add_argument("--json", action="store_true", help="print machine-readable JSON")

    gh = sub.add_parser("github", help="review a GitHub pull request")
    gh.add_argument("url")
    gh.add_argument("--post", action="store_true", help="post the review on the PR")
    gh.add_argument("--json", action="store_true")

    srv = sub.add_parser("serve", help="run the webhook server")
    srv.add_argument("--host", default="0.0.0.0")
    srv.add_argument("--port", type=int, default=8000)

    mcp = sub.add_parser("mcp", help="run the repo-tools MCP server over stdio")
    mcp.add_argument("repo", type=Path)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("pr_reviewer.app:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "mcp":
        from .mcp_server import build_server

        build_server(args.repo).run(transport="stdio")
        return 0

    try:
        result = asyncio.run(_review(args, p))
    except (ConfigError, GitError, ValueError, httpx.HTTPStatusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result.to_dict(), indent=2) if args.json else to_markdown(result))
    if result.errors and len(result.errors) >= max(1, len(result.files_reviewed)):
        return 3  # review incomplete: every file failed
    return 1 if any(f.severity == "critical" for f in result.comments) else 0


async def _review(args: argparse.Namespace, parser: argparse.ArgumentParser):
    if args.cmd == "local":
        return await review_local(args.repo, args.base)
    s = get_settings()
    if not s.github_token:
        parser.error("GITHUB_TOKEN is required for `github` (set it in .env)")
    owner, repo, number = parse_pr_url(args.url)
    async with GitHubClient(s.github_token, s.github_api_url) as client:
        return await review_github_pr(client, owner, repo, number, post=args.post, settings=s)


if __name__ == "__main__":
    sys.exit(main())
