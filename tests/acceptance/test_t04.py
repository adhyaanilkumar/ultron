"""Acceptance tests for T04 (ultron.toml config), written from the spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ultron.config import Config, build_config, load_config

MIN: dict[str, Any] = {"commands": {"test": "make test"}}
CHANGEABLE = ["intent", "spec", "tasks", "plan", "result", "escalation", "maintain"]
LOCKED = ["merge", "deploy", "rollback"]
DEFAULT_TABLE = {
    "intent": "human",
    "spec": "human",
    "tasks": "auto",
    "plan": "human",
    "result": "human",
    "escalation": "auto",
    "maintain": "auto",
}


def build(
    raw: dict[str, Any] | None = None, over: dict[str, Any] | None = None, **kw: Any
) -> Any:
    return build_config(
        raw if raw is not None else MIN, over, windows=kw.pop("windows", True), **kw
    )


def gate_table(cfg: Config) -> dict[str, str]:
    return {g: str(getattr(cfg.gates, g)) for g in CHANGEABLE}


# ---- AC1 ----
def test_ac1_minimal_file_defaults(tmp_path: Path) -> None:
    f = tmp_path / "ultron.toml"
    f.write_text('[commands]\ntest = "pytest"\n')
    loaded = load_config(f, windows=True)
    c = loaded.config
    assert loaded.warnings == []
    assert c.commands.test == "pytest"
    assert c.commands.deploy is None and c.commands.rollback is None
    assert c.max_attempts == 3
    assert c.max_parallel == 2
    assert c.broker.ask_limit == 3
    assert c.deploy.tag_format == "v{version}"
    assert c.deploy.rollback_rehearsal_days == 30
    assert c.maintain.interval_minutes == 30
    assert gate_table(c) == DEFAULT_TABLE
    for g in LOCKED:
        assert getattr(c.gates, g) == "human"


def test_ac1_d16_agent_defaults() -> None:
    a = build().config.agents
    assert (a.intake_scout.model, a.intake_scout.effort) == ("haiku-5.5", "medium")
    assert (a.intake.model, a.intake.effort) == ("sonnet-5.5", "medium")
    assert (a.spec.model, a.spec.effort) == ("opus-5.5", "high")
    assert (a.decomposition.model, a.decomposition.effort) == ("opus-5.5", "high")
    assert (a.maintenance.model, a.maintenance.effort) == ("sonnet-5.5", "medium")
    assert a.spec.adapter == "claude-agent-sdk"


def test_ac1_ladder_default() -> None:
    lad = build().config.ladder
    assert list(lad.rungs) == ["R0", "R1", "R2", "R3", "R4", "R5"]
    got = {k: (v.model, v.effort) for k, v in lad.rungs.items()}
    assert got["R0"] == ("haiku-5.5", "medium")
    assert got["R1"] == ("sonnet-5.5", "medium")
    assert got["R5"] == ("opus-5.5", "max")


def test_ac1_default_paths_windows_and_linux() -> None:
    home = Path("/home/dev")
    w = build(windows=True).config.paths
    assert w.work_folder == r"C:\git\.ultron-work"
    assert w.ultron_home == r"C:\git\.ultron"
    li = build(windows=False, home=home).config.paths
    assert li.work_folder == str(home / ".ultron-work")
    assert li.ultron_home == str(home / ".ultron")


def test_ac1_commands_test_required() -> None:
    with pytest.raises(ValidationError):
        build({})
    with pytest.raises(ValidationError):
        build({"commands": {"smoke": "x"}})


def test_override_merges_on_top_of_file() -> None:
    raw = {
        "commands": {"test": "a"},
        "max_attempts": 5,
        "caps": {"per_requirement_usd": 7.0},
    }
    c = build(raw, {"max_attempts": 2, "gates": {"plan": "auto"}}).config
    assert c.max_attempts == 2
    assert c.caps.per_requirement_usd == 7.0
    assert c.commands.test == "a"
    assert c.gates.plan == "auto"
    assert c.gates.spec == "human"


# ---- AC2 ----
@pytest.mark.parametrize("gate", LOCKED)
def test_ac2_locked_gate_in_file(gate: str) -> None:
    with pytest.raises(ValidationError) as ei:
        build({**MIN, "gates": {gate: "auto"}})
    assert gate in str(ei.value)


@pytest.mark.parametrize("gate", LOCKED)
def test_ac2_locked_gate_in_override(gate: str) -> None:
    with pytest.raises(ValidationError) as ei:
        build(MIN, {"gates": {gate: "auto"}})
    assert gate in str(ei.value)


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("preset", ["strict", "default", "unattended"])
def test_ac2_locked_with_any_preset_and_toml_file(
    gate: str, preset: str, tmp_path: Path
) -> None:
    f = tmp_path / "ultron.toml"
    f.write_text(
        f'[commands]\ntest = "t"\n[gates]\npreset = "{preset}"\n{gate} = "auto"\n'
    )
    with pytest.raises(ValidationError) as ei:
        load_config(f, windows=True)
    assert gate in str(ei.value)


@pytest.mark.parametrize("preset", ["strict", "default", "unattended"])
def test_ac2_presets_never_unlock(preset: str) -> None:
    c = build({**MIN, "gates": {"preset": preset}}).config
    for g in LOCKED:
        assert getattr(c.gates, g) == "human"


@pytest.mark.parametrize("gate", LOCKED)
@pytest.mark.parametrize("bad", ["AUTO", "automatic", True, 1, ""])
def test_ac2_locked_rejects_other_values(gate: str, bad: Any) -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "gates": {gate: bad}})


def test_ac2_override_unlock_with_unattended_preset() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "gates": {"preset": "unattended"}}, {"gates": {"merge": "auto"}})


def test_ac2_override_changing_preset_cannot_unlock() -> None:
    c = build(MIN, {"gates": {"preset": "unattended"}}).config
    assert [getattr(c.gates, g) for g in LOCKED] == ["human"] * 3


# ---- AC3 ----
def test_ac3_deploy_without_rollback() -> None:
    with pytest.raises(ValidationError) as ei:
        build({"commands": {"test": "t", "deploy": "./d"}})
    assert "rollback" in str(ei.value)


def test_ac3_rollback_without_deploy_ok() -> None:
    c = build({"commands": {"test": "t", "rollback": "./r"}}).config
    assert c.commands.rollback == "./r" and c.commands.deploy is None


def test_ac3_both_ok() -> None:
    c = build({"commands": {"test": "t", "deploy": "./d", "rollback": "./r"}}).config
    assert c.commands.deploy == "./d"


def test_ac3_override_adds_deploy_without_rollback() -> None:
    with pytest.raises(ValidationError):
        build(MIN, {"commands": {"deploy": "./d"}})


# ---- AC4 ----
@pytest.mark.parametrize("n", [0, -1, 11, 100])
def test_ac4_max_attempts_out_of_range(n: int) -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "max_attempts": n})
    with pytest.raises(ValidationError):
        build(MIN, {"max_attempts": n})


@pytest.mark.parametrize("n", [1, 3, 10])
def test_ac4_max_attempts_in_range(n: int) -> None:
    assert build({**MIN, "max_attempts": n}).config.max_attempts == n


def test_ac4_preset_strict() -> None:
    c = build({**MIN, "gates": {"preset": "strict"}}).config
    assert set(gate_table(c).values()) == {"human"}


def test_ac4_preset_unattended() -> None:
    c = build({**MIN, "gates": {"preset": "unattended"}}).config
    assert set(gate_table(c).values()) == {"auto"}


def test_ac4_preset_default_matches_table() -> None:
    c = build({**MIN, "gates": {"preset": "default"}}).config
    assert gate_table(c) == DEFAULT_TABLE


def test_ac4_explicit_gate_overrides_preset() -> None:
    c = build({**MIN, "gates": {"preset": "strict", "tasks": "auto"}}).config
    assert c.gates.tasks == "auto" and c.gates.plan == "human"


def test_ac4_unknown_preset_rejected() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "gates": {"preset": "yolo"}})


def test_ac4_bad_gate_value_rejected() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "gates": {"plan": "maybe"}})


# ---- AC5 ----
@pytest.mark.parametrize("key", ["work_folder", "ultron_home"])
@pytest.mark.parametrize(
    "val", [r"C:\Users\Anil Kumar ML\.ultron", "/home/a b/.x", " lead", "trail "]
)
def test_ac5_spaces_rejected(key: str, val: str) -> None:
    with pytest.raises(ValidationError) as ei:
        build({**MIN, "paths": {key: val}})
    msg = str(ei.value).lower()
    assert "space" in msg and key in msg


def test_ac5_override_with_space_rejected() -> None:
    with pytest.raises(ValidationError):
        build(MIN, {"paths": {"work_folder": r"C:\my work"}})


def test_ac5_no_space_accepted() -> None:
    c = build({**MIN, "paths": {"work_folder": r"D:\w", "ultron_home": r"D:\u"}}).config
    assert c.paths.work_folder == r"D:\w"


def test_ac5_default_home_with_space_rejected() -> None:
    # The Linux default derives from the home dir; a home with a space must not slip through.
    home = Path("/home/Anil Kumar")
    with pytest.raises(ValidationError):
        build(windows=False, home=home)


# ---- AC6 ----
@pytest.mark.parametrize("gate", ["intent", "spec", "plan", "result"])
def test_ac6_warning_for_default_human_gate(gate: str) -> None:
    loaded = build({**MIN, "gates": {gate: "auto"}})
    assert len(loaded.warnings) == 1
    w = loaded.warnings[0]
    assert w.gate == gate and w.to_value == "auto" and w.from_value == "human"
    assert w.message


def test_ac6_warning_via_override_and_preset() -> None:
    assert [w.gate for w in build(MIN, {"gates": {"plan": "auto"}}).warnings] == [
        "plan"
    ]
    gates = {w.gate for w in build({**MIN, "gates": {"preset": "unattended"}}).warnings}
    assert gates == {"intent", "spec", "plan", "result"}


@pytest.mark.parametrize("gate", ["tasks", "escalation", "maintain"])
def test_ac6_no_warning_for_default_auto_gates(gate: str) -> None:
    assert build({**MIN, "gates": {gate: "auto"}}).warnings == []
    assert build().warnings == []


def test_ac6_no_warning_strict() -> None:
    assert build({**MIN, "gates": {"preset": "strict"}}).warnings == []


# ---- AC7 ----
def _all_field_names(model: type[BaseModel], seen: set[Any] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    names: set[str] = set()
    for name, f in model.model_fields.items():
        names.add(name)
        stack: list[Any] = [f.annotation]
        while stack:
            a = stack.pop()
            if isinstance(a, type) and issubclass(a, BaseModel) and a not in seen:
                seen.add(a)
                names |= _all_field_names(a, seen)
            stack.extend(getattr(a, "__args__", ()) or ())
    return names


def test_ac7_no_credential_fields() -> None:
    bad = ("token", "password", "secret", "api_key", "apikey", "credential", "auth")
    for n in _all_field_names(Config):
        assert not any(b in n.lower() for b in bad), n


@pytest.mark.parametrize("key", ["api_token", "password", "email"])
def test_ac7_unknown_jira_key_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "jira": {key: "x"}})


def test_ac7_unknown_top_level_and_override_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "api_key": "x"})
    with pytest.raises(ValidationError):
        build(MIN, {"jira": {"api_token": "x"}})


def test_ac7_jira_project_board_epic() -> None:
    c = build({**MIN, "jira": {"project": "AD", "board": "b", "epic": "AD-378"}}).config
    assert (c.jira.project, c.jira.board, c.jira.epic) == ("AD", "b", "AD-378")


# ---- misc spec fields ----
def test_misc_other_validation() -> None:
    with pytest.raises(ValidationError):
        build({**MIN, "max_parallel": 0})
    with pytest.raises(ValidationError):
        build({**MIN, "caps": {"per_requirement_usd": -1}})
    with pytest.raises(ValidationError):
        build({**MIN, "ladder": {"max_rung": "R9"}})
    c = build({**MIN, "ladder": {"max_rung": "R3"}, "intake_research": "off"}).config
    assert c.ladder.max_rung == "R3" and c.intake_research == "off"
