"""The §12 ticket: its title, its short description (ADF) and report.md (D27)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ultron.jira.body import (
    MAX_DESCRIPTION_CHARS,
    MAX_TEST_OUTPUT_CHARS,
    REPORT_NAME,
    AttemptRow,
    TicketBody,
    cut,
    description_text,
    render_report,
    tail,
    ticket_summary,
    to_adf,
)


def _body(**over: Any) -> TicketBody:
    fields: dict[str, Any] = {
        "requirement": "R-20261111-a3f2",
        "task": "T2",
        "title": "Add the export button",
        "reason": "all 3 attempts failed",
        "acceptance_criteria": [
            "AC1: the button exports CSV",
            "AC2: an empty table exports headers",
        ],
        "last_test_output": "FAILED tests/test_export.py::test_empty - AssertionError",
        "attempts": [
            AttemptRow(1, "R2", "fail", "tests failed"),
            AttemptRow(2, "R3", "fail", "tests failed"),
            AttemptRow(3, "R4", "fail", "timeout"),
        ],
    }
    fields.update(over)
    return TicketBody(**fields)


def _nodes(doc: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == kind:
                found.append(node)
            for child in node.get("content", []):
                walk(child)

    walk(doc)
    return found


def _all_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node["text"])
        return " ".join(_all_text(c) for c in node.get("content", []))
    return ""


def test_summary_format() -> None:
    assert ticket_summary("R3", "T2", "Add export") == "[ultron] R3/T2 Add export"


def test_summary_is_one_line_and_capped() -> None:
    summary = ticket_summary("R3", "T2", "line one\nline two " + "x" * 400)
    assert "\n" not in summary
    assert len(summary) <= 255


def test_doc_is_adf() -> None:
    doc = to_adf(_body())
    assert doc["type"] == "doc" and doc["version"] == 1
    json.dumps(doc)  # serialisable


# AC9: the description is at most 280 characters: the reason in one line and a pointer to report.md ------------


def test_description_is_the_reason_and_a_pointer() -> None:
    lines = description_text(_body()).split("\n")
    assert lines == [
        "Reason: all 3 attempts failed",
        f"Criteria, attempts and the last test output: see the attached {REPORT_NAME}.",
    ]
    doc = to_adf(_body())
    assert [p["type"] for p in doc["content"]] == ["paragraph", "paragraph"]
    assert [_all_text(p) for p in doc["content"]] == lines


@pytest.mark.parametrize(
    "notes", [[], ["filed as a Story because project TEST has no Feature type."]]
)
def test_description_stays_within_280_characters(notes: list[str]) -> None:
    long = _body(
        reason="rejected by the tester:\n" + "the export is wrong. " * 80,
        acceptance_criteria=[f"AC{i}: " + "x" * 200 for i in range(30)],
        last_test_output="E" * 100_000,
        attempts=[AttemptRow(i, "R5", "fail", "y" * 300) for i in range(1, 10)],
    )
    text = description_text(long, notes)
    assert len(text) <= MAX_DESCRIPTION_CHARS
    doc = to_adf(long, notes)
    texts = [_all_text(p) for p in doc["content"]]
    assert len("\n".join(texts)) <= MAX_DESCRIPTION_CHARS
    assert sum(len(t) for t in texts) <= MAX_DESCRIPTION_CHARS
    reason = next(t for t in texts if t.startswith("Reason: "))
    assert "\n" not in reason and reason.startswith(
        "Reason: rejected by the tester: the export is wrong."
    )
    assert reason.endswith("…")
    assert REPORT_NAME in texts[-1]


def test_description_leaves_the_details_to_the_report() -> None:
    text = description_text(_body())
    for detail in ("AC1", "the button exports CSV", "AssertionError", "R4", "timeout"):
        assert detail not in text


def test_note_comes_first() -> None:
    note = "filed as a Story because project TEST has no Feature type."
    doc = to_adf(_body(), notes=[note])
    first = doc["content"][0]
    assert first["type"] == "paragraph"
    assert _all_text(first) == f"Note: {note}"
    assert len(description_text(_body(), [note])) <= MAX_DESCRIPTION_CHARS


def test_no_empty_text_nodes() -> None:
    doc = to_adf(_body(last_test_output="", attempts=[AttemptRow(1, "R1", "fail")]))
    assert all(n["text"] for n in _nodes(doc, "text"))


def test_cut() -> None:
    assert cut("short", 10) == "short"
    assert cut("x" * 20, 10) == "x" * 9 + "…"
    assert len(cut("x" * 20, 10)) == 10


# AC9: report.md holds the requirement, task, criteria, each attempt's rung and result, and the last test output --


def test_report_has_everything() -> None:
    report = render_report(
        _body(requirement_title="Export"),
        notes=["filed as a Story because project TEST has no Feature type."],
    )
    assert report.startswith("# [ultron] R-20261111-a3f2/T2 Add the export button\n")
    for needed in [
        "Note: filed as a Story because project TEST has no Feature type.",
        "## Reason",
        "all 3 attempts failed",
        "## Requirement",
        "R-20261111-a3f2: Export",
        "## Task",
        "T2: Add the export button",
        "## Acceptance criteria",
        "- AC1: the button exports CSV",
        "- AC2: an empty table exports headers",
        "## Attempts",
        "| Attempt | Rung | Outcome | Summary |",
        "| 1 | R2 | fail | tests failed |",
        "| 3 | R4 | fail | timeout |",
        "## Last test output",
        "FAILED tests/test_export.py::test_empty - AssertionError",
    ]:
        assert needed in report, needed


def test_report_keeps_the_whole_reason() -> None:
    reason = "rejected by the tester:\n" + "the export is wrong. " * 80
    assert reason.strip() in render_report(_body(reason=reason))


def test_report_cells_and_fences_are_safe() -> None:
    report = render_report(
        _body(
            attempts=[AttemptRow(1, "R2", "fail", "a | b\nc")],
            last_test_output="before\n```\ninside\n```\nafter",
        )
    )
    assert "| 1 | R2 | fail | a \\| b c |" in report
    assert "````text\nbefore\n```\ninside\n```\nafter\n````" in report


def test_report_without_attempts_or_output() -> None:
    report = render_report(
        _body(attempts=[], last_test_output="", acceptance_criteria=[])
    )
    assert "No attempts ran." in report
    assert report.count("None recorded.") == 2  # criteria and test output


def test_report_keeps_the_tail_of_a_long_output() -> None:
    report = render_report(
        _body(last_test_output="a" * MAX_TEST_OUTPUT_CHARS + "THE-END")
    )
    assert "THE-END" in report
    assert len(report) < MAX_TEST_OUTPUT_CHARS + 2_000


def test_long_output_keeps_the_tail() -> None:
    out = "a" * MAX_TEST_OUTPUT_CHARS + "THE-END"
    cut_output = tail(out)
    assert cut_output.endswith("THE-END")
    assert len(cut_output) < len(out) + 100
    assert "cut" in cut_output


@pytest.mark.parametrize("name", ["requirement", "task", "title", "reason"])
def test_required_fields(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        _body(**{name: " "})
