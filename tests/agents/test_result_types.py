"""T15 AC4: results and activity events are JSON-serialisable and round-trip."""

import json

import pytest

from ultron.agents.interface import (
    ActivityEvent,
    ActivityKind,
    AgentResult,
    StopReason,
    Usage,
)


@pytest.mark.parametrize("kind", list(ActivityKind))
def test_activity_round_trip(kind: ActivityKind) -> None:
    event = ActivityEvent(kind, "text", {"path": "a.py", "n": 2})
    again = ActivityEvent.from_dict(json.loads(json.dumps(event.to_dict())))
    assert again == event


@pytest.mark.parametrize("stop", list(StopReason))
def test_result_round_trip(stop: StopReason) -> None:
    result = AgentResult(
        output="out",
        data={"a": [1, 2]},
        usage=Usage(1, 2, 3, 4),
        cost_usd=0.5,
        duration_ms=7,
        transcript_path="transcripts/x.json",
        stop_reason=stop,
        model="claude-sonnet-5-5",
        effort="high",
        activity=(ActivityEvent(ActivityKind.RUN, "run pytest"),),
    )
    again = AgentResult.from_dict(json.loads(json.dumps(result.to_dict())))
    assert again == result


def test_result_without_data_round_trips() -> None:
    result = AgentResult("o", None, Usage(), 0.0, 0, None, StopReason.DONE, "m", "e")
    assert AgentResult.from_dict(json.loads(json.dumps(result.to_dict()))) == result
