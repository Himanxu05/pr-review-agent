from pr_reviewer.diff_parser import parse_patch
from pr_reviewer.graph import PRContext, prefilter
from pr_reviewer.reviewer import review_local, to_markdown
from pr_reviewer.schemas import CriticReport, FileReview, Finding, ReviewPlan, Verdict

from .conftest import FakeLLM


def finding(line, conf=0.9, title="Index out of range", path="calc.py", sev="high"):
    return Finding(path=path, line=line, severity=sev, category="bug", title=title,
                   explanation="xs[len(xs)] is always out of bounds.",
                   suggestion="return xs[-1]", confidence=conf)


def test_prefilter_confidence_anchoring_and_dedup():
    fd = parse_patch("calc.py", "@@ -1,2 +1,6 @@\n a\n b\n+\n+\n+def last(xs):\n+    return xs[len(xs)]\n")
    pr = PRContext(title="t", body="", files=[fd])
    anchored, notes = prefilter(pr, [
        finding(6), finding(6),              # duplicate
        finding(6, conf=0.3, title="meh"),   # low confidence
        finding(8, title="near"),            # snaps to line 6
        finding(99, title="far away"),       # cannot anchor -> general note
    ], min_confidence=0.6)
    assert [(f.line, f.title) for f in anchored] == [(6, "Index out of range"), (6, "near")]
    assert [f.title for f in notes] == ["far away"]


async def test_end_to_end_with_mcp_tools(git_repo, settings):
    """Real LangGraph + real MCP server over stdio + real git diff; only the LLM is faked."""

    def structured(schema, messages):
        if schema is FileReview:
            return FileReview(findings=[
                finding(6),
                finding(6, conf=0.9, title="Style nit", sev="low"),
            ])
        if schema is CriticReport:
            return CriticReport(verdicts=[Verdict(index=1, keep=False, reason="style")],
                                summary="One real bug.")
        if schema is ReviewPlan:
            raise AssertionError("planner LLM should be skipped for a 1-file PR")

    llm = FakeLLM(structured)
    result = await review_local(git_repo, base="main", settings=settings, llm=llm)

    assert result.files_reviewed == ["calc.py"]
    assert [(f.path, f.line, f.title) for f in result.comments] == [
        ("calc.py", 6, "Index out of range")]
    assert result.summary == "One real bug."
    assert result.errors == []
    # the agent really called the MCP read_file tool and got file content back
    tool_steps = [t for t in result.trace if t.get("tool") == "read_file"]
    assert tool_steps and tool_steps[0]["args"] == {"path": "calc.py"}
    assert result.usage["llm_calls"] == 4  # 2 agent turns + finalize + critic
    assert "Index out of range" in to_markdown(result)


async def test_failure_in_one_file_does_not_fail_review(git_repo, settings):
    def structured(schema, messages):
        if schema is FileReview:
            raise RuntimeError("rate limited")
        return CriticReport(verdicts=[], summary="x")

    result = await review_local(git_repo, base="main", settings=settings,
                                llm=FakeLLM(structured, use_tools=False))
    assert result.comments == []
    assert any("rate limited" in e for e in result.errors)
    assert "could not be completed" in result.summary  # never claim "no issues" on failure


async def test_planner_ignores_hallucinated_and_skipped_paths(settings):
    from pr_reviewer.graph import build_graph
    from pr_reviewer.schemas import FilePlan

    files = [parse_patch(p, "@@ -0,0 +1 @@\n+x = 1\n", "added")
             for p in ("a.py", "b.py", "c.py", "poetry.lock")]
    pr = PRContext(title="t", body="", files=files)
    seen_plan_prompt = []

    def structured(schema, messages):
        if schema is ReviewPlan:
            seen_plan_prompt.append(messages[-1].content)
            return ReviewPlan(pr_summary="adds files", files=[
                FilePlan(path="b.py", focus="check x"),
                FilePlan(path="ghost.py", focus="does not exist"),
                FilePlan(path="poetry.lock", focus="lockfile"),
            ])
        if schema is FileReview:
            return FileReview()
        return CriticReport(verdicts=[], summary="clean")

    graph = build_graph(FakeLLM(structured, use_tools=False), [], settings)
    state = await graph.ainvoke({"pr": pr})
    assert [f.path for f in state["plan"].files] == ["b.py"]
    assert "poetry.lock" not in seen_plan_prompt[0]
    assert state["summary"] == "No blocking issues found in the reviewed changes."


async def test_structured_call_retries_in_json_schema_mode():
    from langchain_core.messages import AIMessage

    from pr_reviewer.graph import structured_call

    calls = []

    class Flaky:
        def with_structured_output(self, schema, include_raw=False, method=None):
            class S:
                async def ainvoke(self, messages):
                    calls.append(method)
                    if method is None:
                        raise RuntimeError("attempted to call tool 'json'")
                    return {"raw": AIMessage(content=""), "parsed": FileReview(),
                            "parsing_error": None}
            return S()

    out = await structured_call(Flaky(), FileReview, [])
    assert calls == [None, "json_schema"] and out["parsed"] == FileReview()


async def test_agent_loop_survives_made_up_tool_call(git_repo, settings):
    class FakeToolNameLLM(FakeLLM):
        async def ainvoke(self, messages):
            raise RuntimeError("Error code: 400 - tool_use_failed: attempted to call tool 'json' "
                               "which was not in request.tools")

    def structured(schema, messages):
        if schema is FileReview:
            return FileReview(findings=[finding(6)])
        return CriticReport(verdicts=[], summary="ok")

    result = await review_local(git_repo, base="main", settings=settings,
                                llm=FakeToolNameLLM(structured))
    assert result.errors == [] and [f.line for f in result.comments] == [6]


async def test_structured_call_tells_model_tools_are_gone():
    from langchain_core.messages import AIMessage

    from pr_reviewer.graph import NO_MORE_TOOLS, structured_call

    seen = []

    class WantsTools:
        def with_structured_output(self, schema, include_raw=False, method=None):
            class S:
                async def ainvoke(self, messages):
                    seen.append(messages[-1].content if messages else None)
                    if method is None:
                        raise RuntimeError("tool_use_failed: attempted to call tool 'read_file'")
                    return {"raw": AIMessage(content=""), "parsed": FileReview(),
                            "parsing_error": None}
            return S()

    from langchain_core.messages import HumanMessage
    await structured_call(WantsTools(), FileReview, [HumanMessage("review")])
    assert seen == ["review", NO_MORE_TOOLS]
