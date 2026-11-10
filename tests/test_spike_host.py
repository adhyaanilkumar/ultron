"""T02 spike checks: server self-test, hook logging, findings table shape."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "spikes" / "host"


def test_server_self_test() -> None:
    r = subprocess.run(
        [sys.executable, str(HOST / "server.py"), "--self-test"],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "self-test ok" in r.stdout


def test_hook_logs_and_allows(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    event = {"tool_name": "Bash", "tool_input": {"command": "echo spike-bang-test"}}
    r = subprocess.run(
        [sys.executable, str(HOST / "hook.py")],
        input=json.dumps(event),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        env={"SPIKE_HOOK_LOG": str(log), "PATH": ""},
    )
    assert r.returncode == 0
    assert r.stdout == ""
    line = log.read_text(encoding="utf-8").strip()
    assert "Bash" in line
    assert "echo spike-bang-test" in line


def test_hook_survives_garbage_input(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(HOST / "hook.py")],
        input="not json",
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        env={"SPIKE_HOOK_LOG": str(tmp_path / "hook.log"), "PATH": ""},
    )
    assert r.returncode == 0
    assert r.stdout == ""


def test_findings_host_table() -> None:
    text = (ROOT / "sdlc" / "v0" / "findings.md").read_text(encoding="utf-8")
    assert "## Host" in text
    section = text.split("## Host", 1)[1]
    rows = [
        ln
        for ln in section.splitlines()
        if ln.startswith("|") and not ln.startswith("|---") and "Question" not in ln
    ]
    assert len(rows) == 4
    for ln in rows:
        assert len(ln.strip().strip("|").split("|")) == 5
