"""Typed ultron.toml model. Unknown keys are rejected; there are no credential fields."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ultron.config import defaults as d

GateValue = Literal["human", "auto"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Commands(_Strict):
    test: str = Field(min_length=1)
    smoke: str | None = None
    deploy: str | None = None
    rollback: str | None = None

    @model_validator(mode="after")
    def _deploy_needs_rollback(self) -> Commands:
        if self.deploy and not self.rollback:
            raise ValueError(
                "commands.deploy is set but commands.rollback is not; both are required together"
            )
        return self


class Deploy(_Strict):
    tag_format: str = d.TAG_FORMAT
    rollback_rehearsal_days: int = Field(default=d.ROLLBACK_REHEARSAL_DAYS, ge=0)


class Maintain(_Strict):
    interval_minutes: int = Field(default=d.MAINTAIN_INTERVAL_MINUTES, ge=1)
    log_path: str | None = None
    log_command: str | None = None


class Gates(_Strict):
    preset: Literal["strict", "default", "unattended"] = "default"
    intent: GateValue = "human"
    spec: GateValue = "human"
    tasks: GateValue = "auto"
    plan: GateValue = "human"
    result: GateValue = "human"
    escalation: GateValue = "auto"
    maintain: GateValue = "auto"
    merge: str = "human"
    deploy: str = "human"
    rollback: str = "human"

    @model_validator(mode="before")
    @classmethod
    def _expand_preset(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        preset = data.get("preset", "default")
        table = d.PRESETS.get(preset) if isinstance(preset, str) else None
        if table is None:
            return data  # field validation reports the bad preset
        return {**table, **data}

    @model_validator(mode="after")
    def _locks(self) -> Gates:
        for gate in d.LOCKED_GATES:
            if getattr(self, gate) != "human":
                raise ValueError(
                    f"gate '{gate}' is locked to human and cannot be set to '{getattr(self, gate)}'"
                )
        return self

    def value(self, gate: str) -> str:
        return str(getattr(self, gate))


class AgentSetting(_Strict):
    adapter: str = d.ADAPTER
    model: str
    effort: Effort


def _agent(role: str) -> AgentSetting:
    model, effort = d.DEFAULT_AGENTS[role]
    return AgentSetting(model=model, effort=effort)  # type: ignore[arg-type]


class Agents(_Strict):
    intake_scout: AgentSetting = Field(default_factory=lambda: _agent("intake_scout"))
    intake: AgentSetting = Field(default_factory=lambda: _agent("intake"))
    spec: AgentSetting = Field(default_factory=lambda: _agent("spec"))
    decomposition: AgentSetting = Field(default_factory=lambda: _agent("decomposition"))
    maintenance: AgentSetting = Field(default_factory=lambda: _agent("maintenance"))
    developer_adapter: str = d.ADAPTER
    testing_adapter: str = d.ADAPTER


class Rung(_Strict):
    model: str
    effort: Effort


def _default_ladder() -> dict[str, Rung]:
    return {k: Rung(model=m, effort=e) for k, (m, e) in d.DEFAULT_LADDER.items()}  # type: ignore[arg-type]


class Ladder(_Strict):
    max_rung: str = "R5"
    rungs: dict[str, Rung] = Field(default_factory=_default_ladder)

    @model_validator(mode="after")
    def _check(self) -> Ladder:
        bad = [k for k in self.rungs if k not in d.RUNGS]
        if bad:
            raise ValueError(f"unknown rung names {bad}; allowed {list(d.RUNGS)}")
        if self.max_rung not in self.rungs:
            raise ValueError(f"max_rung '{self.max_rung}' is not in the ladder")
        return self


class Caps(_Strict):
    per_requirement_usd: float = Field(default=d.CAP_PER_REQUIREMENT_USD, gt=0)
    per_evaluation_run_usd: float = Field(default=d.CAP_PER_EVALUATION_RUN_USD, gt=0)


class Thresholds(_Strict):
    denied_actions: int = Field(default=d.DENIED_ACTIONS_LIMIT, ge=1)
    no_progress_minutes: int = Field(default=d.NO_PROGRESS_MINUTES, ge=1)


class Scout(_Strict):
    max_tool_calls: int = Field(default=d.SCOUT_TOOL_CALLS, ge=1)
    budget_cap: int = Field(default=d.SCOUT_TOKEN_CAP, ge=1)


class Broker(_Strict):
    ask_limit: int = Field(default=d.BROKER_ASK_LIMIT, ge=0)


class Jira(_Strict):
    project: str | None = None
    board: str | None = None
    epic: str | None = None


class Paths(_Strict):
    work_folder: str | None = None
    ultron_home: str | None = None

    @model_validator(mode="after")
    def _no_spaces(self) -> Paths:
        for name in ("work_folder", "ultron_home"):
            value = getattr(self, name)
            if value is not None and " " in value:
                raise ValueError(
                    f"paths.{name} '{value}' contains a space; use a path without spaces, "
                    r"for example C:\git\.ultron-work"
                )
        return self


class Config(_Strict):
    commands: Commands
    deploy: Deploy = Field(default_factory=lambda: Deploy())
    maintain: Maintain = Field(default_factory=lambda: Maintain())
    gates: Gates = Field(default_factory=lambda: Gates())
    max_attempts: int = Field(default=d.MAX_ATTEMPTS_DEFAULT, ge=1, le=10)
    max_parallel: int = Field(default=d.MAX_PARALLEL_DEFAULT, ge=1)
    caps: Caps = Field(default_factory=lambda: Caps())
    agents: Agents = Field(default_factory=lambda: Agents())
    ladder: Ladder = Field(default_factory=lambda: Ladder())
    thresholds: Thresholds = Field(default_factory=lambda: Thresholds())
    scout: Scout = Field(default_factory=lambda: Scout())
    broker: Broker = Field(default_factory=lambda: Broker())
    jira: Jira = Field(default_factory=lambda: Jira())
    paths: Paths = Field(default_factory=lambda: Paths())
    repo_instructions: bool = True
    intake_research: Literal["off", "local"] = "local"
