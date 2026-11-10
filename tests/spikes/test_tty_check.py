"""AC1: tty_check reports 'not a terminal' with exit 3 when stdin is a pipe."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "spikes" / "local" / "tty_check.py"


def test_piped_stdin_reports_not_a_terminal() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert "not a terminal" in proc.stdout
    assert "stdin=False" in proc.stdout
    assert proc.returncode == 3
