"""Diff parsing.

GitHub only lets you comment on lines that are part of the diff, so for each
file we track which new-file line numbers exist in it. We also print the patch
with those numbers in a gutter, otherwise the model guesses line numbers badly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# lockfiles, minified/generated stuff, binaries
SKIP_PATTERNS = [
    re.compile(p)
    for p in (
        r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock)$",
        r"\.(min\.js|min\.css|map|svg|png|jpe?g|gif|ico|pdf|zip|lock)$",
        r"(^|/)(dist|build|vendor|node_modules)/",
    )
]


@dataclass
class FileDiff:
    path: str
    status: str = "modified"  # added | modified | removed | renamed
    patch: str = ""
    added_lines: dict[int, str] = field(default_factory=dict)  # new line no -> text
    commentable_lines: set[int] = field(default_factory=set)  # added + context lines

    @property
    def additions(self) -> int:
        return len(self.added_lines)

    def is_reviewable(self) -> bool:
        if self.status == "removed" or not self.patch:
            return False
        return not any(p.search(self.path) for p in SKIP_PATTERNS)

    def annotated_patch(self, max_chars: int = 12_000) -> str:
        """Render the patch with new-file line numbers in a left gutter.

        '+' lines are new, ' ' lines are unchanged context, '-' lines were
        removed (they have no new-file line number).
        """
        out: list[str] = []
        new_no = 0
        for raw in self.patch.splitlines():
            m = HUNK_RE.match(raw)
            if m:
                new_no = int(m.group(3))
                out.append(raw)
                continue
            if raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            tag, text = (raw[:1] or " "), raw[1:]
            if tag == "-":
                out.append(f"{'':>5} - {text}")
            else:
                out.append(f"{new_no:>5} {tag} {text}")
                new_no += 1
        rendered = "\n".join(out)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n... [patch truncated]"
        return rendered

    def nearest_commentable(self, line: int, window: int = 3) -> int | None:
        """Snap a line the LLM cited to the closest line GitHub will accept."""
        if line in self.commentable_lines:
            return line
        for delta in range(1, window + 1):
            for cand in (line - delta, line + delta):
                if cand in self.added_lines:
                    return cand
        return None


def parse_patch(path: str, patch: str, status: str = "modified") -> FileDiff:
    """Parse a single file's hunks (the `patch` field GitHub's API returns)."""
    fd = FileDiff(path=path, status=status, patch=patch)
    new_no = 0
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            new_no = int(m.group(3))
            continue
        if raw.startswith("\\"):
            continue
        tag = raw[:1] or " "
        if tag == "+":
            fd.added_lines[new_no] = raw[1:]
            fd.commentable_lines.add(new_no)
            new_no += 1
        elif tag == " ":
            fd.commentable_lines.add(new_no)
            new_no += 1
        # '-' lines don't advance the new-file counter
    return fd


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Parse full `git diff` output (multiple files with headers)."""
    files: list[FileDiff] = []
    chunks = re.split(r"^diff --git ", diff_text, flags=re.MULTILINE)
    for chunk in chunks:
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        header = lines[0]  # "a/foo.py b/foo.py"
        path = header.split(" b/", 1)[-1].strip()
        status = "modified"
        body_start = None
        for i, line in enumerate(lines[1:], start=1):
            if line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "removed"
            elif line.startswith("rename to "):
                status, path = "renamed", line[len("rename to "):].strip()
            elif line.startswith("+++ "):
                target = line[4:].strip()
                if target != "/dev/null":
                    path = target.removeprefix("b/")
            elif line.startswith("@@"):
                body_start = i
                break
        if body_start is None:  # binary file or pure rename
            continue
        files.append(parse_patch(path, "\n".join(lines[body_start:]), status))
    return files
