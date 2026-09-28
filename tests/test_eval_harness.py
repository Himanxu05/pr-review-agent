import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from cases import CASES, Case, prepare
from run_eval import score, summarize


def test_markers_are_stripped_and_recorded():
    case = prepare(Case(name="x", title="t", base={}, head={"a.py": "ok\nbad()  # <BUG>\n"}))
    assert case.head["a.py"] == "ok\nbad()\n"
    assert case.expected == [("a.py", 2)]


def test_every_buggy_case_has_a_marker():
    for c in CASES:
        c = prepare(Case(**{**c.__dict__, "expected": []}))
        assert bool(c.expected) == (c.category != "clean"), c.name


def test_score_uses_line_tolerance():
    case = Case(name="x", title="t", base={}, head={}, expected=[("a.py", 10)])
    s = score(case, [{"path": "a.py", "line": 12}, {"path": "a.py", "line": 30}])
    assert s == {"bugs": 1, "caught": 1, "comments": 2, "on_target": 1}


def test_summary_counts_false_alarms_on_clean_cases():
    rows = [
        {"bugs": 1, "caught": 1, "comments": 1, "on_target": 1, "needs_context": True,
         "seconds": 2, "tokens": 100, "tool_calls": 2, "errors": []},
        {"bugs": 0, "caught": 0, "comments": 1, "on_target": 0, "needs_context": False,
         "seconds": 4, "tokens": 300, "tool_calls": 0, "errors": []},
    ]
    s = summarize(rows)
    assert s["detection_rate"] == 1.0 and s["false_alarms_on_clean"] == 1
    assert s["precision"] == 0.5 and s["median_seconds"] == 3
