"""The Jira Cloud client, with HTTP mocked by respx. No test here reaches the network."""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import ultron.jira.client as client_module
from ultron.config.models import Jira
from ultron.jira.body import AttemptRow, TicketBody
from ultron.jira.client import (
    ENDPOINTS,
    ENV_VARS,
    EscalationResult,
    JiraClient,
    JiraConfigError,
    JiraCredentials,
    JiraError,
    JiraTarget,
    JiraUnreachable,
    match_endpoint,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://example.atlassian.net"
EMAIL = "bot@example.com"
SENTINEL = "SENTINEL-TOKEN-6f1d2c9e"
BASIC = base64.b64encode(f"{EMAIL}:{SENTINEL}".encode()).decode()
TARGET = JiraTarget(project="TEST", board=1, epic="TEST-100")

ISSUETYPES = "/rest/api/3/issue/createmeta/TEST/issuetypes"
CREATE = "/rest/api/3/issue"
ATTACH = "/rest/api/3/issue/TEST-1/attachments"
SPRINTS = "/rest/agile/1.0/board/1/sprint"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def env(**over: str) -> dict[str, str]:
    values = {"JIRA_BASE_URL": BASE, "JIRA_EMAIL": EMAIL, "JIRA_API_TOKEN": SENTINEL}
    values.update(over)
    return values


def body() -> TicketBody:
    return TicketBody(
        requirement="R-20261111-a3f2",
        task="T2",
        title="Add the export button",
        reason="all 3 attempts failed",
        acceptance_criteria=["AC1: exports CSV"],
        last_test_output="FAILED test_export",
        attempts=[AttemptRow(1, "R2", "fail"), AttemptRow(2, "R3", "fail")],
    )


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        yield r


@pytest.fixture
def jira() -> Iterator[JiraClient]:
    with JiraClient(JiraCredentials.from_env(env())) as c:
        yield c


def sprint_responder(state_to_key: dict[str, str]) -> Any:
    sprints = fixture("sprints.json")

    def respond(request: httpx.Request) -> httpx.Response:
        key = state_to_key.get(request.url.params.get("state", ""), "empty")
        return httpx.Response(200, json=sprints[key])

    return respond


def attach_responder(template: dict[str, Any]) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        found = re.search(rb'filename="([^"]+)"', request.content)
        name = found.group(1).decode() if found else template["filename"]
        return httpx.Response(200, json=[{**template, "filename": name}])

    return respond


def mock_all(
    router: respx.MockRouter,
    *,
    types: str = "issuetypes_ak.json",
    sprints: dict[str, str] | None = None,
) -> None:
    writes = fixture("issue_write.json")
    router.get(ISSUETYPES).mock(
        return_value=httpx.Response(200, json=fixture(types)["response"])
    )
    router.post(CREATE).mock(return_value=httpx.Response(201, json=writes["create"]))
    router.post(ATTACH).mock(side_effect=attach_responder(writes["attachments"][0]))
    router.get(SPRINTS).mock(
        side_effect=sprint_responder(
            {"active": "active"} if sprints is None else sprints
        )
    )
    router.post(re.compile(r"/rest/agile/1\.0/sprint/\d+/issue$")).mock(
        return_value=httpx.Response(204)
    )


def sent(router: respx.MockRouter, method: str, path: str) -> list[httpx.Request]:
    return [
        c.request
        for c in router.calls
        if c.request.method == method and c.request.url.path == path
    ]


def create_fields(router: respx.MockRouter) -> dict[str, Any]:
    (request,) = sent(router, "POST", CREATE)
    fields: dict[str, Any] = json.loads(request.content)["fields"]
    return fields


# AC1: Feature falls back to Story when the project has no Feature type ---------------------------------------


def test_feature_falls_back_to_story(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_all(router)  # AK's real issue-type list: no Feature
    result = jira.escalate(TARGET, issue_type="Feature", body=body())

    fields = create_fields(router)
    assert fields["issuetype"] == {"id": "10016"}  # Story
    note = json.dumps(fields["description"])
    assert "Feature" in note and "Story" in note and "no Feature type" in note
    assert result.requested_type == "Feature"
    assert result.issue_type == "Story"
    assert result.fell_back is True


def test_feature_used_when_present(router: respx.MockRouter, jira: JiraClient) -> None:
    mock_all(router, types="issuetypes_with_feature.json")
    result = jira.escalate(TARGET, issue_type="Feature", body=body())
    fields = create_fields(router)
    assert fields["issuetype"] == {"id": "10099"}
    assert "no Feature type" not in json.dumps(fields["description"])
    assert result.fell_back is False


@pytest.mark.parametrize(("wanted", "type_id"), [("Story", "10016"), ("Bug", "10017")])
def test_story_and_bug(
    router: respx.MockRouter, jira: JiraClient, wanted: str, type_id: str
) -> None:
    mock_all(router)
    jira.escalate(TARGET, issue_type=wanted, body=body())  # type: ignore[arg-type]
    assert create_fields(router)["issuetype"] == {"id": type_id}


def test_missing_bug_is_an_error(router: respx.MockRouter, jira: JiraClient) -> None:
    listing = fixture("issuetypes_ak.json")["response"]
    listing["issueTypes"] = [t for t in listing["issueTypes"] if t["name"] != "Bug"]
    router.get(ISSUETYPES).mock(return_value=httpx.Response(200, json=listing))
    with pytest.raises(JiraError, match="Bug"):
        jira.resolve_issue_type("TEST", "Bug")


def test_create_fields(router: respx.MockRouter, jira: JiraClient) -> None:
    mock_all(router)
    jira.escalate(
        TARGET, issue_type="Bug", body=body(), labels=["escalated", "has space"]
    )
    fields = create_fields(router)
    assert fields["project"] == {"key": "TEST"}
    assert fields["parent"] == {"key": "TEST-100"}
    assert fields["summary"] == "[ultron] R-20261111-a3f2/T2 Add the export button"
    assert fields["labels"] == ["ultron", "escalated", "has-space"]
    assert fields["description"]["type"] == "doc"


def test_issue_types_follow_pages(router: respx.MockRouter, jira: JiraClient) -> None:
    listing = fixture("issuetypes_ak.json")["response"]
    first = {**listing, "maxResults": 3, "issueTypes": listing["issueTypes"][:3]}
    second = {
        **listing,
        "startAt": 3,
        "maxResults": 3,
        "issueTypes": listing["issueTypes"][3:],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", "0"))
        return httpx.Response(200, json=first if start == 0 else second)

    router.get(ISSUETYPES).mock(side_effect=respond)
    names = [t.name for t in jira.issue_types("TEST")]
    assert names == ["Subtask", "Epic", "Story", "Bug", "Task"]


# AC2: active sprint, else first future sprint, else backlog -------------------------------------------------


@pytest.mark.parametrize(
    ("states", "kind", "sprint_id", "name"),
    [
        ({"active": "active", "future": "future"}, "active", 7, "TEST Sprint 4"),
        ({"future": "future"}, "future", 8, "TEST Sprint 5"),
        ({}, "backlog", None, None),
    ],
    ids=["active", "future", "backlog"],
)
def test_sprint_choice(
    router: respx.MockRouter,
    jira: JiraClient,
    states: dict[str, str],
    kind: str,
    sprint_id: int | None,
    name: str | None,
) -> None:
    mock_all(router, sprints=states)
    result = jira.escalate(TARGET, issue_type="Story", body=body())

    assert result.sprint.kind == kind
    assert result.sprint.sprint_id == sprint_id
    assert result.sprint.sprint_name == name
    assert kind in result.sprint.describe()
    moves = [
        c.request
        for c in router.calls
        if c.request.url.path.endswith("/issue") and "/sprint/" in c.request.url.path
    ]
    if sprint_id is None:
        assert moves == []
    else:
        (move,) = moves
        assert move.url.path == f"/rest/agile/1.0/sprint/{sprint_id}/issue"
        assert json.loads(move.content) == {"issues": ["TEST-1"]}


def test_sprint_queries_are_reads(router: respx.MockRouter, jira: JiraClient) -> None:
    mock_all(router, sprints={})
    choice = jira.choose_sprint(1)
    assert choice.kind == "backlog"
    states = [c.request.url.params["state"] for c in router.calls]
    assert states == ["active", "future"]
    assert {c.request.method for c in router.calls} == {"GET"}


# AC3: no sprint create, update, start or close endpoint ---------------------------------------------------


def _is_sprint_mutation(method: str, template: str) -> bool:
    if "/sprint" not in template:
        return False
    if method == "GET":
        return False
    # The one sprint write allowed: moving issues into an existing sprint.
    return not (
        method == "POST" and template == "/rest/agile/1.0/sprint/{sprint_id}/issue"
    )


def test_endpoint_table_has_no_sprint_mutation() -> None:
    assert not [
        e for e in ENDPOINTS.values() if _is_sprint_mutation(e.method, e.template)
    ]
    for method in ("POST", "PUT", "DELETE"):
        assert match_endpoint(method, "/rest/agile/1.0/sprint") is None
        assert match_endpoint(method, "/rest/agile/1.0/sprint/7") is None


def test_endpoints_used(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    log = tmp_path / "attempt-1.log"
    log.write_text("attempt log\n", encoding="utf-8")
    for states in ({"active": "active"}, {"future": "future"}, {}):
        router.reset()
        mock_all(router, sprints=states)
        jira.escalate(TARGET, issue_type="Feature", body=body(), attachments=[log])
        used = {
            match_endpoint(c.request.method, c.request.url.path) for c in router.calls
        }
        assert None not in used
        assert used <= set(ENDPOINTS)
        assert not [
            n
            for n in used
            if n and _is_sprint_mutation(ENDPOINTS[n].method, ENDPOINTS[n].template)
        ]


def test_source_has_no_other_rest_paths() -> None:
    source = Path(client_module.__file__).read_text(encoding="utf-8")
    literals = set(re.findall(r"[\"'](/rest/[^\"']*)[\"']", source))
    assert literals == {e.template for e in ENDPOINTS.values()}


def test_unknown_endpoint_refused(jira: JiraClient) -> None:
    with pytest.raises(JiraError, match="not an allowed endpoint"):
        jira._request("sprint_create")


# AC4: credentials from the environment only; the token never leaks -----------------------------------------


@pytest.mark.parametrize("name", ENV_VARS)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_env(name: str, value: str | None) -> None:
    values = env()
    if value is None:
        del values[name]
    else:
        values[name] = value
    with pytest.raises(JiraConfigError, match=name) as info:
        JiraCredentials.from_env(values)
    assert SENTINEL not in str(info.value)


def test_base_url_must_be_https() -> None:
    with pytest.raises(JiraConfigError, match="JIRA_BASE_URL"):
        JiraCredentials.from_env(env(JIRA_BASE_URL="http://example.atlassian.net"))


def test_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in env().items():
        monkeypatch.setenv(name, value)
    creds = JiraCredentials.from_env()
    assert creds.base_url == BASE
    monkeypatch.delenv("JIRA_EMAIL")
    with pytest.raises(JiraConfigError, match="JIRA_EMAIL"):
        JiraCredentials.from_env()


def test_basic_auth_sent(router: respx.MockRouter, jira: JiraClient) -> None:
    mock_all(router)
    jira.issue_types("TEST")
    (call,) = router.calls
    assert call.request.headers["Authorization"] == f"Basic {BASIC}"


def _assert_clean(text: str) -> None:
    assert SENTINEL not in text
    assert BASIC not in text


def test_token_never_leaks(
    router: respx.MockRouter, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "ultron"):
        logging.getLogger(name).setLevel(logging.DEBUG)

    creds = JiraCredentials.from_env(env())
    _assert_clean(repr(creds) + str(creds))
    log = tmp_path / "attempt-1.log"
    log.write_text("log\n", encoding="utf-8")

    with JiraClient(creds) as jira:
        _assert_clean(repr(jira))
        mock_all(router)
        result = jira.escalate(
            TARGET, issue_type="Feature", body=body(), attachments=[log]
        )
        _assert_clean(json.dumps(result.to_dict()) + repr(result))

        # HTTP error from Jira.
        router.reset()
        router.get(ISSUETYPES).mock(
            return_value=httpx.Response(401, json={"errorMessages": ["Unauthorized"]})
        )
        with pytest.raises(JiraError) as http_error:
            jira.issue_types("TEST")
        assert http_error.value.status == 401
        _assert_clean(
            str(http_error.value)
            + "".join(traceback.format_exception(http_error.value))
        )

        # Network failure.
        router.reset()
        router.get(ISSUETYPES).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(JiraUnreachable) as net_error:
            jira.issue_types("TEST")
        _assert_clean(
            str(net_error.value) + "".join(traceback.format_exception(net_error.value))
        )

    assert caplog.records, "expected some log records to inspect"
    _assert_clean(caplog.text)
    for record in caplog.records:
        _assert_clean(repr(record.args) + record.getMessage())


def test_unreachable_names_the_call(router: respx.MockRouter, jira: JiraClient) -> None:
    router.get(ISSUETYPES).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(
        JiraUnreachable, match="GET /rest/api/3/issue/createmeta/TEST/issuetypes"
    ):
        jira.issue_types("TEST")


# AC5: attachments carry X-Atlassian-Token: no-check -------------------------------------------------------


def test_attachment_header(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    log = tmp_path / "attempt-1.log"
    log.write_text("attempt 1 log\n", encoding="utf-8")
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    mock_all(router)

    result = jira.escalate(
        TARGET, issue_type="Story", body=body(), attachments=[log, plan]
    )

    uploads = sent(router, "POST", ATTACH)
    assert len(uploads) == 2
    for request, path in zip(uploads, (log, plan), strict=True):
        assert request.headers["X-Atlassian-Token"] == "no-check"
        assert request.headers["Content-Type"].startswith("multipart/form-data")
        assert f'name="file"; filename="{path.name}"'.encode() in request.content
        assert path.read_bytes() in request.content
    assert result.attachments == ("attempt-1.log", "plan.md")
    # The create call is JSON and has no no-check header.
    (create,) = sent(router, "POST", CREATE)
    assert "X-Atlassian-Token" not in create.headers


def test_partial_failure_reports_key(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    log = tmp_path / "attempt-1.log"
    log.write_text("x", encoding="utf-8")
    mock_all(router)
    router.post(ATTACH).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=body(), attachments=[log])
    assert info.value.issue_key == "TEST-1"


# Target from config -----------------------------------------------------------------------------------------


def test_target_from_config() -> None:
    target = JiraTarget.from_config(Jira(project="TEST", board="1", epic="TEST-100"))
    assert target == TARGET


@pytest.mark.parametrize(
    ("jira_cfg", "named"),
    [
        (Jira(board="1", epic="TEST-100"), "jira.project"),
        (Jira(project="TEST", epic="TEST-100"), "jira.board"),
        (Jira(project="TEST", board="1"), "jira.epic"),
        (Jira(project="TEST", board="Team board", epic="TEST-100"), "jira.board"),
    ],
)
def test_target_config_errors(jira_cfg: Jira, named: str) -> None:
    with pytest.raises(JiraConfigError, match=re.escape(named)):
        JiraTarget.from_config(jira_cfg)


def test_result_to_dict_names_sprint(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_all(router, sprints={"future": "future"})
    result = jira.escalate(TARGET, issue_type="Story", body=body())
    data = result.to_dict()
    assert isinstance(result, EscalationResult)
    assert data["key"] == "TEST-1"
    assert data["url"] == f"{BASE}/browse/TEST-1"
    assert data["sprint"] == {"kind": "future", "id": 8, "name": "TEST Sprint 5"}


# AC8: fixtures carry a capture header ----------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixtures_have_capture_header(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    capture = data["_capture"]
    dt.date.fromisoformat(capture["date"])
    assert capture["source"].split(":", 1)[0] in {"live", "docs", "derived"}
    text = path.read_text(encoding="utf-8")
    assert "@" not in text, "fixtures must not hold email addresses"
    assert "accountId" not in text


# Attempt 2: wrapped errors keep no reference to the request (AC4) ---------------------------------------------


def _reachable_text(root: object) -> Iterator[str]:
    """Every string or bytes value reachable from `root` through exception chains, args, attributes and containers.

    Tracebacks are not followed: their frames hold `self` (the client) in any method, and `repr(client)` is checked
    separately.
    """
    seen: set[int] = set()
    todo: list[tuple[object, int]] = [(root, 0)]
    while todo:
        obj, depth = todo.pop()
        if obj is None or id(obj) in seen or depth > 12:
            continue
        seen.add(id(obj))
        if isinstance(obj, str):
            yield obj
            continue
        if isinstance(obj, bytes | bytearray | memoryview):
            yield bytes(obj).decode("latin-1")
            continue
        if isinstance(obj, int | float) or callable(obj):  # numbers, classes, functions
            continue
        children: list[object] = []
        if isinstance(obj, dict):
            children += [*obj.keys(), *obj.values()]
        elif isinstance(obj, list | tuple | set | frozenset):
            children += list(obj)
        if isinstance(obj, BaseException):
            children += [obj.args, obj.__cause__, obj.__context__]
        if hasattr(obj, "__dict__"):
            children.append(vars(obj))
        for klass in type(obj).__mro__:
            slots = klass.__dict__.get("__slots__", ())
            for slot in (slots,) if isinstance(slots, str) else slots:
                children.append(getattr(obj, slot, None))
        todo += [(child, depth + 1) for child in children]


def _assert_nothing_reachable(exc: BaseException) -> None:
    text = "\n".join(_reachable_text(exc))
    assert SENTINEL not in text, "token reachable from the exception"
    assert BASIC not in text, "Basic-auth header reachable from the exception"


def test_reachable_text_finds_the_header_in_a_raw_httpx_error() -> None:
    # The walker itself must see what it's meant to catch, or the tests below prove nothing.
    request = httpx.Request("GET", BASE, headers={"Authorization": f"Basic {BASIC}"})
    with pytest.raises(AssertionError, match="Basic-auth"):
        _assert_nothing_reachable(httpx.ConnectError("x", request=request))
    hidden = JiraError("outer")
    hidden.__context__ = httpx.ConnectError("x", request=request)
    with pytest.raises(AssertionError, match="Basic-auth"):
        _assert_nothing_reachable(hidden)
    with pytest.raises(AssertionError, match="token"):
        _assert_nothing_reachable(JiraError("x", path=SENTINEL))
    with pytest.raises(AssertionError, match="token"):
        _assert_nothing_reachable(ValueError(JiraCredentials.from_env(env())))


@pytest.mark.parametrize(
    ("error", "unreachable"),
    [
        (httpx.ConnectError, True),
        (httpx.ReadTimeout, True),
        (httpx.RemoteProtocolError, True),
        (httpx.DecodingError, False),
        (httpx.TooManyRedirects, False),
    ],
    ids=lambda v: v.__name__ if isinstance(v, type) else str(v),
)
def test_wrapped_errors_hold_no_auth(
    router: respx.MockRouter, jira: JiraClient, error: type, unreachable: bool
) -> None:
    router.get(ISSUETYPES).mock(side_effect=error)
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    exc = info.value
    assert isinstance(exc, JiraUnreachable) is unreachable
    assert exc.__cause__ is None and exc.__context__ is None
    assert error.__name__ in str(exc) and ISSUETYPES in str(exc)
    _assert_nothing_reachable(exc)


def test_wrapped_error_after_create_holds_no_auth(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_all(router)
    router.get(SPRINTS).mock(side_effect=httpx.ReadTimeout)
    with pytest.raises(JiraUnreachable) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    assert info.value.issue_key == "TEST-1"
    _assert_nothing_reachable(info.value)


# Attempt 2: any failure after create carries the issue key -------------------------------------------------


def _missing(tmp_path: Path) -> Path:
    return tmp_path / "missing.log"


def _directory(tmp_path: Path) -> Path:
    folder = tmp_path / "attempt-1.log"
    folder.mkdir()
    return folder


@pytest.mark.parametrize(
    "make_path", [_missing, _directory], ids=["missing", "directory"]
)
def test_unreadable_attachment_carries_key(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, make_path: Any
) -> None:
    mock_all(router)
    path = make_path(tmp_path)
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=body(), attachments=[path])
    assert info.value.issue_key == "TEST-1"
    assert path.name in str(info.value)
    assert len(sent(router, "POST", CREATE)) == 1
    assert sent(router, "POST", ATTACH) == []
    assert info.value.__cause__ is None and info.value.__context__ is None
    _assert_nothing_reachable(info.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"values": [{"name": "no id"}]}),
        httpx.Response(200, text="<html>not json</html>"),
    ],
    ids=["sprint-without-id", "not-json"],
)
def test_unexpected_sprint_response_carries_key(
    router: respx.MockRouter, jira: JiraClient, response: httpx.Response
) -> None:
    mock_all(router)
    router.get(SPRINTS).mock(return_value=response)
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    assert info.value.issue_key == "TEST-1"
    assert "TEST-1" in str(info.value)
    assert len(sent(router, "POST", CREATE)) == 1
    assert info.value.__cause__ is None and info.value.__context__ is None
    _assert_nothing_reachable(info.value)


# Attempt 2: a board without sprints (kanban) means the backlog ---------------------------------------------


def test_board_without_sprints_goes_to_backlog(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    no_sprints = fixture("sprints.json")["no_sprints"]
    mock_all(router)
    router.get(SPRINTS).mock(
        return_value=httpx.Response(no_sprints["status"], json=no_sprints["body"])
    )
    result = jira.escalate(TARGET, issue_type="Story", body=body())

    assert result.sprint.kind == "backlog"
    assert result.sprint.sprint_id is None
    assert result.sprint.reason is not None
    assert "does not support sprints" in result.sprint.reason
    assert "does not support sprints" in result.sprint.describe()
    assert result.to_dict()["sprint"]["kind"] == "backlog"
    assert "does not support sprints" in result.to_dict()["sprint"]["reason"]
    assert len(sent(router, "GET", SPRINTS)) == 1  # no second lookup
    assert [
        c for c in router.calls if "/rest/agile/1.0/sprint/" in c.request.url.path
    ] == []


def test_plain_backlog_has_no_reason(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_all(router, sprints={})
    result = jira.escalate(TARGET, issue_type="Story", body=body())
    assert result.sprint.reason is None
    assert result.to_dict()["sprint"] == {"kind": "backlog", "id": None, "name": None}


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_other_sprint_lookup_errors_still_fail(
    router: respx.MockRouter, jira: JiraClient, status: int
) -> None:
    mock_all(router)
    router.get(SPRINTS).mock(
        return_value=httpx.Response(status, json={"errorMessages": ["nope"]})
    )
    with pytest.raises(JiraError) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    assert info.value.status == status
    assert info.value.issue_key == "TEST-1"
