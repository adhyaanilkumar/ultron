"""AC3: every query leaves the activity file, the cache and the event log byte-identical."""

import hashlib
from pathlib import Path

from monitor_helpers import REQ, in_progress, n

from ultron.monitor import append_activity, follow, snapshot, transcript_tail
from ultron.store.cache import Cache
from ultron.store.eventlog import events_path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_queries_change_nothing(tmp_path: Path) -> None:
    log = in_progress(tmp_path)
    log.append([n(REQ, "agent_started", "T1", 1, role="developer")])
    udir = tmp_path / ".ultron"
    append_activity(
        udir,
        [
            {
                "requirement": REQ,
                "task": "T1",
                "role": "developer",
                "kind": "started",
                "text": "go",
                "data": {},
            },
            {
                "requirement": REQ,
                "task": "T1",
                "role": "developer",
                "kind": "usage",
                "text": "u",
                "data": {"input": 1},
            },
        ],
    )
    tdir = udir / "transcripts" / REQ
    tdir.mkdir(parents=True)
    (tdir / "T1-a1-developer-1.json").write_text("{\n}\n", encoding="utf-8")
    Cache(
        tmp_path
    ).refresh()  # the cache is built once; queries must not touch it again

    files = [
        udir / "activity.jsonl",
        udir / "state.db",
        events_path(tmp_path, REQ),
    ]
    before = [_sha(f) for f in files]
    seq_before = (udir / "activity.seq").read_bytes()
    for _ in range(2):
        snapshot(tmp_path, udir)
        follow(udir, 0)
        follow(udir, 1)
        transcript_tail(udir, f"{REQ}/T1", 5)
    assert [_sha(f) for f in files] == before
    assert (udir / "activity.seq").read_bytes() == seq_before
