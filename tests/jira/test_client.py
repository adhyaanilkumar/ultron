"""The Jira Cloud client, with HTTP mocked by respx. No test here reaches the network."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import logging
import re
import traceback
import types
from collections.abc import Callable, Iterator, Mapping
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
    AttachmentProblem,
    EscalationResult,
    JiraClient,
    JiraConfigError,
    JiraCredentials,
    JiraError,
    JiraTarget,
    JiraUnreachable,
    MissingAttachments,
    check_attachments,
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
COMMENT = "/rest/api/3/issue/TEST-1/comment"
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

    # report.md (D27) first, then the files in the order given; every upload carries the no-check header.
    uploads = sent(router, "POST", ATTACH)
    assert len(uploads) == 3
    for request in uploads:
        assert request.headers["X-Atlassian-Token"] == "no-check"
        assert request.headers["Content-Type"].startswith("multipart/form-data")
    assert b'name="file"; filename="report.md"' in uploads[0].content
    for request, path in zip(uploads[1:], (log, plan), strict=True):
        assert f'name="file"; filename="{path.name}"'.encode() in request.content
        assert path.read_bytes() in request.content
    assert result.attachments == ("report.md", "attempt-1.log", "plan.md")
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
# Attempt 3: the walker follows traceback frames too. Attempt 2's walker didn't, and that is where its leak was.

ULTRON_PKG = Path(client_module.__file__).resolve().parents[1]
LEAVES = (
    bool,
    int,
    float,
    complex,
    type,
    types.ModuleType,
    types.BuiltinFunctionType,
    types.CodeType,
)


def _in_ultron(frame: types.FrameType) -> bool:
    try:
        return Path(frame.f_code.co_filename).resolve().is_relative_to(ULTRON_PKG)
    except (OSError, ValueError):
        return False


def _frames_from_ultron(tb: types.TracebackType | None) -> Iterator[types.FrameType]:
    """A traceback's frames from ultron's outermost frame inward: ultron's own, and every library frame below them.

    The frames above are the caller's (here, the test's), which hold the router, the client and the token themselves.
    """
    started = False
    while tb is not None:
        started = started or _in_ultron(tb.tb_frame)
        if started:
            yield tb.tb_frame
        tb = tb.tb_next


def _reach(root: object) -> tuple[list[str], list[str]]:
    """Every string or bytes value reachable from `root`, and the type of every httpx or httpcore object met.

    Followed: exception `__cause__`, `__context__`, `args`, `__notes__` and `__traceback__`; the locals of every
    traceback frame from ultron's outermost frame inward; containers and mappings (httpx.Headers too); `vars()` and
    slots; bound methods' `__self__` and closure cells. A `JiraClient` holds the credentials by design (its httpx client
    sends them) and is the `self` of every method frame, so it is seen only through its repr and str.
    """
    texts: list[str] = []
    foreign: list[str] = []
    seen: set[int] = set()
    todo: list[object] = [root]
    while todo:
        obj = todo.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, str):
            texts.append(obj)
            continue
        if isinstance(obj, bytes | bytearray | memoryview):
            texts.append(bytes(obj).decode("latin-1"))
            continue
        if isinstance(obj, LEAVES):
            continue
        if isinstance(obj, JiraClient):
            texts += [repr(obj), str(obj)]
            continue
        if type(obj).__module__.partition(".")[0] in {"httpx", "httpcore"}:
            foreign.append(type(obj).__name__)
        if isinstance(obj, types.TracebackType):
            todo += [dict(frame.f_locals) for frame in _frames_from_ultron(obj)]
            continue
        if isinstance(obj, types.FrameType):
            continue
        if isinstance(obj, types.MethodType):
            todo.append(obj.__self__)
            continue
        if isinstance(obj, types.FunctionType):
            for cell in obj.__closure__ or ():
                with contextlib.suppress(ValueError):  # an empty cell
                    todo.append(cell.cell_contents)
            continue
        if isinstance(obj, BaseException):
            todo += [obj.__cause__, obj.__context__, obj.args, obj.__traceback__]
            todo.append(getattr(obj, "__notes__", None))
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                todo += [key, value]
        elif isinstance(obj, list | tuple | set | frozenset):
            todo += list(obj)
        with contextlib.suppress(TypeError):
            todo.append(dict(vars(obj)))
        for klass in type(obj).__mro__:
            slots = klass.__dict__.get("__slots__", ())
            for slot in (slots,) if isinstance(slots, str) else slots:
                with contextlib.suppress(AttributeError):
                    todo.append(getattr(obj, slot))
    return texts, foreign


def _assert_nothing_reachable(root: object) -> None:
    texts, foreign = _reach(root)
    text = "\n".join(texts)
    assert SENTINEL not in text, "token reachable from the exception"
    assert BASIC not in text, "Basic-auth header reachable from the exception"
    assert not foreign, (
        f"httpx objects reachable from the exception: {sorted(set(foreign))}"
    )


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


def test_reachable_text_follows_traceback_frames(jira: JiraClient) -> None:
    # Through the locals of ultron's own frame: the caller's header, still in `_request`'s kwargs.
    with pytest.raises(JiraError) as in_frame:
        jira._request("not-an-endpoint", headers={"Authorization": f"Basic {BASIC}"})
    with pytest.raises(AssertionError, match="Basic-auth"):
        _assert_nothing_reachable(in_frame.value)

    # Through a frame below ultron's (code that ultron called), as httpx's own frames would be.
    class Boom:
        def __str__(self) -> str:
            header = f"Basic {BASIC}"
            raise RuntimeError(f"no path for a {len(header)}-character header")

    with pytest.raises(RuntimeError) as below:
        jira.issue_types(Boom())  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="Basic-auth"):
        _assert_nothing_reachable(below.value)

    # Not through the caller's frames above ultron's: what they hold is the caller's own.
    def caller(header: str) -> None:
        jira._request("not-an-endpoint")

    with pytest.raises(JiraError) as above:
        caller(f"Basic {BASIC}")
    _assert_nothing_reachable(above.value)

    # The client is seen through its repr only; any httpx object reached is reported, token or not.
    _assert_nothing_reachable(ValueError(jira))
    with pytest.raises(AssertionError, match="httpx objects"):
        _assert_nothing_reachable(JiraError("x", path=httpx.URL(BASE)))  # type: ignore[arg-type]


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
# D27 (attempt 3): a missing or unreadable attachment is no longer such a failure: it's refused before any call.


def _missing(tmp_path: Path) -> Path:
    return tmp_path / "missing.log"


def _directory(tmp_path: Path) -> Path:
    folder = tmp_path / "attempt-2.log"
    folder.mkdir()
    return folder


@pytest.mark.parametrize(
    "make_path", [_missing, _directory], ids=["missing", "directory"]
)
def test_unreadable_attachment_is_refused_before_any_call(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, make_path: Any
) -> None:
    mock_all(router)
    path = make_path(tmp_path)
    with pytest.raises(MissingAttachments) as info:
        jira.escalate(
            TARGET, issue_type="Story", body=body(), attachments=[_log(tmp_path), path]
        )
    exc = info.value
    assert isinstance(exc, JiraError) and not isinstance(exc, JiraUnreachable)
    assert exc.issue_key is None
    assert [p.path for p in exc.problems] == [path]
    assert str(path) in str(exc) and "no ticket was created" in str(exc)
    assert router.calls.call_count == 0  # not even the issue-type lookup
    assert exc.__cause__ is None and exc.__context__ is None
    _assert_nothing_reachable(exc)


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


# Attempt 3: no error reaches the token or an httpx object, through traceback frames either ----------------------
# Each test runs the walker first, so on attempt 2's code it fails on the leak itself.

ISSUE = "/rest/api/3/issue/TEST-1"
MOVE = "/rest/agile/1.0/sprint/7/issue"
STEPS = ("types", "create", "attach", "sprint_active", "sprint_future", "move")
AFTER_CREATE = frozenset({"attach", "sprint_active", "sprint_future", "move"})
Responder = Callable[[httpx.Request], httpx.Response]


def _log(tmp_path: Path) -> Path:
    path = tmp_path / "attempt-1.log"
    path.write_text("attempt log\n", encoding="utf-8")
    return path


def fail_at(router: respx.MockRouter, step: str, respond: Responder) -> None:
    """The happy routes, except that `step` answers with `respond` (which may raise)."""
    mock_all(router)
    if step.startswith("sprint_"):
        state = step.removeprefix("sprint_")
        happy = sprint_responder(
            {"future": "future"}
        )  # no active sprint, so the future lookup runs

        def sprints(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("state") == state:
                return respond(request)
            return happy(request)

        router.get(SPRINTS).mock(side_effect=sprints)
    elif step == "types":
        router.get(ISSUETYPES).mock(side_effect=respond)
    elif step == "create":
        router.post(CREATE).mock(side_effect=respond)
    elif step == "attach":
        router.post(ATTACH).mock(side_effect=respond)
    else:
        router.post(re.compile(r"/rest/agile/1\.0/sprint/\d+/issue$")).mock(
            side_effect=respond
        )


def echo(status: int) -> Responder:
    """An error reply that echoes the Authorization header back: the worst case for a response kept anywhere."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"WWW-Authenticate": request.headers["Authorization"]},
            json={"errorMessages": [f"jira says {status}"]},
        )

    return respond


