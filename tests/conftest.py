"""Shared test helpers: a scripted fake chat model (no API key or network needed)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pr_reviewer.config import Settings


class FakeLLM:
    """Duck-types the parts of a LangChain chat model the graph uses.

    - agent calls: first turn asks for `read_file` on the file under review,
      the next turn (after the tool result) stops.
    - structured calls are answered by `structured(schema, messages)`.
    """

    def __init__(self, structured: Callable[[type, list], Any], use_tools: bool = True):
        self.structured = structured
        self.use_tools = use_tools
        self.tool_calls_made: list[dict] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        if self.use_tools and not isinstance(messages[-1], ToolMessage):
            path = _path_in(messages)
            call = {"name": "read_file", "args": {"path": path}, "id": f"call_{len(self.tool_calls_made)}"}
            self.tool_calls_made.append(call)
            return AIMessage(content="Let me read the whole file.", tool_calls=[call],
                             usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
        return AIMessage(content="I have enough context.",
                         usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55})

    def with_structured_output(self, schema, include_raw=False, method=None):
        outer = self

        class _Structured:
            async def ainvoke(self, messages):
                parsed = outer.structured(schema, messages)
                raw = AIMessage(content="", usage_metadata={
                    "input_tokens": 10, "output_tokens": 10, "total_tokens": 20})
                return {"raw": raw, "parsed": parsed, "parsing_error": None} if include_raw else parsed

        return _Structured()


def _path_in(messages) -> str:
    for m in messages:
        text = str(getattr(m, "content", ""))
        if "File: " in text:
            return text.split("File: ", 1)[1].split(" ", 1)[0]
    return "."


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, llm_provider="groq", min_confidence=0.6, max_tool_steps=3,
                    max_comments=10, github_webhook_secret="s3cret")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A repo with `main` and a `feature` branch that introduces a bug."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("def average(xs):\n    return sum(xs) / len(xs)\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    git(repo, "checkout", "-qb", "feature")
    (repo / "calc.py").write_text(
        "def average(xs):\n    return sum(xs) / len(xs)\n\n\n"
        "def last(xs):\n    return xs[len(xs)]\n"
    )
    git(repo, "commit", "-qam", "Add last() helper")
    return repo
