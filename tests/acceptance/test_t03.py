"""Acceptance tests for T03 (AC1-AC5), written independently of the developer."""

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
TTY = ROOT / "spikes" / "local" / "tty_check.py"
SDK = ROOT / "spikes" / "local" / "sdk_check.py"
FINDINGS = ROOT / "sdlc" / "v0" / "findings.md"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(
    script: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        input="",
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# AC1
def test_ac1_piped_stdin_not_a_terminal_exit_3() -> None:
    proc = _run(TTY)
    assert "not a terminal" in proc.stdout
    assert proc.returncode == 3


def test_ac1_is_interactive_reusable_and_false_when_piped() -> None:
    mod = _load(TTY, "tty_check_mod")
    assert callable(mod.is_interactive)
    # under pytest capture, stdin/stdout are not both terminals
    assert mod.is_interactive() is False


def test_ac1_main_returns_zero_with_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load(TTY, "tty_check_mod2")

    class FakeTty:
        def isatty(self) -> bool:
            return True

        def write(self, _s: str) -> int:
            return len(_s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(mod.sys, "stdin", FakeTty())
    monkeypatch.setattr(mod.sys, "stdout", FakeTty())
    assert mod.main() == 0


# AC2, AC3, AC4 owner steps present and runnable
def test_ac2_ac3_owner_steps_and_slots_exist() -> None:
    text = FINDINGS.read_text(encoding="utf-8")
    for shell in (
        "PowerShell 5.1",
        "Windows Terminal",
        "Git Bash",
        "Linux",
        "Claude Code Bash tool",
    ):
        assert re.search(rf"^\|\s*{re.escape(shell)}", text, re.MULTILINE), shell
    assert "uv run python spikes/local/tty_check.py" in text
    assert "not a terminal" in text


def test_ac2_owner_command_runs_as_written() -> None:
    proc = subprocess.run(
        ["uv", "run", "python", "spikes/local/tty_check.py"],
        cwd=ROOT,
        input="",
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "UV"},
    )
    assert "not a terminal" in proc.stdout
    assert proc.returncode == 3


def test_ac4_slots_exist_in_findings() -> None:
    text = FINDINGS.read_text(encoding="utf-8")
    assert "uv run --group spikes python spikes/local/sdk_check.py" in text
    assert "SDK per model" in text
    assert "ANTHROPIC_API_KEY unset" in text
    assert "Default adapter" in text


def test_ac4_sdk_check_refuses_with_api_key() -> None:
    env = dict(os.environ, ANTHROPIC_API_KEY="dummy")
    proc = _run(SDK, env=env)
    assert proc.returncode != 0
    assert "ANTHROPIC_API_KEY" in proc.stdout + proc.stderr


def test_ac4_sdk_check_renders_table_with_stub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load(SDK, "sdk_check_mod")
    assert mod.MODELS == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def fake(model: str, effort: str = "low") -> dict[str, object]:
        return {
            "requested_model": model,
            "requested_effort": effort,
            "reported_model": model,
            "effort_applied": effort,
            "ok": model != "claude-opus-5-5",
            "detail": "a|b" if model == "claude-opus-5-5" else "ok",
            "seconds": 1.0,
            "api_key_present": False,
        }

    monkeypatch.setattr(mod, "run_check", fake)
    assert mod.main() == 0
    out = capsys.readouterr().out
    for m in mod.MODELS:
        assert m in out
    assert out.count("FAIL") == 1
    assert "absent" in out
    assert "a|b" not in out  # pipe escaped so the table stays intact


# AC5
def test_ac5_pinned_version_in_pyproject_lock_and_findings() -> None:
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for grp in py["dependency-groups"].values() for d in grp]
    pins = [d for d in deps if d.startswith("claude-agent-sdk")]
    assert len(pins) == 1
    m = re.fullmatch(r"claude-agent-sdk==(\d+\.\d+\.\d+)", pins[0])
    assert m, pins
    ver = m.group(1)
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(rf'name = "claude-agent-sdk"\nversion = "{re.escape(ver)}"', lock)
    assert f"claude-agent-sdk=={ver}" in FINDINGS.read_text(encoding="utf-8")


def test_ac5_findings_names_default_adapter_slot() -> None:
    text = FINDINGS.read_text(encoding="utf-8")
    assert re.search(r"Default adapter:", text)
    assert "T16" in text and "T17" in text