@pytest.mark.parametrize("status", [400, 401, 500])
@pytest.mark.parametrize("step", STEPS)
def test_error_status_holds_no_auth(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, step: str, status: int
) -> None:
    fail_at(router, step, echo(status))
    if status == 400 and step.startswith(
        "sprint_"
    ):  # a board without sprints: the backlog
        result = jira.escalate(
            TARGET, issue_type="Story", body=body(), attachments=[_log(tmp_path)]
        )
        _assert_nothing_reachable(result)
        assert result.sprint.kind == "backlog"
        return
    with pytest.raises(JiraError) as info:
        jira.escalate(
            TARGET, issue_type="Story", body=body(), attachments=[_log(tmp_path)]
        )
    exc = info.value
    _assert_nothing_reachable(exc)
    assert exc.status == status and f"jira says {status}" in str(exc)
    assert not isinstance(exc, JiraUnreachable)
    assert exc.__cause__ is None and exc.__context__ is None
    if step in AFTER_CREATE:
        assert exc.issue_key == "TEST-1" and "TEST-1 was created" in str(exc)
    else:
        assert exc.issue_key is None


PUBLIC_CALLS: dict[str, tuple[str, str, Callable[[JiraClient, Path], object]]] = {
    "issue_types": ("GET", ISSUETYPES, lambda j, _: j.issue_types("TEST")),
    "resolve_issue_type": (
        "GET",
        ISSUETYPES,
        lambda j, _: j.resolve_issue_type("TEST", "Bug"),
    ),
    "create_issue": (
        "POST",
        CREATE,
        lambda j, _: j.create_issue(
            project="TEST",
            issue_type_id="10016",
            summary="s",
            description={"type": "doc", "version": 1, "content": []},
            epic="TEST-100",
        ),
    ),
    "get_issue": ("GET", ISSUE, lambda j, _: j.get_issue("TEST-1")),
    "delete_issue": ("DELETE", ISSUE, lambda j, _: j.delete_issue("TEST-1")),
    "attach": ("POST", ATTACH, lambda j, log: j.attach("TEST-1", log)),
    "choose_sprint": ("GET", SPRINTS, lambda j, _: j.choose_sprint(1)),
    "move_to_sprint": ("POST", MOVE, lambda j, _: j.move_to_sprint(7, ["TEST-1"])),
    "comment": ("POST", COMMENT, lambda j, _: j.comment("TEST-1", "Added the log.")),
    "comment_attach": (
        "POST",
        ATTACH,
        lambda j, log: j.comment("TEST-1", "Added.", log),
    ),
}


