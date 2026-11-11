"""The Makefile finds `uv` on PATH, or else in the per-user install on Windows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _dry_run_build(env: dict[str, str]) -> str:
    make = shutil.which("make", path=env.get("PATH"))
    if make is None:
        pytest.skip("make is not on PATH")
    result = subprocess.run(
        [make, "--no-print-directory", "-n", "build"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_uses_uv_from_path(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(fake_bin), env.get("PATH", "")])
    env.pop("UV", None)

    out = _dry_run_build(env)

    assert "uv sync --locked" in out


@pytest.mark.skipif(sys.platform != "win32", reason="per-user uv install is Windows")
def test_falls_back_to_user_install_when_uv_not_on_path(tmp_path: Path) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not on PATH")
    msys_bin = Path(make).parent
    if not (msys_bin / "cygpath.exe").exists():
        pytest.skip("cygpath is not beside make")
    if shutil.which("uv", path=str(msys_bin)) is not None:
        pytest.skip("uv is on the MSYS2 bin folder; the fallback is not reached")

    profile = tmp_path / "Anil Kumar ML"
    uv_exe = profile / ".local" / "bin" / "uv.exe"
    uv_exe.parent.mkdir(parents=True)
    uv_exe.write_bytes(b"")
    env = dict(os.environ)
    env["PATH"] = str(msys_bin)
    env["USERPROFILE"] = str(profile)
    env.pop("UV", None)

    out = _dry_run_build(env)

    # The per-user path is used, with its spaces escaped for the shell.
    assert "uv.exe sync --locked" in out
    assert "Anil\\ Kumar\\ ML" in out
