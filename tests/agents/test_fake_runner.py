"""T15 AC1: the fake runner replays its script in order."""

from pathlib import Path

import pytest

from ultron.agents.fake import FakeAgentRunner, FakeStep, ScriptExhaustedError
from ultron.agents.interface import (
    ActivityEvent,
    ActivityKind,
    RunLimits,
    StopReason,
)

LIMITS = RunLimits(model="sonnet-5.5", effort="medium")


def call(runner: FakeAgentRunner, tmp_path: Path, on_activity=None):  # type: ignore[no-untyped-def]
    return runner.run(
        "developer", "do it", tmp_path, ["Read"], None, LIMITS, on_activity=on_activity
    )


def test_results_in_order_with_activity_and_diff(tmp_path: Path) -> None:
    act = ActivityEvent(ActivityKind.EDIT, "edit a.py (+1 -0)")
    runner = FakeAgentRunner(
        [
            FakeStep(output="one", activity=[act], diff={"src/a.py": "x = 1\n"}),
            FakeStep(output="two", data={"k": 1}),
        ]
    )
    seen: list[ActivityEvent] = []
    first = call(runner, tmp_path, seen.append)
    second = call(runner, tmp_path)
    assert (first.output, second.output) == ("one", "two")
    assert seen == [act]
    assert first.activity == (act,)
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert second.data == {"k": 1}
    assert [c.prompt for c in runner.calls] == ["do it", "do it"]


def test_model_and_effort_default_to_requested_and_can_differ(tmp_path: Path) -> None:
    runner = FakeAgentRunner([FakeStep(), FakeStep(effort="low")])
    assert call(runner, tmp_path).effort == "medium"
    lowered = call(runner, tmp_path)
    assert (lowered.model, lowered.effort) == ("sonnet-5.5", "low")


def test_failures_and_stops(tmp_path: Path) -> None:
    boom = RuntimeError("boom")
    runner = FakeAgentRunner(
        [
            FakeStep(stop_reason=StopReason.USAGE_LIMIT),
            FakeStep(stop_reason=StopReason.INTERRUPTED),
            FakeStep(raises=boom),
        ]
    )
    assert call(runner, tmp_path).stop_reason is StopReason.USAGE_LIMIT
    assert call(runner, tmp_path).stop_reason is StopReason.INTERRUPTED
    with pytest.raises(RuntimeError, match="boom"):
        call(runner, tmp_path)


def test_script_exhausted(tmp_path: Path) -> None:
    runner = FakeAgentRunner([])
    with pytest.raises(ScriptExhaustedError):
        call(runner, tmp_path)
