"""Acceptance tests for T15 (AgentRunner interface, fake runner, activity events, call log), by the testing agent.

Expected costs are computed by hand from list prices (USD per million tokens): sonnet 2/10, cache read 0.10,
cache write 2.50; opus 4/20, cache read 0.20, cache write 5; haiku 0.50/2.50 (upper-bound tier).
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ultron.agents import interface
from ultron.agents.calllog import CallContext, CapRefusedError, LoggingRunner
from ultron.agents.fake import FakeAgentRunner, FakeStep, ScriptExhaustedError
from ultron.agents.interface import (
    ActivityEvent,
    ActivityKind,
    AgentResult,
    RunLimits,
    StopReason,
    Usage,
)
from ultron.engine.costs import DEFAULT_PRICES, CapLimits, UsageLedger, UsageRecord
from ultron.store.eventlog import EventLog

M = 1_000_000
LIMITS = RunLimits(model="opus-5.5", effort="high", max_turns=5)


def make(
    tmp_path: Path,
    script: list[FakeStep],
    req_cap: str = "100",
    run_cap: str = "100",
) -> tuple[LoggingRunner, FakeAgentRunner, EventLog, UsageLedger, Path]:
    fake = FakeAgentRunner(script)
    log = EventLog(tmp_path / "events.jsonl")
    ledger = UsageLedger(DEFAULT_PRICES)
    udir = tmp_path / ".ultron"
    runner = LoggingRunner(
        fake,
        ultron_dir=udir,
        log=log,
        ledger=ledger,
        caps=CapLimits(Decimal(req_cap), Decimal(run_cap)),
    )
    return runner, fake, log, ledger, udir


def call(
    runner: LoggingRunner,
    tmp_path: Path,
    ctx: CallContext,
    role: str = "developer",
    prompt: str = "do the thing",
) -> AgentResult:
    return runner.run(ctx, role, prompt, tmp_path, ["Read"], None, LIMITS)


def ctx1() -> CallContext:
    return CallContext("R", task="T", attempt=1)


# ---- AC1: fake runner ------------------------------------------------------------------------------------------


def test_ac1_results_in_order_and_activity_emitted(tmp_path: Path) -> None:
    a1 = ActivityEvent(ActivityKind.READ, "a.py", {"path": "a.py"})
    a2 = ActivityEvent(ActivityKind.EDIT, "b.py")
    fake = FakeAgentRunner(
        [
            FakeStep(output="one", data={"k": 1}, activity=[a1]),
            FakeStep(
                output="two", activity=[a1, a2], stop_reason=StopReason.TURN_LIMIT
            ),
            FakeStep(output="three"),
        ]
    )
    live: list[ActivityEvent] = []
    r1 = fake.run("r", "p1", tmp_path, ["Read"], None, LIMITS, on_activity=live.append)
    assert (r1.output, r1.data) == ("one", {"k": 1})
    assert live == [a1]
    live.clear()
    r2 = fake.run("r", "p2", tmp_path, [], None, LIMITS, on_activity=live.append)
    assert r2.output == "two" and live == [a1, a2]
    assert r2.stop_reason is StopReason.TURN_LIMIT
    assert list(r2.activity) == [a1, a2]
    r3 = fake.run("r", "p3", tmp_path, [], None, LIMITS)
    assert r3.output == "three" and r3.stop_reason is StopReason.DONE
    assert [c.prompt for c in fake.calls] == ["p1", "p2", "p3"]


def test_ac1_script_exhaustion_is_an_error(tmp_path: Path) -> None:
    fake = FakeAgentRunner([FakeStep(output="only")])
    fake.run("r", "p", tmp_path, [], None, LIMITS)
    with pytest.raises(ScriptExhaustedError):
        fake.run("r", "p", tmp_path, [], None, LIMITS)
    with pytest.raises(ScriptExhaustedError):
        FakeAgentRunner([]).run("r", "p", tmp_path, [], None, LIMITS)


def test_ac1_scripted_failures_raise_and_later_steps_still_run(tmp_path: Path) -> None:
    fake = FakeAgentRunner(
        [FakeStep(raises=RuntimeError("usage limit")), FakeStep(output="ok")]
    )
    with pytest.raises(RuntimeError, match="usage limit"):
        fake.run("r", "p", tmp_path, [], None, LIMITS)
    assert fake.run("r", "p", tmp_path, [], None, LIMITS).output == "ok"


def test_ac1_diff_written_and_scripted_stop_reasons(tmp_path: Path) -> None:
    fake = FakeAgentRunner(
        [FakeStep(diff={"src/x/y.py": "print(1)\n"})]
        + [FakeStep(stop_reason=s) for s in StopReason]
    )
    fake.run("r", "p", tmp_path, [], None, LIMITS)
    assert (tmp_path / "src" / "x" / "y.py").read_text(encoding="utf-8") == "print(1)\n"
    got = [
        fake.run("r", "p", tmp_path, [], None, LIMITS).stop_reason for _ in StopReason
    ]
    assert got == list(StopReason)


def test_ac1_ran_model_defaults_to_requested_and_can_differ(tmp_path: Path) -> None:
    fake = FakeAgentRunner([FakeStep(), FakeStep(model="haiku", effort="low")])
    r1 = fake.run("r", "p", tmp_path, [], None, LIMITS)
    assert (r1.model, r1.effort) == ("opus-5.5", "high")
    r2 = fake.run("r", "p", tmp_path, [], None, LIMITS)
    assert (r2.model, r2.effort) == ("haiku", "low")


# ---- AC2: one transcript + one event with every FR17 field -----------------------------------------------------


def test_ac2_every_fr17_field_present_and_correct(tmp_path: Path) -> None:
    step = FakeStep(
        output="result text",
        data={"a": [1, 2]},
        usage=Usage(input=M, output=M, cache_read=M, cache_write=M),
        duration_ms=4321,
        model="claude-sonnet-5-5",
        effort="medium",
        activity=[ActivityEvent(ActivityKind.THINKING, "hmm")],
    )
    runner, _, log, _, udir = make(tmp_path, [step])
    ctx = CallContext("REQ-1", task="T7", attempt=3)
    call(runner, tmp_path, ctx, role="tester", prompt="the exact prompt")

    files = [p for p in (udir / "transcripts").rglob("*") if p.is_file()]
    assert len(files) == 1
    events = log.read().events
    assert len(events) == 1
    e = events[0]
    assert e.type == "agent_call"
    assert (e.requirement, e.task, e.attempt) == ("REQ-1", "T7", 3)
    d = e.data
    assert d["role"] == "tester"
    assert d["duration_ms"] == 4321
    assert d["tokens"] == {"input": M, "output": M, "cache_read": M, "cache_write": M}
    # 2 + 10 + 0.10 + 2.50 = 14.60
    assert d["cost_usd"] == pytest.approx(14.6)
    assert d["model"] == "claude-sonnet-5-5" and d["effort"] == "medium"
    assert d["stop_reason"] == "done"
    for key in ("prompt_sha256", "result_sha256", "transcript"):
        assert d[key]
    assert d["prompt_sha256"] == hashlib.sha256(b"the exact prompt").hexdigest()
    tpath = udir / d["transcript"]
    assert tpath == files[0]
    text = tpath.read_text(encoding="utf-8")
    assert "the exact prompt" in text
    assert "result text" in text


def test_ac2_transcript_path_layout(tmp_path: Path) -> None:
    runner, _, log, _, udir = make(tmp_path, [FakeStep(), FakeStep(), FakeStep()])
    ctx = CallContext("REQ-1", task="T7", attempt=2)
    call(runner, tmp_path, ctx, role="developer")
    call(runner, tmp_path, ctx, role="developer")
    call(runner, tmp_path, ctx, role="tester")
    rels = sorted(e.data["transcript"] for e in log.read().events)
    assert rels == [
        "transcripts/REQ-1/T7-a2-developer-1.json",
        "transcripts/REQ-1/T7-a2-developer-2.json",
        "transcripts/REQ-1/T7-a2-tester-1.json",
    ]
    assert all((udir / r).is_file() for r in rels)


def test_ac2_n_calls_n_transcripts_n_events(tmp_path: Path) -> None:
    runner, _, log, _, udir = make(tmp_path, [FakeStep() for _ in range(4)])
    for i in range(4):
        call(runner, tmp_path, CallContext("R", task=f"T{i}", attempt=1))
    assert len(log.read().events) == 4
    assert len(list((udir / "transcripts").rglob("*.json"))) == 4


def test_ac2_requirement_level_call_without_task(tmp_path: Path) -> None:
    runner, _, log, _, udir = make(tmp_path, [FakeStep()])
    call(runner, tmp_path, CallContext("R"), role="spec")
    (e,) = log.read().events
    assert e.task is None and e.attempt is None
    assert (udir / e.data["transcript"]).is_file()


@pytest.mark.parametrize(
    "ran",
    ["claude-opus-5-5", "opus", "opus-5.5", "claude-sonnet-5-5", "sonnet", "haiku-5.5"],
)
def test_ac2_cost_matches_t13_for_ids_and_aliases(tmp_path: Path, ran: str) -> None:
    usage = Usage(input=M, output=M, cache_read=M, cache_write=M)
    runner, _, log, ledger, _ = make(tmp_path, [FakeStep(usage=usage, model=ran)])
    call(runner, tmp_path, ctx1())
    (e,) = log.read().events
    key = DEFAULT_PRICES.resolve(ran)
    assert key is not None
    p = DEFAULT_PRICES.prices[key]
    expected = p.input + p.output + p.cache_read + p.cache_write
    assert e.data["cost_usd"] == pytest.approx(float(expected))
    assert e.data["model"] == ran
    assert ledger.total("R") == expected


def test_ac2_hand_computed_opus_cost(tmp_path: Path) -> None:
    # 500k in (2.00) + 100k out (2.00) + 1M cache read (0.20) + 1M cache write (5.00) = 9.20
    usage = Usage(500_000, 100_000, M, M)
    runner, _, log, _, _ = make(tmp_path, [FakeStep(usage=usage)])
    call(runner, tmp_path, ctx1())
    assert log.read().events[0].data["cost_usd"] == pytest.approx(9.2)


def test_ac2_unknown_model_is_costed_not_free(tmp_path: Path) -> None:
    runner, _, log, ledger, _ = make(
        tmp_path, [FakeStep(usage=Usage(M, 0), model="mystery-9")]
    )
    call(runner, tmp_path, ctx1())
    assert log.read().events[0].data["cost_usd"] > 0
    assert "mystery-9" in ledger.flagged_models


def test_ac2_event_records_actual_not_requested_model_and_effort(
    tmp_path: Path,
) -> None:
    runner, _, log, _, udir = make(
        tmp_path, [FakeStep(model="claude-haiku-5-5", effort="low", usage=Usage(M, 0))]
    )
    call(runner, tmp_path, ctx1())  # requested opus/high
    (e,) = log.read().events
    assert (e.data["model"], e.data["effort"]) == ("claude-haiku-5-5", "low")
    assert e.data["cost_usd"] == pytest.approx(0.5)  # haiku, not opus (4.0)
    text = (udir / e.data["transcript"]).read_text(encoding="utf-8")
    assert "opus-5.5" in text and "claude-haiku-5-5" in text
    assert '"high"' in text and '"low"' in text


def test_ac2_adapter_reported_cost_is_not_trusted(tmp_path: Path) -> None:
    class Liar:
        def run(  # type: ignore[no-untyped-def]
            self, role, prompt, workdir, tools, schema, limits, *, on_activity=None
        ):
            return AgentResult(
                "x",
                None,
                Usage(M, 0),
                999.0,
                1,
                None,
                StopReason.DONE,
                "sonnet",
                "medium",
            )

    log = EventLog(tmp_path / "e.jsonl")
    r = LoggingRunner(
        Liar(),
        ultron_dir=tmp_path / ".ultron",
        log=log,
        ledger=UsageLedger(DEFAULT_PRICES),
        caps=CapLimits(Decimal(100), Decimal(100)),
    )
    call(r, tmp_path, ctx1())
    assert log.read().events[0].data["cost_usd"] == pytest.approx(2.0)


def test_ac2_activity_appended_to_jsonl(tmp_path: Path) -> None:
    acts = [ActivityEvent(k, f"t-{k.value}") for k in ActivityKind]
    runner, _, _, _, udir = make(
        tmp_path, [FakeStep(activity=acts), FakeStep(activity=acts[:1])]
    )
    call(runner, tmp_path, ctx1())
    call(runner, tmp_path, ctx1())
    lines = (udir / "activity.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in lines]
    assert len(rows) == len(acts) + 1
    assert [r["kind"] for r in rows[: len(acts)]] == [k.value for k in ActivityKind]
    assert all(r["requirement"] == "R" and r["role"] == "developer" for r in rows)


def test_ac2_live_callback_still_receives_activity(tmp_path: Path) -> None:
    acts = [ActivityEvent(ActivityKind.RUN, "pytest")]
    runner, _, _, _, _ = make(tmp_path, [FakeStep(activity=acts)])
    live: list[ActivityEvent] = []
    runner.run(ctx1(), "d", "p", tmp_path, [], None, LIMITS, on_activity=live.append)
    assert live == acts


def test_ac2_inner_exception_writes_nothing_and_charges_nothing(
    tmp_path: Path,
) -> None:
    runner, _, log, ledger, udir = make(
        tmp_path, [FakeStep(raises=RuntimeError("boom"))]
    )
    with pytest.raises(RuntimeError, match="boom"):
        call(runner, tmp_path, ctx1())
    assert log.read().events == []
    assert not list(udir.rglob("*.json")) if udir.exists() else True
    assert ledger.total("R") == 0


def test_ac2_failure_between_transcript_and_event_leaves_orphan(
    tmp_path: Path,
) -> None:
    """Developer-flagged risk: the transcript is written before the event, so a log failure orphans it."""
    runner, _, log, _, udir = make(tmp_path, [FakeStep(usage=Usage(M, 0))])

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    log.append = boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        call(runner, tmp_path, ctx1())
    # Documents current behaviour: one orphan transcript, no event.
    assert len(list((udir / "transcripts").rglob("*.json"))) == 1
    assert not (tmp_path / "events.jsonl").exists()


# ---- AC3: cap before the call ---------------------------------------------------------------------------------


def test_ac3_refused_call_no_transcript_no_event_no_run(tmp_path: Path) -> None:
    runner, fake, log, ledger, udir = make(tmp_path, [FakeStep()], req_cap="5")
    ledger.add(
        "R", UsageRecord("claude-sonnet-5-5", {"output": 500_000})
    )  # 5.00 == cap
    before = ledger.total("R")
    with pytest.raises(CapRefusedError) as ei:
        call(runner, tmp_path, ctx1())
    assert fake.calls == []
    assert log.read().events == []
    assert not udir.exists() or not any(udir.rglob("*"))
    assert ledger.total("R") == before
    assert ei.value.decision.allowed is False


def test_ac3_under_cap_allowed_then_over_cap_refused(tmp_path: Path) -> None:
    runner, fake, log, _, udir = make(
        tmp_path, [FakeStep(usage=Usage(0, M), model="sonnet"), FakeStep()], req_cap="5"
    )
    call(runner, tmp_path, ctx1())  # costs 10.00 against a 5.00 cap
    with pytest.raises(CapRefusedError):
        call(runner, tmp_path, ctx1())
    assert len(fake.calls) == 1
    assert len(log.read().events) == 1
    assert len(list((udir / "transcripts").rglob("*.json"))) == 1


def test_ac3_other_requirement_not_blocked(tmp_path: Path) -> None:
    runner, fake, _, ledger, _ = make(tmp_path, [FakeStep()], req_cap="1")
    ledger.add("OTHER", UsageRecord("claude-sonnet-5-5", {"output": M}))
    call(runner, tmp_path, ctx1())
    assert len(fake.calls) == 1


def test_ac3_evaluation_run_cap_refuses_before_run(tmp_path: Path) -> None:
    runner, fake, log, ledger, udir = make(
        tmp_path, [FakeStep()], req_cap="1000", run_cap="3"
    )
    ledger.add("SIB", UsageRecord("claude-sonnet-5-5", {"output": 500_000}), "RUN1")
    with pytest.raises(CapRefusedError) as ei:
        runner.run(
            CallContext("R", task="T", attempt=1, run="RUN1"),
            "developer",
            "p",
            tmp_path,
            [],
            None,
            LIMITS,
        )
    assert ei.value.decision.scope.kind == "evaluation_run"
    assert fake.calls == [] and log.read().events == []
    assert not udir.exists() or not any(udir.rglob("*"))


def test_ac3_run_cap_ignored_without_run_and_other_run_not_blocked(
    tmp_path: Path,
) -> None:
    runner, fake, _, ledger, _ = make(tmp_path, [FakeStep(), FakeStep()], run_cap="3")
    ledger.add("SIB", UsageRecord("claude-sonnet-5-5", {"output": 500_000}), "RUN1")
    call(runner, tmp_path, ctx1())
    runner.run(
        CallContext("R2", task="T", attempt=1, run="RUN2"),
        "developer",
        "p",
        tmp_path,
        [],
        None,
        LIMITS,
    )
    assert len(fake.calls) == 2


def test_ac3_calls_in_a_run_accumulate_to_run_total(tmp_path: Path) -> None:
    runner, _, _, ledger, _ = make(
        tmp_path, [FakeStep(usage=Usage(0, M), model="sonnet")]
    )
    runner.run(
        CallContext("R", task="T", attempt=1, run="RUN1"),
        "developer",
        "p",
        tmp_path,
        [],
        None,
        LIMITS,
    )
    assert ledger.run_total("RUN1") == Decimal(10)


# ---- AC4: JSON round trip ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ActivityKind))
def test_ac4_every_activity_kind_round_trips_through_json(kind: ActivityKind) -> None:
    ev = ActivityEvent(kind, 'text é "q"', {"n": 1, "l": [1, "a"], "m": {"x": None}})
    again = ActivityEvent.from_dict(json.loads(json.dumps(ev.to_dict())))
    assert again == ev and again.kind is kind


def test_ac4_activity_kind_set_is_the_specified_nine() -> None:
    assert {k.value for k in ActivityKind} == {
        "started",
        "finished",
        "read",
        "edit",
        "run",
        "ask",
        "answer",
        "thinking",
        "usage",
    }


def test_ac4_stop_reasons_cover_spec_set() -> None:
    names = {s.value for s in StopReason}
    assert {"done", "turn_limit", "budget", "usage_limit", "overload", "error"} <= names


@pytest.mark.parametrize("stop", list(StopReason))
def test_ac4_every_stop_reason_round_trips(stop: StopReason) -> None:
    res = AgentResult(
        output="o",
        data={"a": {"b": [1, 2, None]}},
        usage=Usage(1, 2, 3, 4),
        cost_usd=1.25,
        duration_ms=77,
        transcript_path="transcripts/R/T-a1-developer-1.json",
        stop_reason=stop,
        model="m",
        effort="e",
        activity=(
            ActivityEvent(ActivityKind.STARTED, "go"),
            ActivityEvent(ActivityKind.FINISHED, ""),
        ),
    )
    again = AgentResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert again == res and again.stop_reason is stop


def test_ac4_none_data_and_none_transcript_round_trip() -> None:
    res = AgentResult("", None, Usage(), 0.0, 0, None, StopReason.ERROR, "m", "e")
    again = AgentResult.from_dict(json.loads(json.dumps(res.to_dict())))
    assert again == res and again.data is None and again.transcript_path is None


def test_ac4_usage_round_trip_and_missing_kinds_default_zero() -> None:
    u = Usage(1, 2, 3, 4)
    assert Usage.from_dict(json.loads(json.dumps(u.as_tokens()))) == u
    assert Usage.from_dict({"input": 5}) == Usage(input=5)


# ---- AC5: interface imports only the standard library --------------------------------------------------------


def test_ac5_interface_imports_only_stdlib() -> None:
    tree = ast.parse(Path(interface.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in interface.py"
            roots.add((node.module or "").split(".")[0])
    assert roots, "no imports found"
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots


def _run_py(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_ac5_importing_interface_loads_no_other_ultron_module_or_adapter() -> None:
    code = (
        "import sys, ultron.agents.interface\n"
        "ok = ('ultron', 'ultron.agents', 'ultron.agents.interface')\n"
        "bad = sorted(m for m in sys.modules if (m.startswith('ultron.') and m not in ok)"
        " or m.startswith('claude_agent_sdk'))\n"
        "print(','.join(bad))\n"
    )
    assert _run_py(code) == ""


def test_ac5_fake_and_calllog_do_not_pull_in_an_adapter() -> None:
    code = (
        "import sys, ultron.agents.fake, ultron.agents.calllog\n"
        "print(any(m.startswith('claude_agent_sdk') for m in sys.modules))\n"
    )
    assert _run_py(code) == "False"


def test_run_limits_carry_requested_model_and_effort() -> None:
    d = LIMITS.to_dict()
    assert d["model"] == "opus-5.5" and d["effort"] == "high"
