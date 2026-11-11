"""Acceptance tests for T25 (Jira Cloud client), written by the testing agent from tasks.md and spec §12.

HTTP is mocked with respx; nothing here reaches the network or Jira. AC6/AC7 are owner steps (live create/delete and
a browser screenshot) and aren't covered here.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import re
import traceback
import types
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
import respx
from pydantic import ValidationError

import ultron.jira.client as client_module
from ultron.config import build_config
from ultron.config.models import Jira
from ultron.jira import (
    ENDPOINTS,
    AttemptRow,
    JiraClient,
    JiraConfigError,
    JiraCredentials,
    JiraError,
    JiraTarget,
    JiraUnreachable,
    TicketBody,
    ticket_summary,
)
from ultron.jira.client import match_endpoint

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "jira" / "fixtures"
BASE = "https://example.atlassian.net"
EMAIL = "acceptance@example.com"
TOKEN = "ACCEPT-SENTINEL-9c41e7b2d0"
BASIC = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
TARGET = JiraTarget(project="TEST", board=1, epic="TEST-100")

TYPES = "/rest/api/3/issue/createmeta/TEST/issuetypes"
CREATE = "/rest/api/3/issue"
ATTACH = "/rest/api/3/issue/TEST-1/attachments"
SPRINTS = "/rest/agile/1.0/board/1/sprint"
MOVE_ACTIVE = "/rest/agile/1.0/sprint/7/issue"
MOVE_FUTURE = "/rest/agile/1.0/sprint/8/issue"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def env(**over: str) -> dict[str, str]:
    values = {"JIRA_BASE_URL": BASE, "JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN}
    values.update(over)
    return values


def ticket(**over: Any) -> TicketBody:
    values: dict[str, Any] = {
        "requirement": "R3",
        "task": "T5",
        "title": "Add the export button",
        "reason": "all attempts failed (3 of 3)",
        "acceptance_criteria": ["AC1: exports CSV", "AC2: header row matches"],
        "last_test_output": "FAILED tests/test_export.py::test_csv - AssertionError",
        "attempts": [
            AttemptRow(1, "R2", "fail", "csv empty"),
            AttemptRow(2, "R3", "fail", "header wrong"),
            AttemptRow(3, "R3", "fail", "timeout"),
        ],
    }
    values.update(over)
    return TicketBody(**values)


def adf_text(node: Any) -> str:
    """Every text value in an ADF tree, joined."""
    if isinstance(node, dict):
        own = node.get("text", "") if node.get("type") == "text" else ""
        return own + " ".join(adf_text(c) for c in node.get("content", []))
    if isinstance(node, list):
        return " ".join(adf_text(c) for c in node)
    return ""


def adf_types(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [
            str(node.get("type")),
            *[t for c in node.get("content", []) for t in adf_types(c)],
        ]
    return []


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    # assert_all_mocked (the default) makes any request without a route fail the test.
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        yield r


@pytest.fixture
def jira() -> Iterator[JiraClient]:
    with JiraClient(JiraCredentials.from_env(env())) as c:
        yield c


def mock_types(router: respx.MockRouter, name: str = "issuetypes_ak.json") -> None:
    router.get(TYPES).mock(
        return_value=httpx.Response(200, json=fixture(name)["response"])
    )


def mock_sprints(router: respx.MockRouter, active: bool, future: bool) -> None:
    s = fixture("sprints.json")
    router.get(SPRINTS, params={"state": "active"}).mock(
        return_value=httpx.Response(200, json=s["active"] if active else s["empty"])
    )
    router.get(SPRINTS, params={"state": "future"}).mock(
        return_value=httpx.Response(200, json=s["future"] if future else s["empty"])
    )


def mock_happy(
    router: respx.MockRouter, *, active: bool = True, future: bool = True
) -> None:
    w = fixture("issue_write.json")
    mock_types(router)
    router.post(CREATE).mock(return_value=httpx.Response(201, json=w["create"]))
    router.post(ATTACH).mock(return_value=httpx.Response(200, json=w["attachments"]))
    mock_sprints(router, active, future)
    router.post(MOVE_ACTIVE).mock(return_value=httpx.Response(204))
    router.post(MOVE_FUTURE).mock(return_value=httpx.Response(204))


def create_payload(router: respx.MockRouter) -> dict[str, Any]:
    calls = [
        c
        for c in router.calls
        if c.request.method == "POST" and c.request.url.path == CREATE
    ]
    assert len(calls) == 1, f"expected exactly one create, got {len(calls)}"
    data: dict[str, Any] = json.loads(calls[0].request.content)
    return data


def log_file(tmp_path: Path, name: str = "attempt-1.log") -> Path:
    p = tmp_path / name
    p.write_text("attempt log\n", encoding="utf-8")
    return p


# ---- AC1: Feature falls back to Story (with a note) when the project has no Feature type ----


def test_ac1_feature_without_feature_type_files_story_with_note(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)  # issuetypes_ak.json: AK's real list, no Feature
    result = jira.escalate(TARGET, issue_type="Feature", body=ticket())
    fields = create_payload(router)["fields"]
    assert fields["issuetype"] == {"id": "10016"}  # AK's Story
    assert result.issue_type == "Story"
    assert result.requested_type == "Feature"
    assert result.fell_back is True
    first = adf_text(fields["description"]["content"][0])
    assert "Feature" in first and "Story" in first, (
        "the fallback note must come first and name both types"
    )


def test_ac1_feature_used_when_project_has_it(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)
    mock_types(router, "issuetypes_with_feature.json")
    result = jira.escalate(TARGET, issue_type="Feature", body=ticket())
    fields = create_payload(router)["fields"]
    assert fields["issuetype"] == {"id": "10099"}
    assert result.fell_back is False
    assert "Note" not in adf_text(fields["description"]["content"][0])


@pytest.mark.parametrize(("wanted", "type_id"), [("Story", "10016"), ("Bug", "10017")])
def test_ac1_story_and_bug_map_to_their_types(
    router: respx.MockRouter, jira: JiraClient, wanted: Any, type_id: str
) -> None:
    mock_happy(router)
    result = jira.escalate(TARGET, issue_type=wanted, body=ticket())
    assert create_payload(router)["fields"]["issuetype"] == {"id": type_id}
    assert result.fell_back is False


@pytest.mark.parametrize("missing", ["Story", "Bug"])
def test_ac1_missing_story_or_bug_is_a_clear_error_and_nothing_is_created(
    router: respx.MockRouter, jira: JiraClient, missing: str
) -> None:
    data = fixture("issuetypes_ak.json")["response"]
    data["issueTypes"] = [t for t in data["issueTypes"] if t["name"] != missing]
    router.get(TYPES).mock(return_value=httpx.Response(200, json=data))
    create = router.post(CREATE).mock(
        return_value=httpx.Response(201, json={"key": "TEST-1"})
    )
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type=missing, body=ticket())  # type: ignore[arg-type]
    assert missing in str(info.value) and "TEST" in str(info.value)
    assert info.value.issue_key is None
    assert not create.called


def test_ac1_feature_with_neither_feature_nor_story_errors(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    data = fixture("issuetypes_ak.json")["response"]
    data["issueTypes"] = [t for t in data["issueTypes"] if t["name"] != "Story"]
    router.get(TYPES).mock(return_value=httpx.Response(200, json=data))
    create = router.post(CREATE).mock(
        return_value=httpx.Response(201, json={"key": "TEST-1"})
    )
    with pytest.raises(JiraError, match="Feature"):
        jira.escalate(TARGET, issue_type="Feature", body=ticket())
    assert not create.called


# ---- AC2: active sprint, else first future sprint, else backlog; the result names which ----


@pytest.mark.parametrize(
    ("active", "future", "kind", "sprint_id", "name", "move_path"),
    [
        (True, True, "active", 7, "TEST Sprint 4", MOVE_ACTIVE),
        (False, True, "future", 8, "TEST Sprint 5", MOVE_FUTURE),
        (False, False, "backlog", None, None, None),
    ],
    ids=["active", "future", "backlog"],
)
def test_ac2_sprint_choice(
    router: respx.MockRouter,
    jira: JiraClient,
    active: bool,
    future: bool,
    kind: str,
    sprint_id: int | None,
    name: str | None,
    move_path: str | None,
) -> None:
    mock_happy(router, active=active, future=future)
    result = jira.escalate(TARGET, issue_type="Bug", body=ticket())
    assert result.sprint.kind == kind
    assert result.sprint.sprint_id == sprint_id
    assert result.sprint.sprint_name == name
    assert result.to_dict()["sprint"] == {"kind": kind, "id": sprint_id, "name": name}
    assert kind in result.sprint.describe()
    moves = [
        c.request
        for c in router.calls
        if "/rest/agile/1.0/sprint/" in c.request.url.path
    ]
    if move_path is None:
        assert moves == [], "backlog must not move the issue anywhere"
    else:
        assert [m.url.path for m in moves] == [move_path]
        assert moves[0].method == "POST"
        assert json.loads(moves[0].content) == {"issues": ["TEST-1"]}


def test_ac2_sprint_lookups_are_get_queries_on_the_configured_board(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router, active=False, future=False)
    jira.escalate(TARGET, issue_type="Story", body=ticket())
    lookups = [c.request for c in router.calls if c.request.url.path == SPRINTS]
    assert [r.method for r in lookups] == ["GET", "GET"]
    assert [r.url.params.get("state") for r in lookups] == ["active", "future"]


# ---- AC3: no sprint create, update, start, close or delete endpoint ----

SPRINT_MUTATIONS = [
    ("POST", "/rest/agile/1.0/sprint"),
    ("PUT", "/rest/agile/1.0/sprint/7"),
    ("POST", "/rest/agile/1.0/sprint/7"),
    ("PATCH", "/rest/agile/1.0/sprint/7"),
    ("DELETE", "/rest/agile/1.0/sprint/7"),
    ("POST", "/rest/agile/1.0/sprint/7/swap"),
    ("PUT", "/rest/agile/1.0/sprint/7/properties/x"),
    ("POST", "/rest/greenhopper/1.0/sprint/7/start"),
    ("PUT", "/rest/greenhopper/1.0/sprint/7/complete"),
]


@pytest.mark.parametrize(("method", "path"), SPRINT_MUTATIONS)
def test_ac3_sprint_mutations_are_not_allowed(method: str, path: str) -> None:
    assert match_endpoint(method, path) is None


def test_ac3_allowlist_sprint_endpoints_are_read_and_move_only() -> None:
    sprintish = {
        (e.method, e.template)
        for e in ENDPOINTS.values()
        if "sprint" in e.template.lower()
    }
    assert sprintish == {
        ("GET", "/rest/agile/1.0/board/{board_id}/sprint"),
        ("POST", "/rest/agile/1.0/sprint/{sprint_id}/issue"),
    }
    assert all(e.method in {"GET", "POST", "DELETE"} for e in ENDPOINTS.values())
    for e in ENDPOINTS.values():
        if e.method == "DELETE":
            assert "sprint" not in e.template.lower()


@pytest.mark.parametrize(
    ("active", "future"), [(True, True), (False, True), (False, False)]
)
def test_ac3_every_request_in_a_full_escalation_is_allowlisted(
    router: respx.MockRouter,
    jira: JiraClient,
    tmp_path: Path,
    active: bool,
    future: bool,
) -> None:
    mock_happy(router, active=active, future=future)
    jira.escalate(
        TARGET,
        issue_type="Feature",
        body=ticket(),
        attachments=[log_file(tmp_path), log_file(tmp_path, "plan.md")],
    )
    for call in router.calls:
        name = match_endpoint(call.request.method, call.request.url.path)
        assert name is not None, (
            f"{call.request.method} {call.request.url.path} isn't allowlisted"
        )
        assert (call.request.method, call.request.url.path) not in SPRINT_MUTATIONS


def test_ac3_unknown_endpoint_name_is_refused(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    with pytest.raises(JiraError):
        jira._request("sprint_create")
    assert not router.calls


def test_ac3_source_has_no_sprint_mutation_paths() -> None:
    src = Path(client_module.__file__).read_text(encoding="utf-8")
    assert "greenhopper" not in src.lower()
    assert '"PUT"' not in src and '"PATCH"' not in src
    rest_paths = set(re.findall(r"/rest/[A-Za-z0-9_./{}-]+", src))
    assert rest_paths <= {e.template for e in ENDPOINTS.values()}


def test_ac3_path_arguments_cannot_reach_other_endpoints(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    route = router.route().mock(
        return_value=httpx.Response(200, json={"issueTypes": [], "total": 0})
    )
    jira.issue_types("../../agile/1.0/sprint")
    assert route.called
    raw = route.calls.last.request.url.raw_path.decode()
    assert raw.startswith("/rest/api/3/issue/createmeta/")
    assert "/agile/" not in raw.split("?")[0].replace("%2F", "|")


# ---- AC4: credentials from the environment only, https, token never leaks ----


@pytest.mark.parametrize("name", ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_ac4_missing_variable_is_named(name: str, value: str | None) -> None:
    values = env()
    if value is None:
        del values[name]
    else:
        values[name] = value
    with pytest.raises(JiraConfigError, match=name):
        JiraCredentials.from_env(values)


def test_ac4_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    # A .env file or ultron.toml in the working folder must not be read.
    (tmp_path / ".env").write_text(
        f"JIRA_BASE_URL={BASE}\nJIRA_EMAIL={EMAIL}\nJIRA_API_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(JiraConfigError, match="JIRA_BASE_URL"):
        JiraClient.from_env()
    for k, v in env().items():
        monkeypatch.setenv(k, v)
    with JiraClient.from_env() as c:
        assert BASE in repr(c)


@pytest.mark.parametrize("key", ["api_token", "token", "email", "password"])
def test_ac4_config_file_cannot_hold_credentials(key: str) -> None:
    raw = {
        "commands": {"test": "make test"},
        "jira": {"project": "TEST", "board": "1", "epic": "TEST-100", key: "x"},
    }
    with pytest.raises(ValidationError):
        build_config(raw, None, windows=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.atlassian.net",
        "example.atlassian.net",
        "ftp://example.atlassian.net",
    ],
)
def test_ac4_https_is_enforced(url: str) -> None:
    with pytest.raises(JiraConfigError, match="https"):
        JiraCredentials.from_env(env(JIRA_BASE_URL=url))


def test_ac4_requests_use_https_basic_auth(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)
    jira.escalate(TARGET, issue_type="Story", body=ticket())
    for call in router.calls:
        assert call.request.url.scheme == "https"
        assert call.request.headers["Authorization"] == f"Basic {BASIC}"


def assert_clean(text: str, where: str) -> None:
    assert TOKEN not in text, f"token in {where}"
    assert BASIC not in text, f"Basic-auth token in {where}"


def exception_chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        todo += [e.__cause__, e.__context__]
    return seen


def assert_exception_clean(exc: BaseException) -> None:
    assert_clean(str(exc), "str(exception)")
    assert_clean(repr(exc), "repr(exception)")
    assert_clean(repr(exc.args), "exception args")
    assert_clean(repr(vars(exc)), "exception attributes")
    assert_clean("".join(traceback.format_exception(exc)), "formatted traceback")
    tb = traceback.TracebackException.from_exception(exc, capture_locals=True)
    assert_clean("".join(tb.format()), "traceback with locals")


def test_ac4_token_never_in_logs_reprs_or_results(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)  # root: ultron, httpx, httpcore, respx
    creds = JiraCredentials.from_env(env())
    assert_clean(repr(creds), "repr(credentials)")
    assert_clean(str(creds), "str(credentials)")
    assert_clean(repr(dataclasses.asdict(creds)), "asdict(credentials)")
    mock_happy(router)
    with JiraClient(creds) as c:
        assert_clean(repr(c), "repr(client)")
        result = c.escalate(
            TARGET,
            issue_type="Feature",
            body=ticket(),
            attachments=[log_file(tmp_path)],
        )
    assert_clean(repr(result), "repr(result)")
    assert_clean(json.dumps(result.to_dict()), "result.to_dict()")
    assert caplog.records, "expected some DEBUG/INFO log lines"
    assert_clean(caplog.text, "logs")
    assert_clean(repr([r.__dict__ for r in caplog.records]), "log records")


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_ac4_token_not_in_http_error(
    router: respx.MockRouter,
    jira: JiraClient,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(TYPES).mock(
        return_value=httpx.Response(
            status, json={"errorMessages": ["nope"], "errors": {}}
        )
    )
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=ticket())
    assert info.value.status == status
    assert "nope" in str(info.value)
    assert_exception_clean(info.value)
    assert_clean(caplog.text, "logs")


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_ac4_token_not_in_unreachable_error(
    router: respx.MockRouter,
    jira: JiraClient,
    caplog: pytest.LogCaptureFixture,
    error: type,
) -> None:
    caplog.set_level(logging.DEBUG)
    router.get(TYPES).mock(side_effect=error)
    with pytest.raises(JiraUnreachable) as info:
        jira.escalate(TARGET, issue_type="Story", body=ticket())
    assert info.value.path == TYPES
    assert_exception_clean(info.value)
    assert_clean(caplog.text, "logs")


def test_ac4_unreachable_error_does_not_keep_the_request_with_auth(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    # The client's own comment says the original httpx error is dropped because its request holds the auth header.
    # `raise ... from None` only hides it from tracebacks; it stays reachable as __context__.
    router.get(TYPES).mock(side_effect=httpx.ConnectError)
    with pytest.raises(JiraUnreachable) as info:
        jira.escalate(TARGET, issue_type="Story", body=ticket())
    for e in exception_chain(info.value):
        request = getattr(e, "_request", None)
        if request is not None:
            assert_clean(
                repr(dict(request.headers)),
                f"headers of {type(e).__name__} in the chain",
            )


# ---- AC5: attachments carry X-Atlassian-Token: no-check ----


def test_ac5_attachment_header_and_multipart(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_happy(router)
    files = [log_file(tmp_path, "attempt-1.log"), log_file(tmp_path, "plan.md")]
    result = jira.escalate(TARGET, issue_type="Bug", body=ticket(), attachments=files)
    uploads = [c.request for c in router.calls if c.request.url.path == ATTACH]
    assert len(uploads) == 2
    for req, f in zip(uploads, files, strict=True):
        assert req.method == "POST"
        assert req.headers["X-Atlassian-Token"] == "no-check"
        assert req.headers["Content-Type"].startswith("multipart/form-data")
        assert b'name="file"' in req.content
        assert f'filename="{f.name}"'.encode() in req.content
    assert len(result.attachments) == 2


# ---- Partial failure after the issue exists carries its key ----


@pytest.mark.parametrize("step", ["attach", "sprints", "move"])
def test_partial_failure_after_create_carries_issue_key(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, step: str
) -> None:
    mock_happy(router)
    if step == "attach":
        router.post(ATTACH).mock(
            return_value=httpx.Response(500, json={"errorMessages": ["x"]})
        )
    elif step == "sprints":
        router.get(SPRINTS, params={"state": "active"}).mock(
            side_effect=httpx.ConnectError
        )
    else:
        router.post(MOVE_ACTIVE).mock(
            return_value=httpx.Response(400, json={"errorMessages": ["x"]})
        )
    with pytest.raises(JiraError) as info:
        jira.escalate(
            TARGET, issue_type="Bug", body=ticket(), attachments=[log_file(tmp_path)]
        )
    assert info.value.issue_key == "TEST-1"
    assert len([c for c in router.calls if c.request.url.path == CREATE]) == 1


def test_partial_failure_missing_attachment_file_carries_issue_key(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    # An attempt log that disappeared after the issue was created: the caller must still learn the key,
    # or a retry files the ticket twice.
    mock_happy(router)
    with pytest.raises(Exception) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[tmp_path / "missing.log"],
        )
    assert getattr(info.value, "issue_key", None) == "TEST-1", (
        f"{type(info.value).__name__} raised after TEST-1 was created doesn't carry the key"
    )


def test_failure_before_create_has_no_issue_key(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_types(router)
    router.post(CREATE).mock(
        return_value=httpx.Response(400, json={"errors": {"parent": "bad"}})
    )
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Bug", body=ticket())
    assert info.value.issue_key is None
    assert "parent" in str(info.value)


# ---- §12 title, body, epic and labels ----


def test_s12_title_format() -> None:
    assert (
        ticket_summary("R3", "T5", "Add the export button")
        == "[ultron] R3/T5 Add the export button"
    )
    one_line = ticket_summary("R3", "T5", "line one\nline two")
    assert "\n" not in one_line and one_line.startswith("[ultron] R3/T5 ")
    assert len(ticket_summary("R3", "T5", "x" * 400)) <= 255


def test_s12_create_request_fields(router: respx.MockRouter, jira: JiraClient) -> None:
    mock_happy(router)
    result = jira.escalate(
        TARGET, issue_type="Bug", body=ticket(), labels=["escalated", "R3"]
    )
    fields = create_payload(router)["fields"]
    assert fields["summary"] == "[ultron] R3/T5 Add the export button"
    assert fields["project"] == {"key": "TEST"}
    assert fields["parent"] == {"key": "TEST-100"}
    assert "ultron" in fields["labels"]
    assert {"escalated", "R3"} <= set(fields["labels"])
    assert all(" " not in label for label in fields["labels"])
    assert result.key == "TEST-1"
    assert result.url == f"{BASE}/browse/TEST-1"


@pytest.mark.parametrize(
    "reason",
    [
        "all attempts failed (3 of 3)",
        "rejected by the tester: wrong file format",
        "spending cap reached",
        "stopped: owner stop",
    ],
)
def test_s12_description_content(
    router: respx.MockRouter, jira: JiraClient, reason: str
) -> None:
    mock_happy(router)
    jira.escalate(TARGET, issue_type="Bug", body=ticket(reason=reason))
    doc = create_payload(router)["fields"]["description"]
    assert doc["type"] == "doc" and doc["version"] == 1
    text = adf_text(doc)
    for needed in [
        "R3",
        "T5",
        "Add the export button",
        reason,
        "AC1: exports CSV",
        "AC2: header row matches",
        "FAILED tests/test_export.py::test_csv",
        "csv empty",
        "header wrong",
    ]:
        assert needed in text, f"{needed!r} missing from the description"
    types = adf_types(doc)
    assert {"heading", "bulletList", "table", "codeBlock"} <= set(types)
    code = [n for n in doc["content"] if n["type"] == "codeBlock"]
    assert "FAILED tests/test_export.py::test_csv" in adf_text(code)
    table = next(n for n in doc["content"] if n["type"] == "table")
    assert len(table["content"]) == 1 + 3  # header and one row per attempt


def test_s12_description_is_not_markdown_or_plain_string(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)
    jira.escalate(TARGET, issue_type="Bug", body=ticket())
    doc = create_payload(router)["fields"]["description"]
    assert isinstance(doc, dict)
    text = adf_text(doc)
    assert "## " not in text and "| ---" not in text


def test_s12_target_from_config() -> None:
    t = JiraTarget.from_config(Jira(project="TEST", board="12", epic="TEST-100"))
    assert t == JiraTarget("TEST", 12, "TEST-100")
    for bad in (
        Jira(project="TEST", board="abc", epic="TEST-100"),
        Jira(project="TEST", board="1"),
    ):
        with pytest.raises(JiraConfigError):
            JiraTarget.from_config(bad)


# ---- AC8 (agent-checkable part): fixtures carry a capture header and no AD or personal data ----

FIXTURE_FILES = sorted(FIXTURES.glob("*.json"))


def test_ac8_fixtures_exist() -> None:
    assert {p.name for p in FIXTURE_FILES} >= {"issuetypes_ak.json", "sprints.json"}


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_ac8_fixture_capture_header(path: Path) -> None:
    capture = json.loads(path.read_text(encoding="utf-8"))["_capture"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", capture["date"])
    assert capture["source"]


def test_ac8_issue_types_match_ak_live_capture() -> None:
    # Cross-checked by the testing agent on 2026-11-11 with Rovo (read only) on project AK.
    data = fixture("issuetypes_ak.json")
    assert data["_capture"]["source"].startswith("live")
    got = {
        (t["id"], t["name"], t["hierarchyLevel"])
        for t in data["response"]["issueTypes"]
    }
    assert got == {
        ("10014", "Subtask", -1),
        ("10015", "Epic", 1),
        ("10016", "Story", 0),
        ("10017", "Bug", 0),
        ("10018", "Task", 0),
    }


AD_KEY = re.compile(r"\bAD-\d+\b|[\"'/=]AD[\"'/]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SITE_RE = re.compile(r"https?://([a-z0-9-]+)\.atlassian\.net")


def jira_test_and_source_files() -> list[Path]:
    files = [*FIXTURE_FILES, *(ROOT / "tests" / "jira").glob("*.py")]
    files += (ROOT / "src" / "ultron" / "jira").glob("*.py")
    return files


@pytest.mark.parametrize("path", jira_test_and_source_files(), ids=lambda p: p.name)
def test_ac8_no_ad_or_personal_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not AD_KEY.search(text), f"project AD referenced in {path.name}"
    assert set(SITE_RE.findall(text)) <= {"example", "your-site"}, (
        f"real Jira site in {path.name}"
    )
    assert all(e.endswith("@example.com") for e in EMAIL_RE.findall(text)), (
        f"email in {path.name}"
    )
    assert (
        'accountId":' not in text
        and "emailAddress" not in text
        and "displayName" not in text
    )


# ==== Attempt 2 (R4): the whole exception graph, every httpx error class at every step, DEBUG logs, kanban ====
#
# These use httpx.MockTransport, not respx: respx re-raises a side effect's exception with its own SideEffectError
# (holding every recorded request, auth header included) as __cause__, which would be a leak of the test harness,
# not of ultron. MockTransport raises the handler's exception unchanged, as httpx's real transport does.

ULTRON_PKG = Path(client_module.__file__).resolve().parents[1]
STEPS = ("types", "create", "attach", "sprint_active", "sprint_future", "move")
AFTER_CREATE = frozenset({"attach", "sprint_active", "sprint_future", "move"})


def _in_ultron(frame: types.FrameType) -> bool:
    try:
        return Path(frame.f_code.co_filename).resolve().is_relative_to(ULTRON_PKG)
    except (OSError, ValueError):
        return False


def reachable_text(root: object) -> list[str]:
    """Every str or bytes value reachable from `root`.

    Followed: exception `__cause__`, `__context__`, `args`, `__notes__` and attributes; the locals of every traceback
    frame in ultron's code; containers and mappings (httpx.Headers included); object `__dict__` and `__slots__`;
    bound methods' `__self__` and closure cells. A JiraClient owns the credentials by design (its httpx client must
    send them), so it is checked through its repr and str only; any other object holding the token is a leak.
    """
    out: list[str] = []
    seen: set[int] = set()
    todo: list[object] = [root]
    while todo:
        obj = todo.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, str):
            out.append(obj)
            continue
        if isinstance(obj, bytes | bytearray | memoryview):
            out.append(bytes(obj).decode("latin-1"))
            continue
        if isinstance(
            obj,
            bool
            | int
            | float
            | complex
            | type
            | types.ModuleType
            | types.BuiltinFunctionType
            | types.CodeType,
        ):
            continue
        if isinstance(obj, JiraClient):
            out += [repr(obj), str(obj)]
            continue
        if isinstance(obj, types.MethodType):
            todo.append(obj.__self__)
            continue
        if isinstance(obj, types.FunctionType):
            for cell in obj.__closure__ or ():
                try:
                    todo.append(cell.cell_contents)
                except ValueError:
                    pass
            continue
        if isinstance(obj, types.TracebackType):
            tb: types.TracebackType | None = obj
            while tb is not None:
                todo.append(tb.tb_frame)
                tb = tb.tb_next
            continue
        if isinstance(obj, types.FrameType):
            if _in_ultron(obj):
                todo.append(dict(obj.f_locals))
            continue
        if isinstance(obj, BaseException):
            out += [str(obj), repr(obj)]
            todo += [obj.__cause__, obj.__context__, obj.args, obj.__traceback__]
            todo.append(getattr(obj, "__notes__", None))
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                todo += [k, v]
        elif isinstance(obj, list | tuple | set | frozenset):
            todo.extend(obj)
        try:
            todo.append(dict(vars(obj)))
        except TypeError:
            pass
        for cls in type(obj).__mro__:
            slots = getattr(cls, "__slots__", ())
            for slot in (slots,) if isinstance(slots, str) else slots:
                try:
                    todo.append(getattr(obj, slot))
                except AttributeError:
                    pass
    return out


def assert_graph_clean(root: object, where: str = "exception graph") -> None:
    assert_clean("\n".join(reachable_text(root)), where)


def assert_error_clean(exc: BaseException) -> None:
    assert_graph_clean(exc)
    assert_exception_clean(exc)


def assert_logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    assert_clean(caplog.text, "logs")
    for record in caplog.records:
        assert_clean(record.getMessage(), f"log message from {record.name}")
        assert_graph_clean(record.__dict__, f"log record from {record.name}")


def test_a2_walker_finds_the_token_where_it_would_leak(jira: JiraClient) -> None:
    # The walker itself must catch the leaks it is meant to catch, or the tests below prove nothing.
    request = httpx.Request("GET", BASE, headers={"Authorization": f"Basic {BASIC}"})
    with pytest.raises(AssertionError):
        assert_graph_clean(httpx.ConnectError("x", request=request))
    try:
        try:
            raise httpx.ConnectError("x", request=request)
        except httpx.ConnectError:
            raise JiraUnreachable("hidden") from None
    except JiraUnreachable as hidden:
        with pytest.raises(AssertionError):
            assert_graph_clean(hidden)  # through __context__, as in attempt 1
    with pytest.raises(JiraError) as in_frame:
        jira._request("not-an-endpoint", headers={"Authorization": f"Basic {BASIC}"})
    with pytest.raises(AssertionError):
        assert_graph_clean(
            in_frame.value
        )  # through the locals of ultron's _request frame
    with pytest.raises(AssertionError):
        assert_graph_clean(
            ValueError(JiraCredentials.from_env(env()))
        )  # _Secret's slot
    assert_graph_clean(ValueError(jira))  # the client itself is opaque: repr only


def _step_of(request: httpx.Request) -> str | None:
    method, path = request.method, request.url.path
    if (method, path) == ("GET", TYPES):
        return "types"
    if (method, path) == ("POST", CREATE):
        return "create"
    if (method, path) == ("POST", ATTACH):
        return "attach"
    if (method, path) == ("GET", SPRINTS):
        return f"sprint_{request.url.params.get('state')}"
    if method == "POST" and re.fullmatch(r"/rest/agile/1\.0/sprint/\d+/issue", path):
        return "move"
    return None


class FakeJira:
    """Jira at the transport level: canned fixture replies, and one step that fails with an error or a reply."""

    def __init__(
        self,
        *,
        fail: str | None = None,
        error: Callable[[httpx.Request], BaseException] | None = None,
        reply: Callable[[httpx.Request], httpx.Response] | None = None,
        sprint: str = "active",
    ) -> None:
        self.fail, self.error, self.reply = fail, error, reply
        self.sprint = "future" if fail == "sprint_future" else sprint
        self.steps: list[str] = []
        self.unexpected: list[str] = []
        self.unauthenticated: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        step = _step_of(request)
        if step is None:
            self.unexpected.append(f"{request.method} {request.url.path}")
            return httpx.Response(418)
        self.steps.append(step)
        if request.headers.get("Authorization") != f"Basic {BASIC}":
            self.unauthenticated.append(step)
        if step == self.fail:
            if self.error is not None:
                raise self.error(request)
            if self.reply is not None:
                return self.reply(request)
        w, s = fixture("issue_write.json"), fixture("sprints.json")
        if step == "types":
            return httpx.Response(200, json=fixture("issuetypes_ak.json")["response"])
        if step == "create":
            return httpx.Response(201, json=w["create"])
        if step == "attach":
            return httpx.Response(200, json=w["attachments"])
        if step == "sprint_active":
            return httpx.Response(
                200, json=s["active" if self.sprint == "active" else "empty"]
            )
        if step == "sprint_future":
            return httpx.Response(
                200, json=s["future" if self.sprint == "future" else "empty"]
            )
        return httpx.Response(204)

    def check(self) -> None:
        assert self.unexpected == [], f"requests outside the fake: {self.unexpected}"
        assert self.unauthenticated == [], "every request must carry Basic auth"


def escalate_with(fake: FakeJira, tmp_path: Path) -> Any:
    with JiraClient(
        JiraCredentials.from_env(env()), transport=httpx.MockTransport(fake)
    ) as c:
        return c.escalate(
            TARGET, issue_type="Bug", body=ticket(), attachments=[log_file(tmp_path)]
        )


def _all_subclasses(cls: type) -> set[type]:
    out = {cls}
    for sub in cls.__subclasses__():
        out |= _all_subclasses(sub)
    return out


HTTPX_ERRORS = sorted(
    (c for c in _all_subclasses(httpx.HTTPError) if c.__module__.startswith("httpx")),
    key=lambda c: c.__name__,
)


def test_a2_the_matrix_covers_every_httpx_error_class() -> None:
    names = {c.__name__ for c in HTTPX_ERRORS}
    assert names >= {
        "HTTPError", "RequestError", "TransportError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
        "WriteTimeout", "PoolTimeout", "NetworkError", "ConnectError", "ReadError", "WriteError", "CloseError",
        "ProtocolError", "LocalProtocolError", "RemoteProtocolError", "ProxyError", "UnsupportedProtocol",
        "DecodingError", "TooManyRedirects", "HTTPStatusError",
    }  # fmt: skip


def hostile_error(
    cls: type[httpx.HTTPError], request: httpx.Request
) -> httpx.HTTPError:
    """The worst case: the error holds the authenticated request and its message quotes the header and token."""
    auth = request.headers["Authorization"]
    message = f"{cls.__name__} sending Authorization: {auth} (token {TOKEN})"
    if issubclass(cls, httpx.HTTPStatusError):
        response = httpx.Response(
            502, headers={"WWW-Authenticate": auth}, request=request
        )
        return cls(message, request=request, response=response)
    if issubclass(cls, httpx.RequestError):
        return cls(message, request=request)
    exc = cls(message)
    exc.request = request
    return exc


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("error", HTTPX_ERRORS, ids=lambda c: c.__name__)
def test_a2_every_httpx_error_at_every_step_reaches_no_token(
    step: str,
    error: type[httpx.HTTPError],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=step, error=lambda r: hostile_error(error, r))
    with pytest.raises(JiraError) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert fake.steps[-1] == step, "the escalation must stop at the failing step"
    assert isinstance(exc, JiraUnreachable) is issubclass(error, httpx.TransportError)
    assert error.__name__ in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert exception_chain(exc) == [exc]
    assert_error_clean(exc)
    assert_logs_clean(caplog)
    assert fake.steps.count("create") == (0 if step == "types" else 1)
    assert exc.issue_key == ("TEST-1" if step in AFTER_CREATE else None)


OTHER_HTTPX_ERRORS: list[Callable[[], Exception]] = [
    lambda: httpx.InvalidURL("Invalid non-printable ASCII character in URL"),
    lambda: httpx.CookieConflict("Multiple cookies exist with name=session"),
    lambda: httpx.StreamError("stream error"),
    httpx.StreamConsumed,
    httpx.StreamClosed,
    httpx.ResponseNotRead,
    httpx.RequestNotRead,
]
OTHER_IDS = ["InvalidURL", "CookieConflict", "StreamError", "StreamConsumed", "StreamClosed",
             "ResponseNotRead", "RequestNotRead"]  # fmt: skip


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("make", OTHER_HTTPX_ERRORS, ids=OTHER_IDS)
def test_a2_httpx_errors_outside_httperror_at_every_step(
    step: str,
    make: Callable[[], Exception],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=step, error=lambda r: make())
    with pytest.raises(Exception) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert_error_clean(exc)
    assert_logs_clean(caplog)
    if step in AFTER_CREATE:
        assert isinstance(exc, JiraError) and not isinstance(exc, JiraUnreachable)
        assert exc.issue_key == "TEST-1"
        assert exc.__cause__ is None and exc.__context__ is None
        assert fake.steps.count("create") == 1
    else:
        assert getattr(exc, "issue_key", None) is None


ERROR_STATUSES = [400, 401, 403, 404, 409, 413, 429, 500, 502, 503, 504]


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("status", ERROR_STATUSES)
def test_a2_error_status_at_every_step(
    step: str, status: int, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def reply(request: httpx.Request) -> httpx.Response:
        # A reply that echoes the auth header back: ultron must not keep the response or its headers.
        return httpx.Response(
            status,
            headers={"WWW-Authenticate": request.headers["Authorization"]},
            json={"errorMessages": [f"jira says {status}"], "errors": {}},
        )

    fake = FakeJira(fail=step, reply=reply)
    if status == 400 and step in {"sprint_active", "sprint_future"}:
        # A board without sprints (kanban): spec §12 says no active and no planned sprint means the backlog.
        result = escalate_with(fake, tmp_path)
        fake.check()
        assert result.sprint.kind == "backlog"
        assert result.sprint.sprint_id is None
        assert "jira says 400" in (result.sprint.reason or "")
        assert fake.steps[-1] == step and "move" not in fake.steps
        assert result.to_dict()["sprint"]["kind"] == "backlog"
        assert_clean(json.dumps(result.to_dict()), "result.to_dict()")
        assert_clean(result.sprint.describe(), "describe()")
        assert_graph_clean(result, "result")
        assert_logs_clean(caplog)
        return
    with pytest.raises(JiraError) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert exc.status == status
    assert not isinstance(exc, JiraUnreachable)
    assert f"jira says {status}" in str(exc)
    assert fake.steps[-1] == step
    assert exc.issue_key == ("TEST-1" if step in AFTER_CREATE else None)
    assert fake.steps.count("create") == (0 if step == "types" else 1)
    assert exc.__cause__ is None and exc.__context__ is None
    assert_error_clean(exc)
    assert_logs_clean(caplog)


# ---- Kanban board (400 on the sprint lookup) with the fixture's own body ----


def test_a2_kanban_board_goes_to_backlog_with_reason(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_happy(router)
    no_sprints = fixture("sprints.json")["no_sprints"]
    lookup = router.get(SPRINTS, params={"state": "active"}).mock(
        return_value=httpx.Response(no_sprints["status"], json=no_sprints["body"])
    )
    future = router.get(SPRINTS, params={"state": "future"})
    result = jira.escalate(
        TARGET, issue_type="Bug", body=ticket(), attachments=[log_file(tmp_path)]
    )
    assert lookup.call_count == 1 and not future.called
    assert result.sprint.kind == "backlog"
    assert "does not support sprints" in (result.sprint.reason or "")
    assert "backlog" in result.sprint.describe()
    assert "does not support sprints" in result.sprint.describe()
    sprint = result.to_dict()["sprint"]
    assert sprint["kind"] == "backlog" and sprint["id"] is None
    assert "does not support sprints" in sprint["reason"]
    assert not [
        c for c in router.calls if "/rest/agile/1.0/sprint/" in c.request.url.path
    ]
    assert len(result.attachments) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502, 503])
@pytest.mark.parametrize("state", ["active", "future"])
def test_a2_other_sprint_lookup_failures_still_fail_with_key(
    router: respx.MockRouter, jira: JiraClient, status: int, state: str
) -> None:
    mock_happy(router, active=False, future=True)
    router.get(SPRINTS, params={"state": state}).mock(
        return_value=httpx.Response(status, json={"errorMessages": ["nope"]})
    )
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Bug", body=ticket())
    assert info.value.status == status
    assert info.value.issue_key == "TEST-1"
    assert not isinstance(info.value, JiraUnreachable)
    assert not [
        c for c in router.calls if "/rest/agile/1.0/sprint/" in c.request.url.path
    ]


# ---- Re-checks of the two attempt-1 failures ----


def test_a2_recheck_attempt1_connect_error_has_no_context(tmp_path: Path) -> None:
    fake = FakeJira(fail="types", error=lambda r: hostile_error(httpx.ConnectError, r))
    with pytest.raises(JiraUnreachable) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    assert exc.__context__ is None and exc.__cause__ is None
    assert not [
        a for a in vars(exc).values() if isinstance(a, httpx.Request | httpx.Response)
    ]
    assert_error_clean(exc)


def test_a2_recheck_attempt1_missing_attachment(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_happy(router)
    with pytest.raises(JiraError) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[tmp_path / "missing.log"],
        )
    exc = info.value
    assert exc.issue_key == "TEST-1"
    assert not isinstance(exc, JiraUnreachable), (
        "a missing local file isn't worth a retry"
    )
    assert "missing.log" in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert len([c for c in router.calls if c.request.url.path == CREATE]) == 1
    assert not [c for c in router.calls if c.request.url.path == ATTACH]
    assert_error_clean(exc)


def test_a2_missing_attachment_never_leaves_an_unknown_issue(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    # The invariant behind the attempt-1 test: either nothing is created, or the caller learns the key. Refusing
    # before create (no POST at all) and create-then-raise-with-key both satisfy it.
    mock_happy(router)
    good = log_file(tmp_path)
    with pytest.raises(JiraError) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[good, tmp_path / "missing.log"],
        )
    creates = len([c for c in router.calls if c.request.url.path == CREATE])
    assert (creates, info.value.issue_key) in {(0, None), (1, "TEST-1")}
    assert not isinstance(info.value, JiraUnreachable)


# ---- DEBUG logs through the real httpx -> httpcore -> h11 path (httpcore's MockBackend replaces the socket) ----


class _RecordingStream(httpcore.MockStream):
    def __init__(self, buffer: list[bytes], sink: bytearray) -> None:
        super().__init__(buffer)
        self._sink = sink

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sink += buffer


class _RecordingBackend(httpcore.MockBackend):
    """Replays canned bytes on one keep-alive connection and records every byte httpcore writes."""

    def __init__(self, buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self.replies = list(buffer)
        self.written = bytearray()

    def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        return _RecordingStream(list(self.replies), self.written)


def wire(status: int, body: Any = None) -> bytes:
    data = b"" if body is None else json.dumps(body).encode()
    head = f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n"
    return head.encode() + data


def real_path_client(backend: _RecordingBackend) -> JiraClient:
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=backend)
    return JiraClient(JiraCredentials.from_env(env()), transport=transport)


def _wire_script(case: str) -> list[bytes]:
    w, s = fixture("issue_write.json"), fixture("sprints.json")
    start = [
        wire(200, fixture("issuetypes_ak.json")["response"]),
        wire(201, w["create"]),
    ]
    scripts = {
        "happy": [
            *start,
            wire(200, w["attachments"]),
            wire(200, s["active"]),
            wire(204),
        ],
        "kanban": [
            *start,
            wire(200, w["attachments"]),
            wire(400, s["no_sprints"]["body"]),
        ],
        "status_401": [wire(401, {"errorMessages": ["Unauthorized"]})],
        "disconnect_before_create": [],
        "disconnect_after_create": start,
        "attach_500": [*start, wire(500, {"errorMessages": ["boom"]})],
    }
    return scripts[case]


REAL_PATH_EXPECTED: dict[str, tuple[type[JiraError], str | None]] = {
    "status_401": (JiraError, None),
    "disconnect_before_create": (JiraUnreachable, None),
    "disconnect_after_create": (JiraUnreachable, "TEST-1"),
    "attach_500": (JiraError, "TEST-1"),
}


@pytest.mark.parametrize("case", ["happy", "kanban", *REAL_PATH_EXPECTED])
def test_a2_debug_logs_through_real_httpcore(
    case: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "ultron"):
        caplog.set_level(logging.DEBUG, logger=name)
    backend = _RecordingBackend(_wire_script(case))
    outcome: Any
    with real_path_client(backend) as c:
        try:
            outcome = c.escalate(
                TARGET,
                issue_type="Bug",
                body=ticket(),
                attachments=[log_file(tmp_path)],
            )
        except JiraError as exc:
            outcome = exc
    # The token really went through httpcore while its loggers were at DEBUG, so a clean log means something.
    assert f"Basic {BASIC}".encode() in backend.written
    assert any(r.name.startswith("httpcore") for r in caplog.records)
    if backend.replies:  # httpx logs a request line only once a response arrives
        assert any(r.name.startswith("httpx") for r in caplog.records)
    assert_logs_clean(caplog)
    if case in {"happy", "kanban"}:
        assert not isinstance(outcome, BaseException), f"{case} failed: {outcome!r}"
        assert outcome.sprint.kind == ("active" if case == "happy" else "backlog")
        assert_graph_clean(outcome, "result")
        return
    kind, key = REAL_PATH_EXPECTED[case]
    assert isinstance(outcome, kind), f"{case}: got {type(outcome).__name__}"
    assert outcome.issue_key == key
    assert outcome.__cause__ is None and outcome.__context__ is None
    assert_error_clean(outcome)


def _httpx_objects_in_ultron_frames(exc: BaseException) -> list[str]:
    found: list[str] = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if _in_ultron(frame):
            for name, value in frame.f_locals.items():
                if isinstance(value, httpx.Request | httpx.Response | httpx.HTTPError):
                    found.append(
                        f"{frame.f_code.co_name}.{name}: {type(value).__name__}"
                    )
        tb = tb.tb_next
    return found


@pytest.mark.parametrize("status", [400, 401, 500])
@pytest.mark.parametrize("step", ["types", "create", "attach", "move"])
def test_a2_error_traceback_frames_hold_no_httpx_request_or_response(
    step: str, status: int, tmp_path: Path
) -> None:
    # httpx.Response.request carries the Authorization header; a frame of ultron's that still holds it keeps the
    # token reachable from the raised error (exc.__traceback__ -> frame -> f_locals), as __context__ did in attempt 1.
    fake = FakeJira(
        fail=step, reply=lambda r: httpx.Response(status, json={"errorMessages": ["x"]})
    )
    with pytest.raises(JiraError) as info:
        escalate_with(fake, tmp_path)
    assert _httpx_objects_in_ultron_frames(info.value) == []
