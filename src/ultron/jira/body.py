"""The escalation ticket's title and body (spec §12), rendered as Atlassian Document Format.

Jira Cloud's v3 API takes the description as ADF. Plain text or Markdown would show as raw text, so the body is
built from standard ADF nodes only: headings, paragraphs, a bullet list, a table and a code block.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Jira's description field holds at most 32,767 characters; the last test output is cut to its tail well below that.
# The full logs travel as attachments.
MAX_TEST_OUTPUT_CHARS = 20_000

ATTEMPT_COLUMNS = ("Attempt", "Rung", "Outcome", "Summary")


@dataclass(frozen=True)
class AttemptRow:
    """One attempt in the ticket's attempt table."""

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
    summary = " ".join(summary.split())
    return summary if len(summary) <= 255 else summary[:254] + "…"


def _text(value: str, *, strong: bool = False) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if strong:
        node["marks"] = [{"type": "strong"}]
    return node


def _paragraph(*parts: dict[str, Any]) -> dict[str, Any]:
    # An ADF text node must not be empty, so empty parts are dropped.
    return {"type": "paragraph", "content": [p for p in parts if p.get("text")]}


def _heading(value: str, level: int = 2) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": [_text(value)]}


def _bullets(items: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_paragraph(_text(item))]} for item in items
        ],
    }


def _cell(kind: str, value: str, *, strong: bool = False) -> dict[str, Any]:
    paragraph = (
        _paragraph(_text(value, strong=strong)) if value else {"type": "paragraph"}
    )
    return {"type": kind, "attrs": {}, "content": [paragraph]}


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> dict[str, Any]:
    head = {
        "type": "tableRow",
        "content": [_cell("tableHeader", h, strong=True) for h in header],
    }
    body = [
        {"type": "tableRow", "content": [_cell("tableCell", v) for v in row]}
        for row in rows
    ]
    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": [head, *body],
    }


def _code(value: str) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "codeBlock", "attrs": {}}
    if value:
        node["content"] = [_text(value)]
    return node


def tail(value: str, limit: int = MAX_TEST_OUTPUT_CHARS) -> str:
    """The last `limit` characters of `value`, with a marker when something was cut."""
    if len(value) <= limit:
        return value
    return "[… earlier output cut; the full log is attached]\n" + value[-limit:]


def to_adf(body: TicketBody, notes: Sequence[str] = ()) -> dict[str, Any]:
    """The ticket description as an ADF document. `notes` (for example the issue-type fallback) come first."""
    content: list[dict[str, Any]] = []
    for note in notes:
        content.append(_paragraph(_text("Note: ", strong=True), _text(note)))

    requirement = body.requirement
    if body.requirement_title:
        requirement = f"{body.requirement}: {body.requirement_title}"
    content.append(_heading("Requirement"))
    content.append(_paragraph(_text(requirement)))
    content.append(_heading("Task"))
    content.append(_paragraph(_text(f"{body.task}: {body.title}")))
    content.append(_heading("Reason"))
    content.append(_paragraph(_text(body.reason)))

    content.append(_heading("Acceptance criteria"))
    if body.acceptance_criteria:
        content.append(_bullets(list(body.acceptance_criteria)))
    else:
        content.append(_paragraph(_text("None recorded.")))

    content.append(_heading("Attempts"))
    if body.attempts:
        rows = [(str(a.number), a.rung, a.outcome, a.summary) for a in body.attempts]
        content.append(_table(ATTEMPT_COLUMNS, rows))
    else:
        content.append(_paragraph(_text("No attempts ran.")))

    content.append(_heading("Last test output"))
    content.append(_code(tail(body.last_test_output)))
    content.append(
        _paragraph(
            _text("Filed by ultron's engine. Attempt logs and plan.md are attached.")
        )
    )
    return {"type": "doc", "version": 1, "content": content}
