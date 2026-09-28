"""Structured-output schemas shared by the graph nodes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal[
    "bug", "security", "performance", "error-handling", "concurrency", "maintainability"
]
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class FilePlan(BaseModel):
    path: str = Field(description="Exact path of a changed file")
    focus: str = Field(description="What the reviewer should pay most attention to in this file")


class ReviewPlan(BaseModel):
    """Planner output: which changed files deserve a review, and why."""

    files: list[FilePlan] = Field(description="Files to review, most important first")
    pr_summary: str = Field(description="One or two sentences on what this PR does")


class Finding(BaseModel):
    path: str = Field(description="File path the issue is in")
    line: int = Field(description="Line number in the NEW version of the file (left gutter)")
    severity: Severity
    category: Category
    title: str = Field(description="Short headline, under 80 characters")
    explanation: str = Field(description="Why this is a real problem and when it breaks")
    suggestion: str | None = Field(default=None, description="Corrected code, if short")
    confidence: float = Field(ge=0.0, le=1.0, description="How sure you are this is a real defect")


class FileReview(BaseModel):
    """Reviewer output for one file. Empty list if the change looks correct."""

    findings: list[Finding] = Field(default_factory=list)


class Verdict(BaseModel):
    index: int = Field(description="Index of the finding being judged")
    keep: bool = Field(description="True only if it is a real, actionable defect")
    reason: str = Field(description="One sentence justifying the decision")


class CriticReport(BaseModel):
    """Critic output: a verdict for every candidate finding, plus the review summary."""

    verdicts: list[Verdict]
    summary: str = Field(description="2-4 sentence overall review for the PR author")
