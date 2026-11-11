"""T18 AC5: each role's output schema accepts a good sample and rejects one missing required fields."""

import copy
import json
from typing import Any

import pytest

from ultron.agents.schemas import ROLES, SchemaError, output_schema, validate_output

TASK = {
    "id": "T1",
    "title": "Add endpoint",
    "type": "story",
    "description": "Do it.",
    "acceptance_criteria": [
        {
            "id": "AC1",
            "text": "It works.",
            "verify_with": [{"method": "pytest", "detail": "tests/test_x.py"}],
        }
    ],
    "depends_on": [],
    "ships_independently": True,
    "rubric": {
        f: {"score": 0, "reason": "r"}
        for f in (
            "files",
            "change_type",
            "risk",
            "ambiguity",
            "algorithmic",
            "test_surface",
        )
    },
    "total": 0,
    "rung": "R0",
    "mcp": {"servers": [], "reason": "none"},
}

GOOD: dict[str, dict[str, Any]] = {
    "intake_scout": {
        "facts": [
            {
                "category": "touches",
                "text": "Login lives in auth.py",
                "source": "src/auth.py:10",
            }
        ],
        "unanswered_questions": ["Who owns sessions?"],
    },
    "intake": {
        "questions": ["Which users?"],
        "intent_md": None,
        "assumptions": [],
    },
    "spec": {
        "spec_md": "# Spec",
        "version_bump": {"kind": "minor", "reason": "New feature"},
        "questions": [],
        "concerns": [],
        "infrastructure": False,
    },
    "decomposition": {"tasks_md": "### T1", "tasks": [TASK]},
    "developer": {
        "phase": "plan",
        "plan_md": "# plan",
        "summary": "planned",
        "files_changed": [],
    },
    "testing": {
        "tests_written": ["tests/acceptance/test_t1.py"],
        "criteria": [{"id": "AC1", "status": "pass", "evidence": "pytest ok"}],
        "summary": "all pass",
    },
    "maintenance": {
        "intent_md": "# intent",
        "diagnosis": "null pointer",
        "fingerprint": "abc123",
        "related_tasks": ["T3"],
    },
}

# role -> a required key to drop for the bad sample
MISSING = {
    "intake_scout": "facts",
    "intake": "questions",
    "spec": "version_bump",
    "decomposition": "tasks",
    "developer": "summary",
    "testing": "criteria",
    "maintenance": "intent_md",
}


def test_every_role_has_samples() -> None:
    assert set(ROLES) == set(GOOD) == set(MISSING)
    assert set(ROLES) == {
        "intake_scout",
        "intake",
        "spec",
        "decomposition",
        "developer",
        "testing",
        "maintenance",
    }


@pytest.mark.parametrize("role", ROLES)
def test_good_sample_validates(role: str) -> None:
    validate_output(role, GOOD[role])


@pytest.mark.parametrize("role", ROLES)
def test_sample_missing_a_required_field_is_rejected(role: str) -> None:
    bad = copy.deepcopy(GOOD[role])
    del bad[MISSING[role]]
    with pytest.raises(SchemaError) as err:
        validate_output(role, bad)
    assert MISSING[role] in str(err.value)


@pytest.mark.parametrize("role", ROLES)
def test_json_schema_is_serialisable_and_names_required_fields(role: str) -> None:
    schema = output_schema(role)
    json.dumps(schema)
    assert schema["type"] == "object"
    assert MISSING[role] in schema["properties"]
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("role", ROLES)
def test_extra_fields_are_rejected(role: str) -> None:
    bad = {**GOOD[role], "sneaky": 1}
    with pytest.raises(SchemaError):
        validate_output(role, bad)


def test_unknown_role() -> None:
    with pytest.raises(KeyError):
        output_schema("nobody")


def test_infrastructure_spec_needs_checklist_and_inr_cost_table() -> None:
    base = {**GOOD["spec"], "infrastructure": True}
    with pytest.raises(SchemaError, match="human_steps"):
        validate_output("spec", base)
    ok = {
        **base,
        "human_steps": ["Create the hosting account"],
        "cost_table": [{"item": "VPS", "inr_per_month": 500}],
    }
    validate_output("spec", ok)
    with pytest.raises(SchemaError):
        validate_output("spec", {**ok, "cost_table": []})


def test_intake_asks_at_most_five_questions_and_needs_something() -> None:
    with pytest.raises(SchemaError):
        validate_output(
            "intake",
            {"questions": list("abcdef"), "intent_md": None, "assumptions": []},
        )
    with pytest.raises(SchemaError):
        validate_output(
            "intake", {"questions": [], "intent_md": None, "assumptions": []}
        )


def test_developer_plan_phase_needs_plan_md() -> None:
    with pytest.raises(SchemaError, match="plan_md"):
        validate_output(
            "developer",
            {"phase": "plan", "summary": "s", "files_changed": []},
        )
    validate_output(
        "developer", {"phase": "code", "summary": "s", "files_changed": ["a.py"]}
    )


def test_scout_facts_must_cite_a_source() -> None:
    bad = {
        "facts": [{"category": "exists", "text": "x", "source": ""}],
        "unanswered_questions": [],
    }
    with pytest.raises(SchemaError):
        validate_output("intake_scout", bad)


def test_decomposition_tasks_use_the_t11_task_schema() -> None:
    bad_task = {**TASK}
    del bad_task["rubric"]
    with pytest.raises(SchemaError):
        validate_output("decomposition", {"tasks_md": "x", "tasks": [bad_task]})
