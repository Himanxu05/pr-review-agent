"""Runs the reviewer on the cases in cases.py and scores it.

    python eval/run_eval.py
    python eval/run_eval.py --ablation        # also without critic / without tools
    python eval/run_eval.py --cases sql_injection missing_await

A bug counts as caught if there's a comment within LINE_TOLERANCE lines of it.
Any comment on a clean case is a false alarm. Results are saved to eval/results/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from cases import CASES, Case, prepare

from pr_reviewer.config import Settings
from pr_reviewer.reviewer import review_local

LINE_TOLERANCE = 2
CONFIGS = {
    "full": {},
    "no-critic": {"use_critic": False},
    "no-tools": {"max_tool_steps": 0},
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def build_repo(case: Case, root: Path) -> Path:
    repo = root / case.name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "eval@local")
    _git(repo, "config", "user.name", "eval")
    for path, text in case.base.items():
        (repo / path).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feature")
    for path, text in case.head.items():
        (repo / path).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", case.title)
    return repo


def score(case: Case, comments: list[dict]) -> dict:
    caught = [
        any(c["path"] == p and abs(c["line"] - line) <= LINE_TOLERANCE for c in comments)
        for p, line in case.expected
    ]
    on_target = [
        c for c in comments
        if any(c["path"] == p and abs(c["line"] - line) <= LINE_TOLERANCE
               for p, line in case.expected)
    ]
    return {"bugs": len(case.expected), "caught": sum(caught),
            "comments": len(comments), "on_target": len(on_target)}


async def run_config(name: str, overrides: dict, cases: list[Case], tmp: Path) -> dict:
    settings = Settings(**overrides)
    rows = []
    for case in cases:
        repo = build_repo(case, tmp / name)
        t0 = time.perf_counter()
        try:
            result = await review_local(repo, base="main", settings=settings)
            comments = [f.model_dump() for f in result.comments]
            usage, errors = result.usage, result.errors
        except Exception as exc:
            comments, usage, errors = [], {}, [str(exc)]
        row = {"case": case.name, "category": case.category,
               "needs_context": case.needs_context,
               "seconds": round(time.perf_counter() - t0, 2),
               "tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
               "tool_calls": 0, "errors": errors, "findings": comments,
               **score(case, comments)}
        if not errors:
            row["tool_calls"] = sum(1 for t in result.trace if "tool" in t)
        rows.append(row)
        mark = "clean" if not case.expected else f"{row['caught']}/{row['bugs']}"
        print(f"  [{name}] {case.name:<32} {mark:<6} comments={row['comments']} "
              f"{row['seconds']}s{'  ERROR' if errors else ''}", flush=True)
    return {"config": name, "rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    buggy = [r for r in rows if r["bugs"]]
    clean = [r for r in rows if not r["bugs"]]
    ctx = [r for r in buggy if r["needs_context"]]
    total_comments = sum(r["comments"] for r in rows)
    return {
        "detection_rate": sum(r["caught"] for r in buggy) / max(1, sum(r["bugs"] for r in buggy)),
        "cross_file_detection": sum(r["caught"] for r in ctx) / max(1, sum(r["bugs"] for r in ctx)),
        "precision": sum(r["on_target"] for r in rows) / total_comments if total_comments else 1.0,
        "false_alarms_on_clean": sum(r["comments"] for r in clean),
        "clean_cases": len(clean),
        "median_seconds": statistics.median(r["seconds"] for r in rows),
        "avg_tokens": round(statistics.mean(r["tokens"] for r in rows)),
        "avg_tool_calls": round(statistics.mean(r["tool_calls"] for r in rows), 1),
        "errors": sum(1 for r in rows if r["errors"]),
    }


def table(results: list[dict]) -> str:
    head = ("| Config | Bugs caught | Cross-file bugs | Precision | False alarms (clean PRs) "
            "| Median time | Avg tokens | Avg tool calls |\n|---|---|---|---|---|---|---|---|")
    lines = [head]
    for r in results:
        s = r["summary"]
        lines.append(
            f"| {r['config']} | {s['detection_rate']:.0%} | {s['cross_file_detection']:.0%} "
            f"| {s['precision']:.0%} | {s['false_alarms_on_clean']} on {s['clean_cases']} "
            f"| {s['median_seconds']:.1f}s | {s['avg_tokens']:,} | {s['avg_tool_calls']} |")
    return "\n".join(lines)


async def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ablation", action="store_true", help="compare full / no-critic / no-tools")
    ap.add_argument("--cases", nargs="*", help="only run these case names")
    args = ap.parse_args()

    cases = [prepare(c) for c in CASES if not args.cases or c.name in args.cases]
    configs = CONFIGS if args.ablation else {"full": {}}
    s = Settings()
    print(f"Model: {s.llm_provider}/{s.llm_model} | {len(cases)} cases | "
          f"configs: {', '.join(configs)}\n")

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, overrides in configs.items():
            (Path(tmp) / name).mkdir()
            results.append(await run_config(name, overrides, cases, Path(tmp)))

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"model": f"{s.llm_provider}/{s.llm_model}",
                               "results": results}, indent=2))
    print("\n" + table(results))
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
