"""T15 AC2 and AC3: the logging wrapper writes a transcript and an event, and checks the cap first."""

from decimal import Decimal
from pathlib import Path

import pytest

from ultron.agents.calllog import CallContext, CapRefusedError, LoggingRunner
from ultron.agents.fake import FakeAgentRunner, FakeStep
from ultron.agents.interface import (
    ActivityEvent,
    ActivityKind,
    RunLimits,
    StopReason,
    Usage,
)
from ultron.engine.costs import DEFAULT_PRICES, CapLimits, UsageLedger, UsageRecord
from ultron.store.eventlog import EventLog
from ultron.store.events import InvalidEventError

LIMITS = RunLimits(model="opus-5.5", effort="high")
CTX = CallContext("R1", task="T1", attempt=2)


def make(
    tmp_path: Path, script: list[FakeStep], cap: str = "10"
) -> tuple[LoggingRunner, FakeAgentRunner, EventLog, UsageLedger]:
    fake = FakeAgentRunner(script)
    log = EventLog(tmp_path / "events.jsonl")
    ledger = UsageLedger(DEFAULT_PRICES)
    runner = LoggingRunner(
        fake,
        ultron_dir=tmp_path / ".ultron",
        log=log,
        ledger=ledger,
        caps=CapLimits(Decimal(cap), Decimal(cap)),
    )
    return runner, fake, log, ledger


def go(runner: LoggingRunner, tmp_path: Path, ctx: CallContext = CTX):  # type: ignore[no-untyped-def]
    return runner.run(ctx, "developer", "the prompt", tmp_path, ["Read"], None, LIMITS)


def test_one_transcript_and_one_event_with_every_fr17_field(tmp_path: Path) -> None:
    step = FakeStep(
        output="done",
        usage=Usage(1_000_000, 0),
        duration_ms=1234,
        effort="medium",
        model="claude-sonnet-5-5",
        activity=[ActivityEvent(ActivityKind.THINKING, "planning")],
    )
    runner, _, log, ledger = make(tmp_path, [step])
    go(runner, tmp_path)

    transcripts = list((tmp_path / ".ultron" / "transcripts").rglob("*.json"))
    assert len(transcripts) == 1
    events = log.read().events
    assert len(events) == 1
    e = events[0]
    assert (e.type, e.requirement, e.task, e.attempt) == ("agent_call", "R1", "T1", 2)
    d = e.data
    for key in (
        "role",
        "model",
        "effort",
        "duration_ms",
        "tokens",
        "cost_usd",
        "transcript",
        "prompt_sha256",
        "result_sha256",
        "stop_reason",
    ):
        assert key in d, key
    assert d["role"] == "developer"
    # The model and effort that actually ran are recorded, not the requested ones.
    assert (d["model"], d["effort"]) == ("claude-sonnet-5-5", "medium")
    assert d["duration_ms"] == 1234
    assert set(d["tokens"]) == {"input", "output", "cache_read", "cache_write"}
    assert d["cost_usd"] == pytest.approx(2.0)
    assert d["stop_reason"] == "done"
    assert (tmp_path / ".ultron" / d["transcript"]).is_file()
    assert ledger.total("R1") == Decimal("2.00")
    assert (tmp_path / ".ultron" / "activity.jsonl").read_text(encoding="utf-8").count(
        "planning"
    ) == 1


def test_transcript_records_requested_and_ran(tmp_path: Path) -> None:
    runner, _, log, _ = make(tmp_path, [FakeStep(effort="low")])
    go(runner, tmp_path)
    path = tmp_path / ".ultron" / log.read().events[0].data["transcript"]
    text = path.read_text(encoding="utf-8")
    assert '"requested"' in text and '"ran"' in text and "the prompt" in text


