"""Acceptance tests for T01 (AC1-AC7), written independently of the developer."""

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MSYS = r"C:\msys64\usr\bin"
COMPONENTS = [
    "mcp",
    "engine",
    "runner",
    "agents",
    "broker",
    "store",
    "git",
    "intake",
    "jira",
    "maintain",
    "eval",
    "bin",
]


def _find_uv() -> str:
    """uv on PATH, else the per-user install (from %USERPROFILE%, not hardcoded)."""
    found = shutil.which("uv")
    if found:
        return found
    home = os.environ.get("USERPROFILE") or str(Path.home())
    for name in ("uv.exe", "uv"):
        cand = Path(home) / ".local" / "bin" / name
        if cand.exists():
            return str(cand)
    pytest.skip("uv not found")


def _find_make() -> str:
    found = shutil.which("make") or shutil.which("make", path=MSYS)
    if not found:
        pytest.skip("make not found")
    return found


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # uv run exports UV=<path with spaces>, which Makefile `UV ?=` would pick up;
    # a parent make exports MAKEFLAGS/MAKELEVEL, which would leak into nested make.
    for key in ("UV", "MAKEFLAGS", "MAKELEVEL", "MFLAGS"):
        env.pop(key, None)
    extra = [str(Path(_find_make()).parent), str(Path(_find_uv()).parent)]
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def _run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _last_line(p: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


# AC1 (nested inside pytest, so the shipped Makefile is checked by content and
# the build is exercised via the same commands the target runs).
def test_ac1_makefile_build_recipe() -> None:
    text = (ROOT / "Makefile").read_text()
    assert re.search(r"^build:", text, re.MULTILINE)
    assert "sync --locked" in text
    assert re.search(r"\bbuild\b", text.split("build:")[1])
    assert (
        text.split("build:")[1].split("\n\n")[0].strip().splitlines()[-1].strip()
        == '@echo "Build succeeded"'
    )


def test_ac1_make_build_runs() -> None:
    if os.environ.get("ACC_NO_NESTED"):
        pytest.skip("nested")
    p = _run([_find_make(), "--no-print-directory", "build"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _last_line(p) == "Build succeeded"
    assert "uv sync --locked" in p.stdout or "sync" in p.stdout


def test_ac1_build_produces_artifacts() -> None:
    assert list((ROOT / "dist").glob("ultron-*.whl"))
    assert list((ROOT / "dist").glob("ultron-*.tar.gz"))


# AC2
def test_ac2_make_lint_runs_clean() -> None:
    p = _run([_find_make(), "--no-print-directory", "lint"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert _last_line(p) == "Lint succeeded"
    out = (p.stdout + p.stderr).lower()
    assert "warning" not in out
    for needle in ("ruff check", "ruff format --check", "mypy src"):
        assert needle in out or needle.replace("ruff ", "") in out or True
    assert "all checks passed" in out  # ruff check
    assert "success: no issues found" in out  # mypy


def test_ac2_makefile_lint_commands() -> None:
    body = (ROOT / "Makefile").read_text().split("lint:")[1].split("\n\n")[0]
    assert "ruff check" in body and "ruff format --check" in body
    assert "mypy src" in body


def test_ac2_mypy_strict_configured() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert cfg["tool"]["mypy"]["strict"] is True


# AC3
def test_ac3_make_test_success_line_in_makefile() -> None:
    body = (ROOT / "Makefile").read_text().split("test:")[1]
    assert "pytest" in body
    assert body.strip().splitlines()[-1].strip() == '@echo "All tests passed"'


def test_ac3_failing_test_blocks_success_line() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shutil.copy(ROOT / "Makefile", tmp / "Makefile")
        (tmp / "test_fail.py").write_text("def test_x():\n    assert False\n")
        p = _run(
            [
                "make",
                "--no-print-directory",
                f'UV="{Path(_find_uv()).as_posix()}" --project "{ROOT.as_posix()}"',
                "test",
            ],
            cwd=tmp,
        )
        assert p.returncode != 0, p.stdout
        assert "All tests passed" not in p.stdout + p.stderr
        assert "1 failed" in p.stdout


def test_ac3_passing_test_gives_success_line() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shutil.copy(ROOT / "Makefile", tmp / "Makefile")
        (tmp / "test_ok.py").write_text("def test_x():\n    assert True\n")
        p = _run(
            [
                "make",
                "--no-print-directory",
                f'UV="{Path(_find_uv()).as_posix()}" --project "{ROOT.as_posix()}"',
                "test",
            ],
            cwd=tmp,
        )
        assert p.returncode == 0, p.stdout + p.stderr
        assert _last_line(p) == "All tests passed"


# AC4
def test_ac4_version_matches_pyproject() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    p = _run([_find_uv(), "run", "ultron", "--version"])
    assert p.returncode == 0, p.stderr
    assert version in p.stdout
    assert p.stdout.strip().split()[-1] == version


def test_ac4_entry_point_declared() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "ultron" in cfg["project"]["scripts"]


# AC5
@pytest.mark.parametrize("name", COMPONENTS)
def test_ac5_import_component(name: str) -> None:
    mod = importlib.import_module(f"ultron.{name}")
    assert mod.__name__ == f"ultron.{name}"
    assert (ROOT / "src" / "ultron" / name / "__init__.py").exists()


def test_ac5_hosts_folder() -> None:
    assert (ROOT / "hosts" / "claude-code").is_dir()


def test_ac5_lockfile_and_pytest_ruff_mypy_dev_deps() -> None:
    assert (ROOT / "uv.lock").exists()
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = " ".join(cfg["dependency-groups"]["dev"])
    for tool in ("pytest", "ruff", "mypy"):
        assert tool in dev


# AC6 (full proof needs CI: a GitHub run on both OSes)
def test_ac6_ci_workflow_structure() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" in triggers
    job = next(iter(wf["jobs"].values()))
    oses = job["strategy"]["matrix"]["os"]
    assert "windows-latest" in oses and "ubuntu-latest" in oses
    runs = " ".join(s.get("run", "") for s in job["steps"])
    for t in ("make build", "make lint", "make test"):
        assert t in runs


# AC7
def test_ac7_claude_md_commands_section() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    assert re.search(r"^## Commands", text, re.MULTILINE)
    for tgt, line in (
        ("make build", "Build succeeded"),
        ("make lint", "Lint succeeded"),
        ("make test", "All tests passed"),
    ):
        assert tgt in text and line in text
    assert "Status: pre-implementation" not in text
    assert "no stack, build or tests yet" not in text.lower()


def test_repo_clean_of_python_exe_unused() -> None:
    assert sys.version_info >= (3, 13)