@pytest.mark.parametrize("call", sorted(PUBLIC_CALLS))
def test_public_method_errors_hold_no_auth(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, call: str
) -> None:
    # Every method frame that an error passes through, not only escalate's.
    method, path, run = PUBLIC_CALLS[call]
    getattr(router, method.lower())(path).mock(side_effect=echo(500))
    with pytest.raises(JiraError) as info:
        run(jira, _log(tmp_path))
    _assert_nothing_reachable(info.value)
    assert info.value.status == 500 and "jira says 500" in str(info.value)


@pytest.mark.parametrize("step", ["types", "sprint_active"])
def test_non_json_body_is_a_jira_error(
    router: respx.MockRouter, jira: JiraClient, step: str
) -> None:
    fail_at(
        router, step, lambda r: httpx.Response(200, text="<html>a proxy page</html>")
    )
    with pytest.raises(Exception) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    _assert_nothing_reachable(info.value)
    assert isinstance(info.value, JiraError)
    assert "isn't JSON" in str(info.value)
    assert info.value.issue_key == ("TEST-1" if step in AFTER_CREATE else None)


SEND_ERRORS: list[Callable[[], Exception]] = [
    lambda: httpx.InvalidURL("Invalid URL component 'path'"),
    lambda: httpx.CookieConflict("Multiple cookies exist with name=session"),
    httpx.StreamClosed,
    lambda: RuntimeError("the transport broke"),
]