def test_two_calls_two_transcripts(tmp_path: Path) -> None:
    runner, _, log, _ = make(tmp_path, [FakeStep(), FakeStep()])
    go(runner, tmp_path)
    go(runner, tmp_path)
    assert len(list((tmp_path / ".ultron" / "transcripts").rglob("*.json"))) == 2
    assert len(log.read().events) == 2


def test_stop_reason_is_logged(tmp_path: Path) -> None:
    runner, _, log, _ = make(tmp_path, [FakeStep(stop_reason=StopReason.USAGE_LIMIT)])
    go(runner, tmp_path)
    assert log.read().events[0].data["stop_reason"] == "usage_limit"


def test_refused_call_writes_nothing_and_starts_no_agent(tmp_path: Path) -> None:
    runner, fake, log, _ = make(tmp_path, [FakeStep(usage=Usage(1_000_000, 0))], "1")
    go(runner, tmp_path)  # 2 USD spent, cap 1
    with pytest.raises(CapRefusedError):
        go(runner, tmp_path)
    assert len(fake.calls) == 1
    assert len(list((tmp_path / ".ultron" / "transcripts").rglob("*.json"))) == 1
    assert len(log.read().events) == 1


def test_refused_first_call_leaves_no_files(tmp_path: Path) -> None:
    runner, fake, _log, ledger = make(tmp_path, [FakeStep()], "1")
    ledger.add("R1", UsageRecord("opus-5.5", {"output": 1_000_000}))
    with pytest.raises(CapRefusedError):
        go(runner, tmp_path)
    assert fake.calls == []
    assert not (tmp_path / ".ultron").exists()
    assert not (tmp_path / "events.jsonl").exists()


def test_evaluation_run_cap_is_checked(tmp_path: Path) -> None:
    runner, fake, _, _ = make(
        tmp_path, [FakeStep(usage=Usage(1_000_000, 0)), FakeStep()], "1"
    )
    ctx = CallContext("R1", run="E1")
    runner.run(ctx, "developer", "p", tmp_path, [], None, LIMITS)
    with pytest.raises(CapRefusedError):
        runner.run(ctx, "developer", "p", tmp_path, [], None, LIMITS)
    assert len(fake.calls) == 1


# T18 attempt 3: a hostile (non-Unicode) path must never stop the call from being logged


def test_non_utf8_context_values_are_escaped_in_the_event(tmp_path: Path) -> None:
    runner, fake, log, _ = make(tmp_path, [FakeStep()])
    entry = {
        "path": "bad\udc80/CLAUDE.md",
        "sha256": "0" * 64,
        "kind": "repo_instruction",
        "truncated": False,
    }
    ctx = CallContext(
        "R1", context_files=(entry,), context_omitted=("x\udcff/AGENTS.md",)
    )
    go(runner, tmp_path, ctx)
    (e,) = log.read().events
    assert e.data["context_files"] == [{**entry, "path": "bad\\udc80/CLAUDE.md"}]
    assert e.data["context_omitted"] == ["x\\udcff/AGENTS.md"]
    assert len(fake.calls) == 1


@pytest.mark.parametrize("bad", ["prompt", "role", "requirement", "task"])
def test_event_it_cannot_write_is_refused_before_the_agent_runs(
    bad: str, tmp_path: Path
) -> None:
    runner, fake, _log, _ = make(tmp_path, [FakeStep()])
    ctx, role, prompt = CTX, "developer", "the prompt"
    if bad == "prompt":
        prompt = "lone \ud800 surrogate"
    elif bad == "role":
        role = "Not A Role"
    elif bad == "requirement":
        ctx = CallContext("R\udc80")
    else:
        ctx = CallContext("R1", task="T\udc80")
    with pytest.raises(InvalidEventError):
        runner.run(ctx, role, prompt, tmp_path, ["Read"], None, LIMITS)
    assert fake.calls == [], "the agent must not run (and be charged) first"
    assert not (tmp_path / ".ultron").exists()
    assert not (tmp_path / "events.jsonl").exists()
