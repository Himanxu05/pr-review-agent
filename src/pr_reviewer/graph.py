"""Review workflow built with LangGraph.

planner -> review_file (one per file, run in parallel) -> critic
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import prompts
from .config import Settings, get_settings
from .diff_parser import FileDiff
from .schemas import SEVERITY_ORDER, CriticReport, FilePlan, FileReview, Finding, ReviewPlan

log = logging.getLogger(__name__)

# PRs this small don't need an LLM call to decide what to review
SMALL_PR_FILES = 2
MAX_TOOL_OUTPUT = 6000
NO_MORE_TOOLS = ("You can't call any more tools. Answer now using what you've already seen, "
                 "in the requested format.")


@dataclass
class PRContext:
    title: str
    body: str
    files: list[FileDiff]
    repo: str = ""
    number: int | None = None
    head_sha: str | None = None

    def file(self, path: str) -> FileDiff | None:
        for f in self.files:
            if f.path == path:
                return f
        return None


def add_counts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


class ReviewState(TypedDict, total=False):
    pr: PRContext
    plan: ReviewPlan
    # these get appended to from the parallel review_file runs
    candidates: Annotated[list[Finding], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    usage: Annotated[dict[str, int], add_counts]
    comments: list[Finding]
    general_notes: list[Finding]
    summary: str


class FileTask(TypedDict):
    pr: PRContext
    plan: ReviewPlan
    file_plan: FilePlan


def count_usage(counts: dict[str, int], msg: Any) -> None:
    meta = getattr(msg, "usage_metadata", None) or {}
    counts["llm_calls"] = counts.get("llm_calls", 0) + 1
    for key in ("input_tokens", "output_tokens"):
        counts[key] = counts.get(key, 0) + int(meta.get(key) or 0)


def is_invalid_tool_call(exc: Exception) -> bool:
    # Groq returns this when the model calls a tool we didn't give it
    text = str(exc)
    return "tool_use_failed" in text or "which was not in request.tools" in text


async def structured_call(llm: BaseChatModel, schema: type, messages: list) -> dict:
    """with_structured_output, with one retry in json_schema mode.

    gpt-oss sometimes answers by calling a made-up tool (e.g. `json`) and the
    request fails. json_schema mode doesn't go through tool calling, so it's a
    good fallback.
    """
    try:
        out = await llm.with_structured_output(schema, include_raw=True).ainvoke(messages)
        if out["parsed"] is not None:
            return out
        first_error: Exception = ValueError(out.get("parsing_error"))
    except Exception as e:
        first_error = e

    log.info("structured output failed (%s), retrying with json_schema", first_error)
    retry = list(messages)
    if is_invalid_tool_call(first_error):
        retry.append(HumanMessage(NO_MORE_TOOLS))
    try:
        out = await llm.with_structured_output(
            schema, method="json_schema", include_raw=True).ainvoke(retry)
        if out["parsed"] is None:
            raise ValueError(out.get("parsing_error"))
        return out
    except Exception:
        # the first error is usually the more useful one
        raise first_error from None


def build_graph(llm: BaseChatModel, tools: list[BaseTool], settings: Settings | None = None):
    s = settings or get_settings()
    tools_by_name = {t.name: t for t in tools}

    async def planner(state: ReviewState) -> dict:
        pr = state["pr"]
        files = [f for f in pr.files if f.is_reviewable()]
        files.sort(key=lambda f: f.additions, reverse=True)
        review_all = ReviewPlan(
            files=[FilePlan(path=f.path, focus="General correctness and security")
                   for f in files[:s.max_files]],
            pr_summary=pr.title,
        )
        if len(files) <= SMALL_PR_FILES:
            return {"plan": review_all, "trace": [{"node": "planner", "skipped_llm": True}]}

        usage: dict[str, int] = {}
        file_list = "\n".join(f"- {f.path} ({f.status}, +{f.additions})" for f in files)
        preview = "\n\n".join(f"### {f.path}\n{f.annotated_patch(max_chars=1500)}"
                              for f in files[:8])
        try:
            out = await structured_call(llm, ReviewPlan, [
                SystemMessage(prompts.PLANNER_SYSTEM),
                HumanMessage(prompts.PLANNER_USER.format(
                    title=pr.title, body=pr.body or "(none)", file_list=file_list,
                    preview=preview)),
            ])
        except Exception as e:
            log.warning("planner failed, reviewing all files: %s", e)
            return {"plan": review_all, "errors": [f"planner: {e}"]}
        count_usage(usage, out["raw"])
        plan: ReviewPlan = out["parsed"]

        # the model sometimes invents paths, only keep ones that are really in the diff
        valid = {f.path for f in files}
        chosen: list[FilePlan] = []
        for fp in plan.files:
            if fp.path in valid and fp.path not in {c.path for c in chosen}:
                chosen.append(fp)
        if not chosen:
            return {"plan": review_all, "usage": usage}

        plan = ReviewPlan(files=chosen[:s.max_files], pr_summary=plan.pr_summary or pr.title)
        return {"plan": plan, "usage": usage,
                "trace": [{"node": "planner", "files": [f.path for f in plan.files]}]}

    def fan_out(state: ReviewState):
        plan = state["plan"]
        if not plan.files:
            return "critic"
        return [Send("review_file", {"pr": state["pr"], "plan": plan, "file_plan": fp})
                for fp in plan.files]

    async def review_file(task: FileTask) -> dict:
        pr, fp = task["pr"], task["file_plan"]
        fd = pr.file(fp.path)
        if fd is None:
            return {"errors": [f"{fp.path}: not in diff"]}

        usage: dict[str, int] = {}
        trace: list[dict] = []
        messages: list[Any] = [
            SystemMessage(prompts.REVIEWER_SYSTEM.format(max_steps=s.max_tool_steps)),
            HumanMessage(prompts.REVIEWER_USER.format(
                title=pr.title, pr_summary=task["plan"].pr_summary, focus=fp.focus,
                path=fd.path, status=fd.status,
                patch=fd.annotated_patch(max_chars=s.max_patch_chars))),
        ]
        steps = 0
        use_tools = bool(tools) and s.max_tool_steps > 0
        agent = llm.bind_tools(tools) if use_tools else llm

        # A failure here only drops this file, the rest of the PR still gets reviewed.
        try:
            while use_tools:
                try:
                    ai: AIMessage = await agent.ainvoke(messages)
                except Exception as e:
                    if not is_invalid_tool_call(e):
                        raise
                    # model tried to hand in its answer through a fake tool -> it's done
                    log.info("%s: model called an unknown tool, moving on", fd.path)
                    break
                count_usage(usage, ai)
                messages.append(ai)
                if not ai.tool_calls:
                    break
                # every tool call needs a matching ToolMessage or the next request fails
                for tc in ai.tool_calls:
                    steps += 1
                    tool = tools_by_name.get(tc["name"])
                    if tool is None:
                        result = f"Error: unknown tool '{tc['name']}'"
                    else:
                        try:
                            result = await tool.ainvoke(tc["args"])
                        except Exception as e:
                            result = f"Tool error: {e}"
                    trace.append({"node": "review_file", "file": fd.path,
                                  "tool": tc["name"], "args": tc["args"]})
                    messages.append(ToolMessage(content=str(result)[:MAX_TOOL_OUTPUT],
                                                tool_call_id=tc["id"]))
                if steps >= s.max_tool_steps:
                    break

            messages.append(HumanMessage(prompts.REVIEWER_FINALIZE.format(path=fd.path)))
            out = await structured_call(llm, FileReview, messages)
            count_usage(usage, out["raw"])
        except Exception as e:
            log.warning("review of %s failed: %s", fd.path, e)
            return {"errors": [f"{fd.path}: {e}"], "usage": usage, "trace": trace}

        findings = [f.model_copy(update={"path": fd.path}) for f in out["parsed"].findings]
        trace.append({"node": "review_file", "file": fd.path, "tool_calls": steps,
                      "findings": len(findings)})
        return {"candidates": findings, "usage": usage, "trace": trace}

    async def critic(state: ReviewState) -> dict:
        pr = state["pr"]
        plan = state.get("plan") or ReviewPlan(files=[], pr_summary=pr.title)
        anchored, notes = prefilter(pr, state.get("candidates", []), s.min_confidence)

        if not anchored and not notes:
            failed = len(state.get("errors", []))
            if failed and failed >= len(plan.files):
                summary = "Review could not be completed: every file failed (see errors)."
            elif failed:
                summary = (f"No blocking issues found, but {failed} file(s) could not be "
                           "reviewed (see errors).")
            else:
                summary = "No blocking issues found in the reviewed changes."
            return {"comments": [], "general_notes": [], "summary": summary}

        def by_severity(f: Finding):
            return (SEVERITY_ORDER[f.severity], -f.confidence)

        everything = anchored + notes
        if not s.use_critic:
            anchored.sort(key=by_severity)
            return {"comments": anchored[:s.max_comments], "general_notes": notes,
                    "summary": f"Found {len(everything)} potential issue(s)."}

        usage: dict[str, int] = {}
        listing = "\n".join(
            f"[{i}] {f.path}:{f.line} ({f.severity}/{f.category}) {f.title} -- {f.explanation}"
            for i, f in enumerate(everything))
        paths = {f.path for f in everything}
        excerpts = "\n\n".join(f"### {fd.path}\n{fd.annotated_patch(max_chars=3000)}"
                               for fd in pr.files if fd.path in paths)
        try:
            out = await structured_call(llm, CriticReport, [
                SystemMessage(prompts.CRITIC_SYSTEM),
                HumanMessage(prompts.CRITIC_USER.format(
                    title=pr.title, pr_summary=plan.pr_summary, findings=listing,
                    excerpts=excerpts)),
            ])
            count_usage(usage, out["raw"])
            report: CriticReport | None = out["parsed"]
        except Exception as e:
            # better to post the prefiltered findings than nothing
            log.warning("critic failed, keeping prefiltered findings: %s", e)
            report = None

        keep = set(range(len(everything)))
        if report is None:
            summary = f"Found {len(everything)} potential issue(s)."
        else:
            keep -= {v.index for v in report.verdicts if not v.keep}
            summary = report.summary

        kept_comments = sorted((f for i, f in enumerate(anchored) if i in keep), key=by_severity)
        kept_notes = [f for i, f in enumerate(notes, start=len(anchored)) if i in keep]
        return {
            "comments": kept_comments[:s.max_comments],
            "general_notes": kept_notes,
            "summary": summary,
            "usage": usage,
            "trace": [{"node": "critic", "candidates": len(everything),
                       "kept": len(kept_comments) + len(kept_notes)}],
        }

    g = StateGraph(ReviewState)
    g.add_node("planner", planner)
    g.add_node("review_file", review_file)
    g.add_node("critic", critic)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", fan_out, ["review_file", "critic"])
    g.add_edge("review_file", "critic")
    g.add_edge("critic", END)
    return g.compile()


def prefilter(pr: PRContext, candidates: list[Finding],
              min_confidence: float) -> tuple[list[Finding], list[Finding]]:
    """Cheap checks before the critic.

    Drops low-confidence and duplicate findings and moves each one onto a line
    GitHub will accept. Returns (inline, notes); notes are findings we couldn't
    place on a line, they go into the review body instead.
    """
    inline: list[Finding] = []
    notes: list[Finding] = []
    seen = set()
    for f in candidates:
        if f.confidence < min_confidence:
            continue
        fd = pr.file(f.path)
        line = fd.nearest_commentable(f.line) if fd else None
        key = (f.path, line, f.title.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        if line is None:
            notes.append(f)
        else:
            inline.append(f.model_copy(update={"line": line}))
    return inline, notes
