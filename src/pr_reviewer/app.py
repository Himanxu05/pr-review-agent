"""Webhook server for the GitHub App.

Reviews a PR when it's opened, gets new commits or is marked ready, and when
someone comments `/review`. GitHub gives up on a webhook after 10s, so we
answer 202 right away and do the review in a background task.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .config import get_settings
from .github_client import GitHubClient
from .reviewer import review_github_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pr_reviewer.app")

load_dotenv()

PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
REVIEW_COMMAND = "/review"

app = FastAPI(title="PR Review Agent", version="1.0.0")

# GitHub sometimes delivers the same event twice. Keyed by (repo, pr, head sha).
# TODO: move to redis if this ever runs with more than one instance
_reviewed: set[tuple[str, int, str]] = set()
_stats: dict[str, int] = {"reviews_started": 0, "reviews_failed": 0, "comments_posted": 0}


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def review_target(event: str, payload: dict[str, Any]) -> tuple[str, str, int, str | None] | None:
    """(owner, repo, number, head_sha) if the event should trigger a review, else None."""
    repo = payload.get("repository", {})
    owner, name = repo.get("owner", {}).get("login"), repo.get("name")
    if event == "pull_request":
        pr = payload.get("pull_request", {})
        if payload.get("action") not in PR_ACTIONS or pr.get("draft"):
            return None
        return owner, name, pr["number"], pr["head"]["sha"]
    if event == "issue_comment":
        issue = payload.get("issue", {})
        body = (payload.get("comment", {}).get("body") or "").strip()
        # ignore bots so we can never end up in a loop
        is_bot = payload.get("comment", {}).get("user", {}).get("type") == "Bot"
        if (payload.get("action") == "created" and "pull_request" in issue
                and body.startswith(REVIEW_COMMAND) and not is_bot):
            return owner, name, issue["number"], None
    return None


async def _run(owner: str, repo: str, number: int, installation_id: int) -> None:
    s = get_settings()
    _stats["reviews_started"] += 1
    try:
        gh = await GitHubClient.for_installation(
            s.github_app_id, s.github_private_key_path, installation_id, s.github_api_url)
        async with gh:
            result = await review_github_pr(gh, owner, repo, number, post=True, settings=s)
        _stats["comments_posted"] += len(result.comments)
        log.info("%s/%s#%d: %d comments, %s tokens, %.1fs", owner, repo, number,
                 len(result.comments), result.usage, result.elapsed_s)
    except Exception:
        _stats["reviews_failed"] += 1
        log.exception("review failed for %s/%s#%d", owner, repo, number)


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "model": f"{s.llm_provider}/{s.llm_model}",
            "app_configured": bool(s.github_app_id and s.github_private_key_path), **_stats}


@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(...),
    x_hub_signature_256: str | None = Header(None),
) -> dict:
    s = get_settings()
    body = await request.body()
    if not s.github_webhook_secret:
        raise HTTPException(500, "GITHUB_WEBHOOK_SECRET is not configured")
    if not verify_signature(s.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(401, "Invalid signature")

    if x_github_event == "ping":
        return {"status": "pong"}
    payload = await request.json()
    target = review_target(x_github_event, payload)
    if target is None:
        return {"status": "ignored"}

    owner, repo, number, sha = target
    if sha:
        key = (f"{owner}/{repo}", number, sha)
        if key in _reviewed:
            return {"status": "duplicate"}
        _reviewed.add(key)
    installation_id = payload.get("installation", {}).get("id")
    if not installation_id:
        raise HTTPException(400, "Missing installation id (is this a GitHub App webhook?)")

    background.add_task(_run, owner, repo, number, installation_id)
    return {"status": "queued", "pr": f"{owner}/{repo}#{number}"}
