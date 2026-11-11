"""AC5: the claude-agent-sdk pin agrees in pyproject.toml, uv.lock and findings.md."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_pin() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps: list[str] = []
    for group in data.get("dependency-groups", {}).values():
        deps += [d for d in group if isinstance(d, str)]
    deps += data.get("project", {}).get("dependencies", [])
    pins = [
        m.group(1)
        for d in deps
        if (m := re.fullmatch(r"claude-agent-sdk==([\w.]+)", d))
    ]
    assert len(pins) >= 1, "claude-agent-sdk must be pinned with =="
    assert len(set(pins)) == 1
    return pins[0]


def test_pin_agrees_across_pyproject_lock_and_findings() -> None:
    pin = _pyproject_pin()
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "claude-agent-sdk"]
    assert locked == [pin]
    findings = (ROOT / "sdlc" / "v0" / "findings.md").read_text(encoding="utf-8")
    assert f"claude-agent-sdk=={pin}" in findings
