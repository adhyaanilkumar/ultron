"""AC1: a file with only [commands] test takes every spec default (spec 8, 9.1, 10.1, 11.4)."""

from pathlib import Path

import pytest

from ultron.config import Config, load_config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    f = tmp_path / "ultron.toml"
    f.write_text('[commands]\ntest = "pytest"\n')
    loaded = load_config(f, windows=True)
    assert loaded.warnings == []
    return loaded.config


def test_commands_only_test_is_set(cfg: Config) -> None:
    assert cfg.commands.test == "pytest"
    assert cfg.commands.smoke is None
    assert cfg.commands.deploy is None
    assert cfg.commands.rollback is None


@pytest.mark.parametrize(
    ("gate", "value"),
    [
        ("intent", "human"),
        ("spec", "human"),
        ("tasks", "auto"),
        ("plan", "human"),
        ("result", "human"),
        ("escalation", "auto"),
        ("maintain", "auto"),
        ("merge", "human"),
        ("deploy", "human"),
        ("rollback", "human"),
    ],
)
def test_gate_default(cfg: Config, gate: str, value: str) -> None:
    assert getattr(cfg.gates, gate) == value


def test_scalar_defaults(cfg: Config) -> None:
    assert cfg.max_attempts == 3
    assert cfg.max_parallel == 2
    assert cfg.broker.ask_limit == 3
    assert cfg.repo_instructions is True
    assert cfg.intake_research == "local"
    assert cfg.deploy.tag_format == "v{version}"
    assert cfg.deploy.rollback_rehearsal_days == 30
    assert cfg.maintain.interval_minutes == 30
    assert cfg.scout.max_tool_calls == 15
    assert cfg.thresholds.denied_actions == 5
    assert cfg.thresholds.no_progress_minutes == 10


@pytest.mark.parametrize(
    ("role", "model", "effort"),
    [
        ("intake_scout", "haiku-5.5", "medium"),
        ("intake", "sonnet-5.5", "medium"),
        ("spec", "opus-5.5", "high"),
        ("decomposition", "opus-5.5", "high"),
        ("maintenance", "sonnet-5.5", "medium"),
    ],
)
def test_agent_default(cfg: Config, role: str, model: str, effort: str) -> None:
    a = getattr(cfg.agents, role)
    assert (a.adapter, a.model, a.effort) == ("claude-agent-sdk", model, effort)


@pytest.mark.parametrize(
    ("rung", "model", "effort"),
    [
        ("R0", "haiku-5.5", "medium"),
        ("R1", "sonnet-5.5", "medium"),
        ("R2", "sonnet-5.5", "high"),
        ("R3", "opus-5.5", "high"),
        ("R4", "opus-5.5", "xhigh"),
        ("R5", "opus-5.5", "max"),
    ],
)
def test_ladder_default(cfg: Config, rung: str, model: str, effort: str) -> None:
    assert (cfg.ladder.rungs[rung].model, cfg.ladder.rungs[rung].effort) == (
        model,
        effort,
    )
    assert cfg.ladder.max_rung == "R5"


def test_paths_default_windows(cfg: Config) -> None:
    assert cfg.paths.work_folder == r"C:\git\.ultron-work"
    assert cfg.paths.ultron_home == r"C:\git\.ultron"
