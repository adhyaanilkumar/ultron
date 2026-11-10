"""AC2: 4 processes appending 50 events each give 200 valid lines with unique, gap-free `seq`."""

import json
import subprocess
import sys
import time
from pathlib import Path

from ultron.store.eventlog import EventLog

WORKERS = 4
PER_WORKER = 50
TIMEOUT_S = 60

WORKER = """
import sys, time
from pathlib import Path
from ultron.store.eventlog import EventLog
from ultron.store.events import NewEvent

log_path, ready, go, worker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
log = EventLog(log_path)
ready.touch()
deadline = time.monotonic() + 60
while not go.exists():
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.002)
for i in range(int(sys.argv[5])):
    log.append(NewEvent(actor="engine", type="probe", requirement="R1",
                        data={"worker": worker, "i": i}))
"""


def _wait_for(paths: list[Path], deadline: float) -> None:
    while not all(p.exists() for p in paths):
        assert time.monotonic() < deadline, "workers did not start in time"
        time.sleep(0.01)


def test_concurrent_appends(tmp_path: Path) -> None:
    log_path = tmp_path / "sdlc" / "requirements" / "R1" / "events.jsonl"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{w}" for w in range(WORKERS)]
    deadline = time.monotonic() + TIMEOUT_S
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                WORKER,
                str(log_path),
                str(ready[w]),
                str(go),
                str(w),
                str(PER_WORKER),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for w in range(WORKERS)
    ]
    try:
        _wait_for(ready, deadline)
        go.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, err.decode(errors="replace")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    data = log_path.read_bytes()
    assert data.endswith(b"\n")
    lines = data.split(b"\n")[:-1]
    assert len(lines) == WORKERS * PER_WORKER
    objs = [json.loads(line) for line in lines]
    assert sorted(o["seq"] for o in objs) == list(range(1, WORKERS * PER_WORKER + 1))
    assert [o["seq"] for o in objs] == list(range(1, WORKERS * PER_WORKER + 1))
    for w in range(WORKERS):
        mine = [o["data"]["i"] for o in objs if o["data"]["worker"] == str(w)]
        assert mine == list(range(PER_WORKER))

    result = EventLog(log_path).read(strict=True)
    assert len(result.events) == WORKERS * PER_WORKER
