from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ultron.config import Config, ConfigWarning, build_config, load_config
from ultron.config import defaults as d

MIN: dict[str, Any] = {"commands": {"test": "make test"}}


def build(
    raw: dict[str, Any] | None = None, over: dict[str, Any] | None = None, **kw: Any
) -> Any:
    return build_config(
        raw if raw is not None else MIN, over, windows=kw.pop("windows", True), **kw
    )


def test_minimal_file_loads_with_defaults(tmp_path: Path) -> None:
    f = tmp_path / "ultron.toml"
    f.write_text('[commands]\ntest = "make test"\n')
    loaded = load_config(f, windows=True)
    c = loaded.config
    assert loaded.warnings == []
    assert c.max_attempts == 3 and c.max_parallel == 2
    assert (
        c.deploy.tag_format == "v{version}" and c.deploy.rollback_rehearsal_days == 30
    )
    assert c.maintain.interval_minutes == 30
    assert c.repo_instructions is True and c.intake_research == "local"
    assert c.broker.ask_limit == 3
    assert c.thresholds.denied_actions == 5 and c.thresholds.no_progress_minutes == 10
    assert c.scout.max_tool_calls == 15
    assert (
        c.paths.work_folder == "C:\\git\\.ultron-work"
        and c.paths.ultron_home == "C:\\git\\.ultron"
    )
    assert c.ladder.max_rung == "R5"
    assert (c.ladder.rungs["R3"].model, c.ladder.rungs["R3"].effort) == (
        "opus-5.5",
        "high",
    )
    assert (c.agents.intake_scout.model, c.agents.intake_scout.effort) == (
        "haiku-5.5",
        "medium",
    )
    assert (c.agents.spec.model, c.agents.spec.effort) == ("opus-5.5", "high")
    gates = {g: c.gates.value(g) for g in d.DEFAULT_GATES}
    assert gates == d.DEFAULT_GATES
    assert c.gates.merge == c.gates.deploy == c.gates.rollback == "human"


@pytest.mark.parametrize("gate", ["merge", "deploy", "rollback"])
def test_locked_gates_reject_auto_in_file_and_override(gate: str) -> None:
    with pytest.raises(ValidationError, match=gate):
        build({**MIN, "gates": {gate: "auto"}})
    with pytest.raises(ValidationError, match=gate):
        build(MIN, {"gates": {gate: "auto"}})
    with pytest.raises(ValidationError, match=gate):
        build({**MIN, "gates": {"preset": "unattended", gate: "auto"}})


def test_deploy_requires_rollback() -> None:
    with pytest.raises(ValidationError, match="rollback"):
        build({"commands": {"test": "t", "deploy": "./deploy.sh"}})
    assert build(
        {"commands": {"test": "t", "rollback": "./rb.sh"}}
    ).config.commands.rollback
    assert (
        build(
            {"commands": {"test": "t", "deploy": "d", "rollback": "r"}}
        ).config.commands.deploy
        == "d"
    )


@pytest.mark.parametrize("n", [0, 11, -1])
def test_max_attempts_out_of_range(n: int) -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "max_attempts": n})


@pytest.mark.parametrize("n", [1, 10])
def test_max_attempts_edges(n: int) -> None:
    assert build({**MIN, "max_attempts": n}).config.max_attempts == n


@pytest.mark.parametrize("preset", ["strict", "default", "unattended"])
def test_presets_expand(preset: str) -> None:
    c = build({**MIN, "gates": {"preset": preset}}).config
    for g, v in d.PRESETS[preset].items():
        assert c.gates.value(g) == v
    assert c.gates.merge == "human"


def test_explicit_gate_beats_preset() -> None:
    c = build({**MIN, "gates": {"preset": "unattended", "spec": "human"}}).config
    assert c.gates.spec == "human" and c.gates.plan == "auto"


def test_warnings_for_default_human_gates() -> None:
    w = build({**MIN, "gates": {"intent": "auto", "plan": "auto"}}).warnings
    assert [x.gate for x in w] == ["intent", "plan"]
    assert all(
        isinstance(x, ConfigWarning)
        and x.from_value == "human"
        and x.to_value == "auto"
        for x in w
    )
    assert build(MIN, {"gates": {"result": "auto"}}).warnings[0].gate == "result"
    assert build({**MIN, "gates": {"tasks": "human"}}).warnings == []
    assert len(build({**MIN, "gates": {"preset": "unattended"}}).warnings) == 4


def test_override_merges_over_file() -> None:
    c = build(
        {**MIN, "max_attempts": 5},
        {"max_attempts": 2, "caps": {"per_requirement_usd": 1}},
    ).config
    assert c.max_attempts == 2 and c.caps.per_requirement_usd == 1
    assert c.commands.test == "make test"


def test_space_in_paths_rejected() -> None:
    with pytest.raises(ValidationError, match="without spaces"):
        build({**MIN, "paths": {"work_folder": "C:\\my dir\\work"}})
    with pytest.raises(ValidationError, match="without spaces"):
        build({**MIN, "paths": {"ultron_home": "C:\\my dir"}})


def test_linux_defaults_and_home_with_space() -> None:
    c = build(windows=False, home=Path("/home/u")).config
    assert c.paths.work_folder == str(Path("/home/u") / ".ultron-work")
    assert c.paths.ultron_home == str(Path("/home/u") / ".ultron")
    with pytest.raises(ValidationError, match="without spaces"):
        build(windows=False, home=Path("/home/a b"))


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "jira": {"api_token": "x"}})
    with pytest.raises(ValidationError):
        build({**MIN, "bogus": 1})


def _walk(model: type[BaseModel], seen: set[type[BaseModel]]) -> list[str]:
    names: list[str] = []
    for name, field in model.model_fields.items():
        names.append(name)
        for t in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if isinstance(t, type) and issubclass(t, BaseModel) and t not in seen:
                seen.add(t)
                names += _walk(t, seen)
    return names


def test_no_credential_fields() -> None:
    names = _walk(Config, {Config})
    assert not [
        n
        for n in names
        if any(w in n.lower() for w in ("token", "password", "secret", "credential"))
    ]


def test_ladder_validation() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "ladder": {"max_rung": "R9"}})
    with pytest.raises(ValidationError):
        build({**MIN, "ladder": {"rungs": {"R9": {"model": "m", "effort": "high"}}}})
    assert build({**MIN, "ladder": {"max_rung": "R3"}}).config.ladder.max_rung == "R3"
