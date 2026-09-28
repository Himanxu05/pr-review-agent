import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from pr_reviewer import app as app_module
from pr_reviewer.config import Settings


def sign(body: bytes, secret: str = "s3cret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "get_settings",
                        lambda: Settings(_env_file=None, github_webhook_secret="s3cret"))
    calls = []

    async def fake_run(owner, repo, number, installation_id):
        calls.append((owner, repo, number, installation_id))

    monkeypatch.setattr(app_module, "_run", fake_run)
    app_module._reviewed.clear()
    c = TestClient(app_module.app)
    c.calls = calls
    return c


def pr_event(action="opened", draft=False, sha="abc"):
    return {
        "action": action, "installation": {"id": 42},
        "repository": {"name": "demo", "owner": {"login": "me"}},
        "pull_request": {"number": 7, "draft": draft, "head": {"sha": sha}},
    }


def post(client, event, payload, secret="s3cret"):
    body = json.dumps(payload).encode()
    return client.post("/webhook", content=body, headers={
        "X-GitHub-Event": event, "X-Hub-Signature-256": sign(body, secret),
        "Content-Type": "application/json"})


def test_rejects_bad_signature(client):
    assert post(client, "pull_request", pr_event(), secret="wrong").status_code == 401


def test_ping(client):
    assert post(client, "ping", {"zen": "hi"}).json() == {"status": "pong"}


def test_opened_pr_is_queued_once(client):
    assert post(client, "pull_request", pr_event()).json()["status"] == "queued"
    assert post(client, "pull_request", pr_event()).json()["status"] == "duplicate"
    assert client.calls == [("me", "demo", 7, 42)]


def test_new_commit_is_reviewed_again(client):
    post(client, "pull_request", pr_event(sha="a1"))
    post(client, "pull_request", pr_event(action="synchronize", sha="b2"))
    assert len(client.calls) == 2


def test_draft_and_closed_are_ignored(client):
    assert post(client, "pull_request", pr_event(draft=True)).json()["status"] == "ignored"
    assert post(client, "pull_request", pr_event(action="closed")).json()["status"] == "ignored"
    assert client.calls == []


def test_review_command_comment(client):
    payload = {
        "action": "created", "installation": {"id": 42},
        "repository": {"name": "demo", "owner": {"login": "me"}},
        "issue": {"number": 7, "pull_request": {}},
        "comment": {"body": "/review please", "user": {"type": "User"}},
    }
    assert post(client, "issue_comment", payload).json()["status"] == "queued"
    payload["comment"]["user"]["type"] = "Bot"
    assert post(client, "issue_comment", payload).json()["status"] == "ignored"


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