@pytest.mark.parametrize("step", ["types", "sprint_active"])
@pytest.mark.parametrize(
    "make",
    SEND_ERRORS,
    ids=["InvalidURL", "CookieConflict", "StreamClosed", "RuntimeError"],
)
def test_other_send_errors_are_wrapped(
    router: respx.MockRouter,
    jira: JiraClient,
    step: str,
    make: Callable[[], Exception],
) -> None:
    # Not httpx.HTTPError, so attempt 2 let them out raw, through httpx frames holding the request.
    def respond(request: httpx.Request) -> httpx.Response:
        raise make()

    fail_at(router, step, respond)
    with pytest.raises(Exception) as info:
        jira.escalate(TARGET, issue_type="Story", body=body())
    exc = info.value
    _assert_nothing_reachable(exc)
    assert isinstance(exc, JiraError) and not isinstance(exc, JiraUnreachable)
    assert type(make()).__name__ in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert exc.issue_key == ("TEST-1" if step in AFTER_CREATE else None)


def _set_env(monkeypatch: pytest.MonkeyPatch, name: str, template: str) -> None:
    """Put the three variables in os.environ, with `name` set from `template` (`{token}` becomes the token).

    Done here so that the test's own frame never holds the token: the check with locals below covers every frame.
    """
    for key, value in env(**{name: template.format(token=SENTINEL)}).items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("entry", ["credentials", "client"])
@pytest.mark.parametrize(
    ("name", "template"),
    [
        ("JIRA_BASE_URL", "http://example.atlassian.net"),
        ("JIRA_BASE_URL", "https://example.atlassian.net:port"),
        ("JIRA_BASE_URL", "https://"),
        # A token pasted into the URL would show in repr(client), result.url and the event log.
        ("JIRA_BASE_URL", "https://bot:{token}@example.atlassian.net"),
        ("JIRA_EMAIL", ""),
    ],
    ids=["http", "malformed", "no-host", "userinfo", "no-email"],
)
def test_config_errors_hold_no_token(
    monkeypatch: pytest.MonkeyPatch, name: str, template: str, entry: str
) -> None:
    # The production path: the variables are in os.environ and nothing is passed in.
    _set_env(monkeypatch, name, template)
    make = JiraCredentials.from_env if entry == "credentials" else JiraClient.from_env
    with pytest.raises(Exception) as info:
        make()
    _assert_nothing_reachable(info.value)
    assert isinstance(info.value, JiraConfigError)
    assert name in str(info.value)
    with_locals = traceback.TracebackException.from_exception(
        info.value, capture_locals=True
    )
    assert SENTINEL not in "".join(with_locals.format())


@pytest.mark.parametrize("entry", ["credentials", "client"])
def test_config_error_keeps_no_mapping_passed_in(entry: str) -> None:
    # How the tests call it: a mapping with the token, which the error must not keep in from_env's frame.
    make = JiraCredentials.from_env if entry == "credentials" else JiraClient.from_env
    with pytest.raises(Exception) as info:
        make(env(JIRA_BASE_URL="http://example.atlassian.net"))
    _assert_nothing_reachable(info.value)
    assert isinstance(info.value, JiraConfigError)
    assert "JIRA_BASE_URL" in str(info.value)
    with_locals = traceback.TracebackException.from_exception(
        info.value, capture_locals=True
    )
    assert SENTINEL not in "".join(with_locals.format())


def test_client_setup_error_holds_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # httpx.Client(...) itself fails (here on a proxy URL it can't use) while its frame holds the BasicAuth header.
    _set_env(monkeypatch, "JIRA_EMAIL", EMAIL)
    for name in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", "foo://proxy.example.com")
    with pytest.raises(Exception) as info:
        JiraClient.from_env()
    _assert_nothing_reachable(info.value)
    assert isinstance(info.value, JiraConfigError)
    assert "ValueError" in str(info.value)
    with_locals = traceback.TracebackException.from_exception(
        info.value, capture_locals=True
    )
    assert SENTINEL not in "".join(with_locals.format())
    assert BASIC not in "".join(with_locals.format())


