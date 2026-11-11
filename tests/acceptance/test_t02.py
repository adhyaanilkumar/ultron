"""T02 acceptance tests (testing agent, attempt 1)."""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "spikes" / "host"
FINDINGS = ROOT / "sdlc" / "v0" / "findings.md"


def _run_hook(
    stdin: str, log: Path | None, cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "SPIKE_HOOK_LOG"}
    if log is not None:
        env["SPIKE_HOOK_LOG"] = str(log)
    return subprocess.run(
        [sys.executable, str(HOST / "hook.py")],
        input=stdin.encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
        cwd=cwd,
        check=False,
    )


# AC1
def test_ac1_files_exist() -> None:
    assert (HOST / "server.py").is_file()
    assert (HOST / "hook.py").is_file()


def test_ac1_self_test_exits_zero() -> None:
    r = subprocess.run(
        [sys.executable, str(HOST / "server.py"), "--self-test"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stderr


def test_ac1_server_lists_tool_over_stdio() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go() -> list[str]:
        params = StdioServerParameters(
            command=sys.executable, args=[str(HOST / "server.py")]
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]

    names = asyncio.run(asyncio.wait_for(go(), 30))
    assert names == ["spike_probe"]


def test_probe_tool_exercises_elicitation_progress() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ElicitRequestParams, ElicitResult

    elicited: list[object] = []
    progressed: list[object] = []

    async def elicit(ctx: object, params: ElicitRequestParams) -> ElicitResult:
        elicited.append(params)
        return ElicitResult(action="accept", content={"approve": True, "comment": "hi"})

    async def progress(p: float, total: float | None, msg: str | None) -> None:
        progressed.append((p, total, msg))

    async def go() -> str:
        params = StdioServerParameters(
            command=sys.executable, args=[str(HOST / "server.py")]
        )
        async with (
            stdio_client(params) as (r, w),
            ClientSession(r, w, elicitation_callback=elicit) as s,  # type: ignore[arg-type]
        ):
            await s.initialize()
            res = await s.call_tool("spike_probe", {}, progress_callback=progress)
            return str(res.content[0].text)  # type: ignore[union-attr]

    out = asyncio.run(asyncio.wait_for(go(), 60))
    assert elicited
    assert progressed
    assert "action=accept" in out
    assert "approve=True" in out


def test_probe_survives_client_without_elicitation() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go() -> str:
        params = StdioServerParameters(
            command=sys.executable, args=[str(HOST / "server.py")]
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("spike_probe", {})
            return str(res.content[0].text)  # type: ignore[union-attr]

    out = asyncio.run(asyncio.wait_for(go(), 60))
    assert "elicitation" in out


# AC4 support: the hook never blocks, always exits 0, logs every call
@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        "null",
        "123",
        '"str"',
        "{}",
        '{"tool_name": 5, "tool_input": "x"}',
        '{"tool_name": "Bash", "tool_input": null}',
        '{"tool_name": "Bash", "tool_input": {"command": 7}}',
        '{"tool_name": "Bash", "tool_input": {"command": "echo \\u00e9\\u4e2d"}}',
        "garbage é中",
    ],
)
def test_hook_always_exit0_silent_and_logs(stdin: str, tmp_path: Path) -> None:
    log = tmp_path / "h.log"
    r = _run_hook(stdin, log)
    assert r.returncode == 0
    assert r.stdout == b""
    assert log.is_file()


def test_hook_one_line_per_call(tmp_path: Path) -> None:
    log = tmp_path / "h.log"
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo a\necho b"}})
    _run_hook(ev, log)
    _run_hook(ev, log)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_hook_exit0_when_log_unwritable(tmp_path: Path) -> None:
    r = _run_hook("{}", tmp_path / "no" / "such" / "dir" / "h.log")
    assert r.returncode == 0
    assert r.stdout == b""


def test_hook_default_log_next_to_script_ignoring_cwd(tmp_path: Path) -> None:
    default = HOST / "hook.log"
    existed = default.exists()
    before = default.read_text(encoding="utf-8") if existed else ""
    try:
        ev = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "echo marker-t02-xyz"}}
        )
        r = _run_hook(ev, None, cwd=tmp_path)
        assert r.returncode == 0
        assert "marker-t02-xyz" in default.read_text(encoding="utf-8")
        assert not (tmp_path / "hook.log").exists()
    finally:
        if existed:
            default.write_text(before, encoding="utf-8")
        else:
            default.unlink(missing_ok=True)


