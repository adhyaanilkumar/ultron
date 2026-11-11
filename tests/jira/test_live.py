"""Owner step (T25 AC6/AC7): one live create and delete in the Jira test project. Never run by agents or CI.

Run it only with `-m live`, with the `JIRA_*` variables set and `ULTRON_JIRA_TEST_CONFIG` pointing at an
`ultron.toml` whose `[jira]` table names the test project, its board id and an epic in it:

    uv run pytest -m live tests/jira/test_live.py

For the AC7 screenshot, add `-s` and set `ULTRON_JIRA_LIVE_PAUSE_SECONDS` (for example 180): the test prints the
issue URL and waits that long before deleting the issue.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ultron.config import load_config
from ultron.jira import AttemptRow, JiraClient, JiraTarget, TicketBody

pytestmark = pytest.mark.live


def _live_selected(config: pytest.Config) -> bool:
    expr = str(config.getoption("markexpr") or "")
    return "live" in expr and "not live" not in expr


def test_live_create_check_delete(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if not _live_selected(request.config):
        pytest.skip("live Jira test: owner step, run with -m live")

    config_path = os.environ.get("ULTRON_JIRA_TEST_CONFIG", "").strip()
    assert config_path, "set ULTRON_JIRA_TEST_CONFIG to the test project's ultron.toml"
    target = JiraTarget.from_config(load_config(config_path).config.jira)

    attachment = tmp_path / "attempt-1.log"
    attachment.write_text("ultron T25 live test: attempt 1 log\n", encoding="utf-8")
    body = TicketBody(
        requirement="R-00000000-live",
        task="T25",
        title="live client check (safe to delete)",
        reason="T25 AC6 live test; deleted by the test",
        acceptance_criteria=[
            "AC6: created and deleted",
            "AC7: body shows as formatted text",
        ],
        last_test_output="FAILED tests/example.py::test_x - AssertionError (sample)",
        attempts=[
            AttemptRow(1, "R2", "fail", "sample row"),
            AttemptRow(2, "R3", "fail", "sample"),
        ],
    )

    with JiraClient.from_env() as jira:
        result = jira.escalate(
            target,
            issue_type="Story",
            body=body,
            labels=["ultron-live-test"],
            attachments=[attachment],
        )
        try:
            print(
                f"\ncreated {result.key}: {result.url}; sprint: {result.sprint.describe()}"
            )
            issue = jira.get_issue(
                result.key,
                ["parent", "issuetype", "labels", "attachment", "description"],
            )
            fields = issue["fields"]
            assert fields["parent"]["key"] == target.epic
            assert fields["issuetype"]["name"] == result.issue_type == "Story"
            assert "ultron" in fields["labels"]
            assert [a["filename"] for a in fields["attachment"]] == ["attempt-1.log"]
            assert fields["description"]["type"] == "doc"

            pause = float(os.environ.get("ULTRON_JIRA_LIVE_PAUSE_SECONDS", "0") or 0)
            if pause > 0:
                print(
                    f"waiting {pause:.0f}s before deleting {result.key} (screenshot it now)"
                )
                time.sleep(pause)
        finally:
            jira.delete_issue(result.key)
            print(f"deleted {result.key}")