@pytest.mark.parametrize("page", ["json", "html", "cut"])
@pytest.mark.parametrize("step", ["types", "attach", "sprint_active"])
def test_auth_quoted_in_an_error_body_is_masked(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path, step: str, page: str
) -> None:
    # A proxy or a debug page can quote the request headers in its error page, and that text goes into errors and
    # into the backlog reason (an event). The token is masked before the text is cut, so no piece of it is left.
    status = 400 if step.startswith("sprint_") else 500

    def respond(request: httpx.Request) -> httpx.Response:
        quoted = f"Authorization: {request.headers['Authorization']} (token {SENTINEL})"
        if page == "json":
            return httpx.Response(status, json={"errorMessages": [quoted]})
        if page == "html":
            return httpx.Response(status, text=f"<html><pre>{quoted}</pre></html>")
        return httpx.Response(
            status, text="x" * 490 + SENTINEL
        )  # the cut at 500 falls inside the token

    fail_at(router, step, respond)
    if status == 400:
        result = jira.escalate(
            TARGET, issue_type="Story", body=body(), attachments=[_log(tmp_path)]
        )
        _assert_nothing_reachable(result)
        texts = [result.sprint.describe(), json.dumps(result.to_dict())]
    else:
        with pytest.raises(JiraError) as info:
            jira.escalate(
                TARGET, issue_type="Story", body=body(), attachments=[_log(tmp_path)]
            )
        _assert_nothing_reachable(info.value)
        texts = [str(info.value)]
    for text in texts:
        assert SENTINEL[:8] not in text and BASIC[:8] not in text
        assert "***" in text


def test_redirect_is_an_error(router: respx.MockRouter, jira: JiraClient) -> None:
    # Redirects aren't followed, and to httpx a 3xx is neither an error nor a success.
    router.get(ISSUETYPES).mock(
        return_value=httpx.Response(
            302, headers={"Location": f"{BASE}/login"}, text="moved"
        )
    )
    with pytest.raises(Exception) as info:
        jira.issue_types("TEST")
    _assert_nothing_reachable(info.value)
    assert isinstance(info.value, JiraError) and info.value.status == 302


# D27 (attempt 3): AC9 a short description and an attached report.md -------------------------------------------


def _adf_text(node: Any) -> str:
    if isinstance(node, dict):
        own = str(node.get("text", "")) if node.get("type") == "text" else ""
        return own + "".join(_adf_text(c) for c in node.get("content", []))
    return ""


