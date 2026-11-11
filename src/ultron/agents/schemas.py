"""The JSON output each ultron agent role returns (spec 11.1, 11.3), as pydantic models.

`output_schema(role)` is the JSON schema handed to `AgentRunner.run`; `validate_output(role, data)` is what the
engine runs on `AgentResult.data` before it uses anything. Agents return the text of their artifact and the engine
writes the file, so read-only and tool-less roles work the same way as the others.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ultron.engine.task_graph import Task

__all__ = [
    "OUTPUT_MODELS",
    "ROLES",
    "SchemaError",
    "output_schema",
    "validate_output",
]


class SchemaError(ValueError):
    """An agent's output does not match its role's schema. The message lists every problem, one per line."""


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(_Out):
    category: Literal["touches", "exists", "related", "duplicate"]
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    """A path, a path:line or a requirement id. A fact without one is not a fact."""


class ScoutOutput(_Out):
    facts: list[Fact]
    unanswered_questions: list[str]


class IntakeOutput(_Out):
    questions: list[str] = Field(max_length=5)
    intent_md: str | None
    assumptions: list[str]

    @model_validator(mode="after")
    def _something_to_return(self) -> IntakeOutput:
        if not self.questions and not (self.intent_md and self.intent_md.strip()):
            raise ValueError("return either questions or intent_md")
        return self


class VersionBump(_Out):
    kind: Literal["major", "minor", "patch"]
    reason: str = Field(min_length=1)


class CostRow(_Out):
    item: str = Field(min_length=1)
    inr_per_month: float = Field(ge=0)


class SpecOutput(_Out):
    spec_md: str = Field(min_length=1)
    version_bump: VersionBump
    questions: list[str]
    concerns: list[str]
    infrastructure: bool
    human_steps: list[str] = Field(default_factory=list)
    cost_table: list[CostRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infrastructure_sections(self) -> SpecOutput:
        if self.infrastructure and not (self.human_steps and self.cost_table):
            raise ValueError(
                "an infrastructure requirement needs human_steps and cost_table (spec 7.3)"
            )
        return self


class DecompositionOutput(_Out):
    tasks_md: str = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)


class DeveloperOutput(_Out):
    phase: Literal["plan", "code"]
    plan_md: str | None = None
    summary: str = Field(min_length=1)
    files_changed: list[str]
    blocked: str | None = None

    @model_validator(mode="after")
    def _plan_phase_returns_a_plan(self) -> DeveloperOutput:
        if self.phase == "plan" and not (self.plan_md and self.plan_md.strip()):
            raise ValueError("phase 'plan' needs plan_md")
        return self


class CriterionResult(_Out):
    id: str = Field(min_length=1)
    status: Literal["pass", "fail", "unverifiable"]
    evidence: str


class TestingOutput(_Out):
    __test__ = False  # not a pytest class

    tests_written: list[str]
    criteria: list[CriterionResult] = Field(min_length=1)
    summary: str = Field(min_length=1)


class MaintenanceOutput(_Out):
    intent_md: str = Field(min_length=1)
    diagnosis: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    related_tasks: list[str]
    version_bump: Literal["major", "minor", "patch"] = "patch"
    proposes_rollback: bool = False


OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "intake_scout": ScoutOutput,
    "intake": IntakeOutput,
    "spec": SpecOutput,
    "decomposition": DecompositionOutput,
    "developer": DeveloperOutput,
    "testing": TestingOutput,
    "maintenance": MaintenanceOutput,
}
ROLES: tuple[str, ...] = tuple(OUTPUT_MODELS)


def output_schema(role: str) -> dict[str, Any]:
    """The JSON schema for `role`'s output. Raises `KeyError` for an unknown role."""
    return OUTPUT_MODELS[role].model_json_schema()


def validate_output(role: str, data: Mapping[str, Any] | None) -> BaseModel:
    """Parse `data` with the role's model. Raises `SchemaError` naming each problem."""
    model = OUTPUT_MODELS[role]
    if data is None:
        raise SchemaError(f"{role}: the agent returned no structured output")
    try:
        return model.model_validate(dict(data))
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "output"
            lines.append(f"{role}: {where}: {err['msg']}")
        raise SchemaError("\n".join(lines)) from e
