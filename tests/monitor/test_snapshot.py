"""AC2: the snapshot shows the last 5 activities and `attempt 2/3` for a running fake agent."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from monitor_helpers import REQ, in_progress, n

from ultron.agents.calllog import CallContext, LoggingRunner
from ultron.agents.fake import FakeAgentRunner, FakeStep
from ultron.agents.interface import ActivityEvent, ActivityKind, RunLimits
from ultron.engine.costs import DEFAULT_PRICES, CapLimits, UsageLedger
from ultron.monitor import snapshot
from ultron.store.eventlog import EventLog, events_path


def test_snapshot_shows_last_five_and_attempt(tmp_path: Path) -> None:
    log = in_progress(tmp_path)
    # a first, finished call for attempt 1, then attempt 2 running
    udir = tmp_path / ".ultron"
    acts = [ActivityEvent(ActivityKind.STARTED, "developer started")] + [
        ActivityEvent(ActivityKind.EDIT, f"edit f{i}") for i in range(6)
    ]
    acts.append(ActivityEvent(ActivityKind.USAGE, "so far", {"input": 10, "output": 4}))
    runner = LoggingRunner(
        FakeAgentRunner([FakeStep(activity=acts)]),
        ultron_dir=udir,
        log=EventLog(events_path(tmp_path, REQ)),
        ledger=UsageLedger(DEFAULT_PRICES),
        caps=CapLimits(Decimal(100), Decimal(100)),
    )
    runner.run(
        CallContext(REQ, "T1", 2),
        "developer",
        "p",
        tmp_path,
        [],
        None,
        RunLimits("claude-sonnet-5-5", "medium"),
    )
    # the call finished and closed the agent; open it again, as the engine does at the start of attempt 2
    log.append([n(REQ, "agent_started", "T1", 2, role="developer")])

    got = snapshot(tmp_path, udir, now=datetime.now(UTC) + timedelta(seconds=30))
    assert got["untrusted"] is True
    [agent] = got["running"]
    assert agent["role"] == "developer" and agent["task"] == "T1"
    assert agent["requirement"] == REQ
    assert agent["attempt"] == "2/3"
    assert [r["text"] for r in agent["recent"]] == [
        "edit f2",
        "edit f3",
        "edit f4",
        "edit f5",
        "so far",
    ]
    assert agent["usage"] == {"input": 10, "output": 4}
    assert agent["elapsed_s"] is not None and agent["elapsed_s"] >= 25
    assert [q["task"] for q in got["queued"]] == ["T2"]
    assert got["cursor"] == 8