def test_description_is_short_and_report_md_is_attached_first(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_all(router)  # AK's types have no Feature, so the fallback note shows in both
    reason = "rejected by the tester: " + "the CSV header is wrong. " * 40
    ticket = TicketBody(
        requirement="R-20261111-a3f2",
        task="T2",
        title="Add the export button",
        reason=reason,
        acceptance_criteria=["AC1: exports CSV", "AC2: header row matches"],
        last_test_output="FAILED test_export - AssertionError: header",
        attempts=[
            AttemptRow(1, "R2", "fail", "csv empty"),
            AttemptRow(2, "R3", "fail", "header wrong"),
        ],
    )
    result = jira.escalate(
        TARGET, issue_type="Feature", body=ticket, attachments=[_log(tmp_path)]
    )

    description = create_fields(router)["description"]
    lines = [_adf_text(p) for p in description["content"]]
    assert len("\n".join(lines)) <= 280
    assert lines[0].startswith("Note: ") and "no Feature type" in lines[0]
    assert lines[1].startswith("Reason: rejected by the tester") and lines[1].endswith(
        "…"
    )
    assert "report.md" in lines[-1]
    assert "AC1" not in json.dumps(description)
    assert "AssertionError" not in json.dumps(description)

    upload = sent(router, "POST", ATTACH)[0]
    assert b'name="file"; filename="report.md"' in upload.content
    report = upload.content.decode()
    for needed in [
        reason.strip(),
        "- AC1: exports CSV",
        "| 1 | R2 | fail | csv empty |",
        "| 2 | R3 | fail | header wrong |",
        "FAILED test_export - AssertionError: header",
        "no Feature type",
    ]:
        assert needed in report, needed
    assert result.attachments == ("report.md", "attempt-1.log")


# D27 (attempt 3): AC10 attachments checked before any call, and comments ---------------------------------------


def test_check_attachments_names_every_problem(tmp_path: Path) -> None:
    good = _log(tmp_path)
    missing = tmp_path / "missing.log"
    folder = tmp_path / "plan.md"
    folder.mkdir()
    assert check_attachments([good, missing, folder]) == [
        AttachmentProblem(missing, "missing"),
        AttachmentProblem(folder, "not a file"),
    ]
    assert check_attachments([good]) == []
    assert JiraClient.check_attachments([str(missing)]) == [
        AttachmentProblem(missing, "missing")
    ]


def test_check_attachments_reports_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = _log(tmp_path)
    real_open = Path.open

    def deny(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == locked:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    assert check_attachments([locked]) == [
        AttachmentProblem(locked, "unreadable: PermissionError")
    ]


def test_comment_is_cut_to_280_and_attaches_first(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    writes = fixture("issue_write.json")
    router.post(ATTACH).mock(side_effect=attach_responder(writes["attachments"][0]))
    route = router.post(COMMENT).mock(
        return_value=httpx.Response(201, json=writes["comment"])
    )
    answer = tmp_path / "answer.md"
    answer.write_text("the whole answer\n", encoding="utf-8")
    result = jira.comment(
        "TEST-1", "The log went to the wrong folder; it's attached now. " * 10, answer
    )

    assert [c.request.url.path for c in router.calls] == [ATTACH, COMMENT]
    (upload,) = sent(router, "POST", ATTACH)
    assert upload.headers["X-Atlassian-Token"] == "no-check"
    assert b'filename="answer.md"' in upload.content
    posted = json.loads(route.calls.last.request.content)["body"]
    assert posted["type"] == "doc" and posted["version"] == 1
    text = _adf_text(posted)
    assert len(text) <= 280 and text.endswith("…")
    assert text.startswith("The log went to the wrong folder; it's attached now.")
    assert result.text == text
    assert result.to_dict() == {
        "issue_key": "TEST-1",
        "comment_id": writes["comment"]["id"],
        "text": text,
        "attachment": "answer.md",
    }


def test_comment_without_attachment(router: respx.MockRouter, jira: JiraClient) -> None:
    route = router.post(COMMENT).mock(
        return_value=httpx.Response(201, json=fixture("issue_write.json")["comment"])
    )
    result = jira.comment("TEST-1", "Short answer.\nTwo lines.")
    assert [c.request.url.path for c in router.calls] == [COMMENT]
    posted = json.loads(route.calls.last.request.content)["body"]
    assert _adf_text(posted) == "Short answer. Two lines."
    assert result.attachment is None


def test_comment_refuses_a_missing_attachment_before_any_call(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    missing = tmp_path / "answer.md"
    with pytest.raises(MissingAttachments) as info:
        jira.comment("TEST-1", "See the answer.", missing)
    assert router.calls.call_count == 0
    assert info.value.problems == (AttachmentProblem(missing, "missing"),)
    assert "no comment was added" in str(info.value)
    _assert_nothing_reachable(info.value)


def test_comment_needs_text(router: respx.MockRouter, jira: JiraClient) -> None:
    with pytest.raises(ValueError, match="text"):
        jira.comment("TEST-1", "  \n ")
    assert router.calls.call_count == 0


def test_the_token_is_masked_in_everything_sent(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    # Agents and the target repo's tests may see the environment: a transcript that ran `env`, or a test that dumps
    # its config, would carry the token into the report, the logs or a comment, and so onto the board.
    mock_all(router)
    router.post(COMMENT).mock(
        return_value=httpx.Response(201, json=fixture("issue_write.json")["comment"])
    )
    leaked = f"JIRA_API_TOKEN={SENTINEL} Authorization: Basic {BASIC}"
    log = tmp_path / "attempt-1.log"
    log.write_text(f"$ env\n{leaked}\n", encoding="utf-8")
    ticket = TicketBody(
        requirement="R-20261111-a3f2",
        task="T2",
        title="Add the export button",
        reason=f"stopped: {leaked}",
        acceptance_criteria=[f"AC1: {leaked}"],
        last_test_output="x" * 19_990 + leaked,  # the tail cut falls inside the token
        attempts=[AttemptRow(1, "R2", "fail", leaked)],
    )
    result = jira.escalate(TARGET, issue_type="Story", body=ticket, attachments=[log])
    comment = jira.comment("TEST-1", f"Found it: {leaked}", log)

    for call in router.calls:
        sent_body = call.request.content.decode("utf-8", "replace")
        assert SENTINEL[:8] not in sent_body and BASIC[:8] not in sent_body, (
            call.request.url.path
        )
    assert b"***" in sent(router, "POST", ATTACH)[0].content  # the report
    _assert_nothing_reachable(result)
    _assert_nothing_reachable(comment)
    assert SENTINEL not in json.dumps(comment.to_dict())


def test_attach_refuses_a_missing_file(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    with pytest.raises(MissingAttachments):
        jira.attach("TEST-1", tmp_path / "missing.log")
    assert router.calls.call_count == 0
