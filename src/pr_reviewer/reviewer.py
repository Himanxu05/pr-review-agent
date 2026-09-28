"""Run a review on a local branch or a GitHub PR, and format the result."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings, get_settings
from .diff_parser import parse_unified_diff
from .github_client import GitHubClient
from .graph import PRContext, build_graph
from .llm import get_chat_model
from .schemas import Finding
from .tracing import tracing_callbacks
from .workspace import checkout_pr, head_title, local_diff

log = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent.parent


@dataclass
class ReviewResult:
    summary: str
    comments: list[Finding]
    general_notes: list[Finding]
    files_reviewed: list[str]
    usage: dict[str, int]
    trace: list[dict[str, Any]]
    errors: list[str]
    elapsed_s: float
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["comments"] = [f.model_dump() for f in self.comments]
        d["general_notes"] = [f.model_dump() for f in self.general_notes]
        return d


@asynccontextmanager
async def mcp_repo_tools(repo_root: Path):
    # the server runs as a subprocess; make sure it can import this package
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, [str(SRC_DIR), os.environ.get("PYTHONPATH")]))}
    client = MultiServerMCPClient({
        "repo": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "pr_reviewer.mcp_server", str(repo_root)],
            "env": env,
        }
    })
    async with client.session("repo") as session:
        yield await load_mcp_tools(session)


async def run_review(
    pr: PRContext, repo_root: Path, llm: BaseChatModel | None = None,
    settings: Settings | None = None, tools: list[BaseTool] | None = None,
) -> ReviewResult:
    s = settings or get_settings()
    llm = llm or get_chat_model(s)
    start = time.perf_counter()

    async def invoke(tool_list: list[BaseTool]) -> dict:
        graph = build_graph(llm, tool_list, s)
        return await graph.ainvoke(
            {"pr": pr},
            config={"max_concurrency": s.max_concurrency, "callbacks": tracing_callbacks(),
                    "run_name": f"review {pr.repo}#{pr.number or 'local'}"},
        )

    if tools is not None:
        state = await invoke(tools)
    else:
        async with mcp_repo_tools(repo_root) as mcp_tools:
            state = await invoke(mcp_tools)

    plan = state.get("plan")
    return ReviewResult(
        summary=state.get("summary", ""),
        comments=state.get("comments", []),
        general_notes=state.get("general_notes", []),
        files_reviewed=[f.path for f in plan.files] if plan else [],
        usage=state.get("usage", {}),
        trace=state.get("trace", []),
        errors=state.get("errors", []),
        elapsed_s=round(time.perf_counter() - start, 2),
        meta={"repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha,
              "model": f"{s.llm_provider}/{s.llm_model}"},
    )


async def review_local(repo: Path, base: str = "main", settings: Settings | None = None,
                       llm: BaseChatModel | None = None) -> ReviewResult:
    repo = repo.resolve()
    files = parse_unified_diff(local_diff(repo, base))
    pr = PRContext(title=head_title(repo), body="", files=files, repo=repo.name)
    return await run_review(pr, repo, llm=llm, settings=settings)


async def review_github_pr(
    gh: GitHubClient, owner: str, repo: str, number: int, post: bool = False,
    settings: Settings | None = None,
) -> ReviewResult:
    pull = await gh.get_pull(owner, repo, number)
    files = await gh.list_files(owner, repo, number)
    pr = PRContext(title=pull["title"], body=pull.get("body") or "", files=files,
                   repo=f"{owner}/{repo}", number=number, head_sha=pull["head"]["sha"])
    with tempfile.TemporaryDirectory(prefix="pr-review-") as tmp:
        root = checkout_pr(owner, repo, number, gh.token, Path(tmp))
        result = await run_review(pr, root, settings=settings)
    if not post:
        return result
    if result.errors and len(result.errors) >= max(1, len(result.files_reviewed)):
        # don't spam the PR with an empty review when nothing actually got reviewed
        log.error("not posting, every file failed: %s", result.errors[0][:200])
        result.meta["posted"] = False
    else:
        await gh.create_review(
            owner, repo, number, commit_id=pr.head_sha,
            body=review_body(result),
            comments=[{"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)}
                      for f in result.comments],
        )
        result.meta["posted"] = True
        log.info("posted review on %s/%s#%s (%d comments)",
                 owner, repo, number, len(result.comments))
    return result


def comment_body(f: Finding) -> str:
    parts = [f"**{f.title}** ({f.severity}, {f.category})", "", f.explanation]
    if f.suggestion:
        parts += ["", "Suggested fix:", "```", f.suggestion.strip("\n"), "```"]
    return "\n".join(parts)


def review_body(r: ReviewResult) -> str:
    lines = ["### Automated review", "", r.summary, ""]
    if r.comments:
        counts: dict[str, int] = {}
        for f in r.comments:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        lines.append("Inline comments: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    if r.general_notes:
        lines += ["", "Other notes:"]
        lines += [f"- `{f.path}`: **{f.title}** - {f.explanation}" for f in r.general_notes]
    lines += ["", (f"<sub>{len(r.files_reviewed)} file(s) reviewed in {r.elapsed_s}s "
                   f"with {r.meta.get('model', '')}</sub>")]
    return "\n".join(lines)


def to_markdown(r: ReviewResult) -> str:
    out = [review_body(r), ""]
    for f in r.comments:
        out += [f"---\n**`{f.path}:{f.line}`**", comment_body(f), ""]
    if r.errors:
        out += ["---", "Errors:"] + [f"- {e}" for e in r.errors]
    return "\n".join(out)
