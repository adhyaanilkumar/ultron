"""The escalation ticket's title, its short description and its report (spec §12, D27).

People get overwhelmed reading AI content, so the description stays within 280 characters: any note (for example the
issue-type fallback), the reason in one line, and a pointer to `report.md`. Jira Cloud's v3 API takes the description
as Atlassian Document Format (ADF), so each of those lines is a plain paragraph. Everything else (the requirement, the
task, the acceptance criteria, each attempt's rung and result, the last test output) goes in `report.md`, Markdown,
which the client attaches to the ticket.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_DESCRIPTION_CHARS = 280
MAX_COMMENT_CHARS = 280
MAX_NOTE_CHARS = 100

# The report keeps the tail of the last test output; the full logs travel as their own attachments.
MAX_TEST_OUTPUT_CHARS = 20_000

REPORT_NAME = "report.md"
POINTER = (
    f"Criteria, attempts and the last test output: see the attached {REPORT_NAME}."
)

ATTEMPT_COLUMNS = ("Attempt", "Rung", "Outcome", "Summary")


@dataclass(frozen=True)
class AttemptRow:
    """One attempt in the report's attempt table."""

    number: int
    rung: str
    outcome: str
    summary: str = ""


@dataclass(frozen=True)
class TicketBody:
    """Everything spec §12 puts in an escalation ticket, except attachments and labels."""

    requirement: str
    task: str
    title: str
    reason: str
    acceptance_criteria: Sequence[str] = field(default_factory=tuple)
    last_test_output: str = ""
    attempts: Sequence[AttemptRow] = field(default_factory=tuple)
    requirement_title: str = ""

    def __post_init__(self) -> None:
        for name in ("requirement", "task", "title", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"TicketBody.{name} must be a non-empty string")


def ticket_summary(requirement: str, task: str, title: str) -> str:
    """The ticket title: `[ultron] R<n>/T<k> <title>` (spec §12)."""
    summary = f"[ultron] {requirement}/{task} {title}".strip()
    # Jira's summary is one line of at most 255 characters.
    summary = one_line(summary)
    return cut(summary, 255)


def one_line(value: str) -> str:
    """`value` with every run of whitespace, line breaks included, made one space."""
    return " ".join(value.split())


def cut(value: str, limit: int) -> str:
    """`value` if it fits in `limit` characters, else its start and "…", `limit` characters in all."""
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…"


def description_lines(body: TicketBody, notes: Sequence[str] = ()) -> list[str]:
    """The description's lines: any notes (as one line), the reason in one line, and the pointer to report.md.

    Joined with line breaks they come to at most 280 characters. The reason is cut to fit; report.md has it whole.
    """
    lines: list[str] = []
    note = one_line(" ".join(notes))
    if note:
        lines.append(cut(f"Note: {note}", MAX_NOTE_CHARS))
    used = len("\n".join([*lines, POINTER])) + 1  # +1: the reason's own line break
    lines.append(cut(f"Reason: {one_line(body.reason)}", MAX_DESCRIPTION_CHARS - used))
    lines.append(POINTER)
    return lines


def description_text(body: TicketBody, notes: Sequence[str] = ()) -> str:
    """The description as plain text, one line per paragraph: at most 280 characters (D27)."""
    return "\n".join(description_lines(body, notes))


def short_doc(lines: Sequence[str]) -> dict[str, Any]:
    """An ADF document with one plain paragraph per line (an ADF text node must not be empty)."""
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in lines
        if line
    ]
    return {"type": "doc", "version": 1, "content": content}


def to_adf(body: TicketBody, notes: Sequence[str] = ()) -> dict[str, Any]:
    """The ticket description as ADF: `description_lines`, one paragraph each. `notes` come first."""
    return short_doc(description_lines(body, notes))


def tail(value: str, limit: int = MAX_TEST_OUTPUT_CHARS) -> str:
    """The last `limit` characters of `value`, with a marker when something was cut."""
    if len(value) <= limit:
        return value
    return "[… earlier output cut; the full log is attached]\n" + value[-limit:]


def _cell(value: str) -> str:
    """A Markdown table cell: one line, with `|` escaped."""
    return one_line(value).replace("|", "\\|")


def _fenced(value: str) -> list[str]:
    """`value` in a code fence longer than any run of backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", value, fence]


def render_report(body: TicketBody, notes: Sequence[str] = ()) -> str:
    """`report.md`: what the short description leaves out, as Markdown (spec §12, D27)."""
    requirement = body.requirement
    if body.requirement_title:
        requirement = f"{body.requirement}: {body.requirement_title}"
    out = [f"# {ticket_summary(body.requirement, body.task, body.title)}", ""]
    for note in notes:
        out += [f"Note: {one_line(note)}", ""]
    out += ["## Reason", "", body.reason.strip(), ""]
    out += ["## Requirement", "", requirement, ""]
    out += ["## Task", "", f"{body.task}: {body.title}", ""]
    out += ["## Acceptance criteria", ""]
    if body.acceptance_criteria:
        out += [f"- {one_line(criterion)}" for criterion in body.acceptance_criteria]
    else:
        out.append("None recorded.")
    out += ["", "## Attempts", ""]
    if body.attempts:
        out.append("| " + " | ".join(ATTEMPT_COLUMNS) + " |")
        out.append("|" + "---|" * len(ATTEMPT_COLUMNS))
        for row in body.attempts:
            cells = (str(row.number), row.rung, row.outcome, row.summary)
            out.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    else:
        out.append("No attempts ran.")
    out += ["", "## Last test output", ""]
    output = tail(body.last_test_output)
    out += _fenced(output) if output.strip() else ["None recorded."]
    return "\n".join(out) + "\n"
