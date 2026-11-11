"""The §12 ticket title and body, rendered as ADF."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ultron.jira.body import (
    MAX_TEST_OUTPUT_CHARS,
    AttemptRow,
    TicketBody,
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


def test_headings_criteria_table_and_code() -> None:
    doc = to_adf(_body())
    headings = [_all_text(h) for h in _nodes(doc, "heading")]
    assert headings == [
        "Requirement",
        "Task",
        "Reason",
        "Acceptance criteria",
        "Attempts",
        "Last test output",
    ]
    items = _nodes(doc, "listItem")
    assert [_all_text(i) for i in items] == list(_body().acceptance_criteria)

    (table,) = _nodes(doc, "table")
    rows = table["content"]
    assert [_all_text(c) for c in rows[0]["content"]] == [
        "Attempt",
        "Rung",
        "Outcome",
        "Summary",
    ]
    assert [_all_text(c) for c in rows[3]["content"]] == ["3", "R4", "fail", "timeout"]
    assert rows[0]["content"][0]["type"] == "tableHeader"

    (code,) = _nodes(doc, "codeBlock")
    assert "AssertionError" in _all_text(code)


def test_reason_and_task_present() -> None:
    text = _all_text(to_adf(_body()))
    assert "all 3 attempts failed" in text
    assert "T2: Add the export button" in text
    assert "R-20261111-a3f2" in text


def test_notes_come_first() -> None:
    doc = to_adf(_body(), notes=["filed as a Story"])
    first = doc["content"][0]
    assert first["type"] == "paragraph"
    assert "filed as a Story" in _all_text(first)


def test_no_empty_text_nodes() -> None:
    doc = to_adf(_body(last_test_output="", attempts=[AttemptRow(1, "R1", "fail")]))
    assert all(n["text"] for n in _nodes(doc, "text"))


def test_long_output_keeps_the_tail() -> None:
    out = "a" * MAX_TEST_OUTPUT_CHARS + "THE-END"
    cut = tail(out)
    assert cut.endswith("THE-END")
    assert len(cut) < len(out) + 100
    assert "cut" in cut


@pytest.mark.parametrize("name", ["requirement", "task", "title", "reason"])
def test_required_fields(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        _body(**{name: " "})