def test_hook_log_gitignored() -> None:
    r = subprocess.run(
        ["git", "check-ignore", "spikes/host/hook.log"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0


# AC2-AC4: owner steps exist and are runnable as written
def test_owner_steps_readme() -> None:
    text = (HOST / "README.md").read_text(encoding="utf-8")
    for needle in (
        "claude mcp add spike",
        "server.py",
        "settings.snippet.json",
        "spike_probe",
        "! echo",
        "hook.log",
        "findings.md",
        "claude --version",
        "claude mcp remove spike",
    ):
        assert needle in text, needle


def test_settings_snippet_valid_and_points_to_hook() -> None:
    d = json.loads((HOST / "settings.snippet.json").read_text(encoding="utf-8"))
    entries = d["hooks"]["PreToolUse"]
    assert entries
    cmd = entries[0]["hooks"][0]
    assert cmd["type"] == "command"
    assert entries[0]["matcher"] in ("*", "", "Bash")
    assert "spikes/host/hook.py" in cmd["command"]


# AC5
def _host_rows() -> list[list[str]]:
    text = FINDINGS.read_text(encoding="utf-8")
    assert re.search(r"^## Host\s*$", text, re.MULTILINE)
    section = text.split("## Host", 1)[1]
    section = re.split(r"^## ", section, flags=re.MULTILINE)[0]
    rows = [
        ln
        for ln in section.splitlines()
        if ln.startswith("|") and not re.match(r"^\|\s*-", ln)
    ]
    return [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in rows]


def test_ac5_header_columns() -> None:
    header = [c.lower() for c in _host_rows()[0]]
    assert len(header) == 5
    assert "question" in header[0]
    assert "result" in header[1]
    assert "version" in header[2]
    assert "evidence" in header[3]
    for t in ("t36", "t38", "t41"):
        assert t in header[4]


def test_ac5_slot_per_question() -> None:
    rows = _host_rows()[1:]
    assert all(len(r) == 5 for r in rows)
    joined = " ".join(r[0].lower() for r in rows)
    assert "elicitation" in joined
    assert "notification" in joined or "progress" in joined
    assert "!" in joined
    assert "version" in joined
    assert len(rows) >= 4


def test_ac2_ac3_ac4_have_slots_in_findings() -> None:
    text = FINDINGS.read_text(encoding="utf-8").lower()
    for needle in (
        "form",
        "model",
        "progress",
        "resource-update",
        "hook",
        "hook.log",
        "screenshot",
    ):
        assert needle in text, needle


# Rerun additions (R1): README gives the owner commands and screenshot instructions
def test_readme_screenshot_and_observation_instructions() -> None:
    text = (HOST / "README.md").read_text(encoding="utf-8").lower()
    for needle in (
        "screenshot",
        "sdlc/v0/evidence",
        "progress",
        "resource-update",
        "pending",
    ):
        assert needle in text, needle


def test_ac5_exactly_one_row_per_question() -> None:
    rows = _host_rows()[1:]
    assert len(rows) == 4
    questions = [r[0].lower() for r in rows]
    for key in ("elicitation", "notification", "`!`", "version"):
        assert sum(key in q for q in questions) == 1, key


def test_ac6_screenshot_paths_named_in_host_section_exist() -> None:
    # Vacuous while cells are PENDING (owner fills them); checks paths once filled.
    section = FINDINGS.read_text(encoding="utf-8").split("## Host", 1)[1]
    section = re.split(r"^## ", section, flags=re.MULTILINE)[0]
    for m in re.findall(r"[\w./\\-]+\.(?:png|jpg|jpeg|gif|webp)", section):
        assert (ROOT / m).is_file(), m
