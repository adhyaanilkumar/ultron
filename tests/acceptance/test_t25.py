"""Acceptance tests for T25 (Jira Cloud client), written by the testing agent from tasks.md and spec §12.

HTTP is mocked with respx; nothing here reaches the network or Jira. AC6/AC7 are owner steps (live create/delete and
a browser screenshot) and aren't covered here.

D27 (owner, 2026-11-11) changed what ten attempt-1/2 tests checked: the description is at most 280 characters with the
details in an attached report.md, and attachments are checked before any call. Those ten now check D27 instead (the
owner approved replacing them); they are marked "D27:" where they changed.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import functools
import json
import logging
import os
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
    CommentResult,
    JiraClient,
    JiraConfigError,
    JiraCredentials,
    JiraError,
    JiraTarget,
    JiraUnreachable,
    MissingAttachments,
    TicketBody,
    check_attachments,
    ticket_summary,
    to_adf,
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
ISSUE = "/rest/api/3/issue/TEST-1"
COMMENT = "/rest/api/3/issue/TEST-1/comment"

# D27 (spec §12): Jira text stays within 280 characters; the details go in report.md.
LIMIT = 280
REPORT = "report.md"


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


# ---- D27 helpers: what a reader sees in the description, the uploaded files, report.md ----

ADF_INLINE = {"text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status"}


def adf_plain(node: Any) -> str:
    """ADF as a reader sees it: inline nodes run together, block nodes one per line, a hard break is a line break."""
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    kids = [k for k in node.get("content", []) if isinstance(k, dict)]
    if kids and all(k.get("type") in ADF_INLINE for k in kids):
        return "".join(adf_plain(k) for k in kids)
    return "\n".join(adf_plain(k) for k in kids)


def squash(text: str) -> str:
    """`text` on one line: every run of whitespace, line breaks included, as one space."""
    return " ".join(text.split())


def multipart_file(request: httpx.Request) -> tuple[str, bytes]:
    """The one `file` part of a multipart upload: its file name and its bytes."""
    request.read()
    boundary = request.headers["Content-Type"].split("boundary=", 1)[1].strip('"')
    found: list[tuple[str, bytes]] = []
    for part in request.content.split(b"--" + boundary.encode()):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or b'name="file"' not in head:
            continue
        name = re.search(rb'filename="([^"]*)"', head)
        assert name is not None, f"upload part without a file name: {head!r}"
        found.append((name.group(1).decode(), data.removesuffix(b"\r\n")))
    assert len(found) == 1, f"expected one file part, got {len(found)}"
    return found[0]


def upload_requests(
    router: respx.MockRouter, path: str = ATTACH
) -> list[httpx.Request]:
    return [
        c.request
        for c in router.calls
        if c.request.method == "POST" and c.request.url.path == path
    ]


def uploads(router: respx.MockRouter, path: str = ATTACH) -> list[tuple[str, bytes]]:
    return [multipart_file(r) for r in upload_requests(router, path)]


def report_md(router: respx.MockRouter) -> str:
    """report.md as uploaded: it must be the first upload (spec §12, D27)."""
    sent = uploads(router)
    names = [name for name, _ in sent]
    assert names and names[0] == REPORT, f"{REPORT} must be uploaded first, got {names}"
    return sent[0][1].decode("utf-8")


def description_of(router: respx.MockRouter) -> str:
    doc = create_payload(router)["fields"]["description"]
    assert (
        isinstance(doc, dict) and doc.get("type") == "doc" and doc.get("version") == 1
    )
    return adf_plain(doc)


def table_rows(markdown: str) -> list[list[str]]:
    """The cells of every Markdown table row, separator rows left out."""
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        s = line.strip()
        if len(s) > 1 and s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s[1:-1].split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                rows.append(cells)
    return rows


def fenced_blocks(markdown: str) -> list[str]:
    """The text inside every fenced code block (``` or ~~~, any length)."""
    blocks: list[str] = []
    fence: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        s = line.strip()
        if fence is None:
            m = re.match(r"(`{3,}|~{3,})", s)
            if m:
                fence, buf = m.group(1), []
        elif s and set(s) == {fence[0]} and len(s) >= len(fence):
            blocks.append("\n".join(buf))
            fence = None
        else:
            buf.append(line)
    return blocks


def list_items(markdown: str) -> list[str]:
    return [
        line.strip()[2:]
        for line in markdown.splitlines()
        if line.strip()[:2] in {"- ", "* ", "+ "}
    ]


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


# Pieces of the secret too: text cut at a length limit could keep part of it. BASIC encodes "email:token"; from its
# 33rd character on it encodes token bytes only (the start encodes the email, which isn't secret).
TOKEN_PIECES = (TOKEN[:16], TOKEN[-10:])
BASIC_PIECES = (BASIC[32:48], BASIC[-16:])


def assert_clean(text: str, where: str) -> None:
    assert TOKEN not in text, f"token in {where}"
    assert BASIC not in text, f"Basic-auth token in {where}"
    for piece in TOKEN_PIECES:
        assert piece not in text, f"part of the token ({piece!r}) in {where}"
    for piece in BASIC_PIECES:
        assert piece not in text, f"part of the Basic-auth token in {where}"


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
    # D27: three uploads, report.md first and then the two files in the order given (was: two uploads). Each one
    # still carries X-Atlassian-Token: no-check and is a multipart `file` part with its own name and bytes.
    mock_happy(router)
    files = [log_file(tmp_path, "attempt-1.log"), log_file(tmp_path, "plan.md")]
    files[1].write_text("# plan\nsteps\n", encoding="utf-8")
    result = jira.escalate(TARGET, issue_type="Bug", body=ticket(), attachments=files)
    sent = upload_requests(router)
    assert len(sent) == 3
    expected = [REPORT, "attempt-1.log", "plan.md"]
    for req, name in zip(sent, expected, strict=True):
        assert req.method == "POST"
        assert req.headers["X-Atlassian-Token"] == "no-check"
        assert req.headers["Content-Type"].startswith("multipart/form-data")
        assert b'name="file"' in req.content
        assert f'filename="{name}"'.encode() in req.content
    parts = [multipart_file(r) for r in sent]
    assert [name for name, _ in parts] == expected
    assert parts[1][1] == files[0].read_bytes()
    assert parts[2][1] == files[1].read_bytes()
    assert b"AC1: exports CSV" in parts[0][1]
    assert len(result.attachments) == 3


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


def test_partial_failure_missing_attachment_file_is_refused_before_create(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    # D27 (renamed from ..._carries_issue_key): a missing attempt log is found before any Jira call, so nothing is
    # created and there is no key to carry (was: the issue was created first and the error carried TEST-1).
    mock_happy(router)
    with pytest.raises(MissingAttachments) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[log_file(tmp_path), tmp_path / "missing.log"],
        )
    exc = info.value
    assert isinstance(exc, JiraError) and not isinstance(exc, JiraUnreachable)
    assert exc.issue_key is None
    assert "missing.log" in str(exc)
    assert [p.path.name for p in exc.problems] == ["missing.log"]
    assert not router.calls, "no Jira call at all: not even the issue-type lookup"


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
    # D27: the description is at most 280 characters, holding the reason in one line and a pointer to report.md; the
    # requirement, task, criteria, attempt table and last test output moved to report.md, which is checked here with
    # the same items (was: all of them in the description, as a heading, bullet list, table and code block).
    mock_happy(router)
    jira.escalate(TARGET, issue_type="Bug", body=ticket(reason=reason))
    text = description_of(router)
    assert len(text) <= LIMIT, f"description is {len(text)} characters"
    assert [line for line in text.splitlines() if reason in line], (
        f"the reason {reason!r} must be whole, on one line"
    )
    assert REPORT in text, "the description must point to report.md"
    for detail in [
        "AC1: exports CSV",
        "AC2: header row matches",
        "FAILED tests/test_export.py",
        "csv empty",
        "header wrong",
    ]:
        assert detail not in text, (
            f"{detail!r} belongs in report.md, not the description"
        )
    report = report_md(router)
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
        "timeout",
    ]:
        assert needed in report, f"{needed!r} missing from report.md"
    assert {"AC1: exports CSV", "AC2: header row matches"} <= set(list_items(report))
    rows = table_rows(report)
    assert len(rows) == 1 + 3, "attempt table: a header and one row per attempt"
    for number, rung, outcome, summary in [
        ("1", "R2", "fail", "csv empty"),
        ("2", "R3", "fail", "header wrong"),
        ("3", "R3", "fail", "timeout"),
    ]:
        assert [r for r in rows if {number, rung, outcome, summary} <= set(r)], (
            f"attempt {number} ({rung}, {outcome}) has no row of its own"
        )
    assert [
        b for b in fenced_blocks(report) if "FAILED tests/test_export.py::test_csv" in b
    ], "the last test output must be in a code block"


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


def frames_from_ultron(tb: types.TracebackType | None) -> list[types.FrameType]:
    """A traceback's frames from ultron's outermost frame inward: ultron's own and every frame below them (httpx,
    httpcore, anything ultron called). The frames above are the caller's (here, the test's), which hold the router
    and the client themselves.
    """
    frames: list[types.FrameType] = []
    started = False
    while tb is not None:
        started = started or _in_ultron(tb.tb_frame)
        if started:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    return frames


def reachable_text(root: object) -> list[str]:
    """Every str or bytes value reachable from `root`.

    Followed: exception `__cause__`, `__context__`, `args`, `__notes__`, `__traceback__` and attributes; the locals of
    every traceback frame from ultron's outermost frame inward (attempt 3: library frames below ultron's too, not only
    ultron's own); the frames of generators and coroutines; containers and mappings (httpx.Headers included); object
    `__dict__` and `__slots__`; bound methods' `__self__`; closure cells, defaults and `functools.partial` arguments.
    A JiraClient owns the credentials by design (its httpx client must send them), so it is checked through its repr
    and str only; any other object holding the token is a leak.
    """
    out: list[str] = []
    seen: set[int] = set()
    keep: list[
        object
    ] = []  # holds every visited object, so no id is reused by a temporary during the walk
    todo: list[object] = [root]
    while todo:
        obj = todo.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        keep.append(obj)
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
            todo += [obj.__defaults__, obj.__kwdefaults__]
            continue
        if isinstance(obj, functools.partial):
            todo += [obj.func, obj.args, obj.keywords]
            continue
        if isinstance(obj, types.TracebackType):
            todo += [dict(frame.f_locals) for frame in frames_from_ultron(obj)]
            continue
        if isinstance(
            obj, types.FrameType
        ):  # reached other than through a traceback: its locals, not its callers
            todo.append(dict(obj.f_locals))
            continue
        if isinstance(
            obj, types.GeneratorType | types.CoroutineType | types.AsyncGeneratorType
        ):
            todo.append(
                getattr(obj, "gi_frame", None)
                or getattr(obj, "cr_frame", None)
                or getattr(obj, "ag_frame", None)
            )
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
            slots = vars(cls).get("__slots__", ())
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
    if (method, path) == ("POST", COMMENT):
        return "comment"
    if (method, path) == ("GET", ISSUE):
        return "get"
    if (method, path) == ("DELETE", ISSUE):
        return "delete"
    return None


def _fine_step_of(request: httpx.Request) -> str | None:
    """As `_step_of`, with an upload split into report.md ("attach_report") and any other file ("attach_file")."""
    step = _step_of(request)
    if step == "attach":
        return (
            "attach_report" if multipart_file(request)[0] == REPORT else "attach_file"
        )
    return step


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
        self.fine: list[
            str
        ] = []  # as steps, with uploads split into attach_report and attach_file
        self.bodies: list[bytes] = []  # every request body sent, in order
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.unauthenticated: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        step = _step_of(request)
        if step is None:
            self.unexpected.append(f"{request.method} {request.url.path}")
            return httpx.Response(418)
        fine = _fine_step_of(request) or step
        self.steps.append(step)
        self.fine.append(fine)
        self.bodies.append(request.read())
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Basic {BASIC}":
            self.unauthenticated.append(step)
        if self.fail in {step, fine}:
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
        if step == "comment":
            return httpx.Response(201, json=w["comment"])
        if step == "get":
            return httpx.Response(
                200, json={"id": "10000", "key": "TEST-1", "fields": {}}
            )
        if step == "delete":
            return httpx.Response(204)
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
    # D27: report.md and the log (was: 1, the log only).
    assert len(result.attachments) == 2
    assert [name for name, _ in uploads(router)] == [REPORT, "attempt-1.log"]


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
    # D27: MissingAttachments before any HTTP call, no create and no key (was: one create, then an error with TEST-1).
    mock_happy(router)
    with pytest.raises(JiraError) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[tmp_path / "missing.log"],
        )
    exc = info.value
    assert isinstance(exc, MissingAttachments)
    assert exc.issue_key is None
    assert not isinstance(exc, JiraUnreachable), (
        "a missing local file isn't worth a retry"
    )
    assert "missing.log" in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert not [c for c in router.calls if c.request.url.path == CREATE]
    assert not [c for c in router.calls if c.request.url.path == ATTACH]
    assert not router.calls, (
        "nothing at all is sent before the files are known to exist"
    )
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
    # D27: happy and kanban each upload report.md and then the log, so two attach replies (was: one).
    scripts = {
        "happy": [
            *start,
            wire(200, w["attachments"]),
            wire(200, w["attachments"]),
            wire(200, s["active"]),
            wire(204),
        ],
        "kanban": [
            *start,
            wire(200, w["attachments"]),
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
        # D27: both uploads really went over the wire, report.md first.
        written = bytes(backend.written)
        assert written.count(b"POST /rest/api/3/issue/TEST-1/attachments ") == 2
        assert (
            0
            <= written.find(b'filename="report.md"')
            < written.find(b'filename="attempt-1.log"')
        )
        assert len(outcome.attachments) == 2
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


# ==== Attempt 3 (R5): D27 (AC9, AC10), and AC4 again ====
#
# AC4's threat model, as in attempts 1-2: the sentinel token enters through the environment only, and Jira is hostile
# in every failure. Send errors hold the request and quote its header; error replies (any status outside 2xx, 3xx
# included) and 2xx bodies that can't be parsed quote the header and the token. A 2xx JSON reply is Jira's data and is
# trusted. The walker (reachable_text) now follows every frame from ultron's outermost frame inward, library frames
# included, and assert_clean also looks for pieces of the secret.

FINE_STEPS = (
    "types",
    "create",
    "attach_report",
    "attach_file",
    "sprint_active",
    "sprint_future",
    "move",
)
FINE_AFTER_CREATE = frozenset(FINE_STEPS[2:])


def frames_with_locals(exc: BaseException) -> str:
    """The traceback from ultron's outermost frame inward, with every frame's locals, as a crash reporter or pytest's
    --showlocals would print it. The caller's frames above are left out: what they hold is the caller's own."""
    tb = exc.__traceback__
    entries: list[tuple[types.FrameType, int]] = []
    started = False
    while tb is not None:
        started = started or _in_ultron(tb.tb_frame)
        if started:
            entries.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    return "".join(
        traceback.StackSummary.extract(iter(entries), capture_locals=True).format()
    )


def assert_raised_clean(exc: BaseException) -> None:
    """Everything an error the client raised carries: text, args, attributes, chain, frames and their locals."""
    assert_clean(str(exc), "str(exception)")
    assert_clean(repr(exc), "repr(exception)")
    assert_clean(repr(exc.args), "exception args")
    assert_clean(repr(vars(exc)), "exception attributes")
    assert_clean("".join(traceback.format_exception(exc)), "formatted traceback")
    assert_clean(frames_with_locals(exc), "traceback with locals")
    assert_graph_clean(exc)


def raising(
    make: Callable[[httpx.Request], httpx.Response | BaseException],
) -> Callable[[httpx.Request], httpx.Response]:
    """A FakeJira reply that raises what `make` gives when it's an exception."""

    def reply(request: httpx.Request) -> httpx.Response:
        out = make(request)
        if isinstance(out, BaseException):
            raise out
        return out

    return reply


def test_a3_walker_sees_every_frame_below_ultron_but_not_the_callers(
    jira: JiraClient, tmp_path: Path
) -> None:
    # The stronger walker must find what it is meant to find, or the attempt-3 tests prove nothing.
    # httpx's own frames below ultron's hold the request: Ctrl+C during a send unwinds through them.
    fake = FakeJira(fail="types", error=lambda r: KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt) as interrupted:
        escalate_with(fake, tmp_path)
    with pytest.raises(AssertionError):
        assert_graph_clean(interrupted.value)

    # A generator's frame, a partial's arguments and a function's defaults.
    def holds_header() -> Iterator[int]:
        header = f"Basic {BASIC}"
        yield len(header)

    suspended = holds_header()
    next(suspended)

    def with_default(token: str = TOKEN) -> str:
        return token

    for holder in (suspended, functools.partial(len, TOKEN), with_default):
        with pytest.raises(AssertionError):
            assert_graph_clean(ValueError(holder))

    # Part of a secret, as a message cut at a length limit could keep it.
    with pytest.raises(AssertionError, match="part of the token"):
        assert_clean(f"... {TOKEN[:18]}", "a cut message")
    with pytest.raises(AssertionError, match="part of the Basic-auth token"):
        assert_clean(f"Basic {BASIC[:52]}", "a cut message")

    # Not the caller's frames above ultron's: what they hold is the caller's own.
    def caller(header: str) -> None:
        jira._request("not-an-endpoint")

    with pytest.raises(JiraError) as above:
        caller(f"Basic {BASIC}")
    assert_raised_clean(above.value)


@pytest.mark.parametrize("step", FINE_STEPS)
def test_a3_ctrl_c_during_a_send_is_not_swallowed(step: str, tmp_path: Path) -> None:
    # Ctrl+C must stop ultron: not become a JiraError, nor a JiraUnreachable that T26 would retry later. (No client
    # can keep the token out of the frames an interrupt unwinds through; that residual risk is the developer's note.)
    fake = FakeJira(fail=step, error=lambda r: KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt) as info:
        escalate_with(fake, tmp_path)
    assert type(info.value) is KeyboardInterrupt
    assert fake.fine[-1] == step, "nothing more is sent after the interrupt"
    assert_clean("".join(traceback.format_exception(info.value)), "printed traceback")


# ---- AC9: the description is at most 280 characters; the details go in an attached report.md ----

WORDS = "the export writes an empty file when the header row is missing; "


def reason_of(length: int) -> str:
    """A reason of about `length` characters that starts with a marker found nowhere else in a ticket."""
    return ("Zq reason: " + WORDS * (length // len(WORDS) + 2))[:length].rstrip()


@pytest.mark.parametrize("fallback", [False, True], ids=["story", "feature-fallback"])
@pytest.mark.parametrize(
    "length", [12, 40, 100, 180, 195, 200, 201, 210, 250, 279, 280, 281, 400, 5000]
)
def test_ac9_description_is_at_most_280_characters(
    router: respx.MockRouter, jira: JiraClient, length: int, fallback: bool
) -> None:
    mock_happy(router)
    reason = reason_of(length)
    jira.escalate(
        TARGET,
        issue_type="Feature" if fallback else "Story",
        body=ticket(reason=reason),
    )
    text = description_of(router)
    assert len(text) <= LIMIT, f"description is {len(text)} characters"
    lines = text.splitlines()
    reason_lines = [line for line in lines if "Zq reason" in line]
    assert len(reason_lines) == 1, "the reason in one line"
    if len(reason) <= 100:
        assert reason in reason_lines[0], "a short reason is there whole"
    assert [line for line in lines if REPORT in line], "a pointer to report.md"
    if fallback:
        assert "Feature" in lines[0] and "Story" in lines[0], (
            "AC1's note still comes first"
        )
    assert squash(reason) in squash(report_md(router)), "report.md has the whole reason"


def test_ac9_built_body_sweep() -> None:
    # verify_with: a length check on the built body, for every reason length up to 600 characters, with no note, the
    # fallback note, a note far too long and several notes, and reasons with line breaks or characters beyond ASCII.
    notes_cases: list[tuple[str, ...]] = [
        (),
        ("filed as a Story because project TEST has no Feature type.",),
        ("n " * 600,),
        ("first\nnote", "second note"),
    ]
    makers: list[Callable[[int], str]] = [
        reason_of,
        lambda n: ("line\n" * n)[:n].strip() or "x",
        lambda n: "é" * n,
        lambda n: "\U0001f44d" * n,
    ]
    for notes in notes_cases:
        for make in makers:
            for length in range(1, 601):
                text = adf_plain(to_adf(ticket(reason=make(length)), notes))
                assert len(text) <= LIMIT, (notes[:1], length, len(text))
                assert REPORT in text, (notes[:1], length)


def test_ac9_reason_with_line_breaks_stays_on_one_line(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)
    reason = "stopped:\nowner stop\r\n\tafter attempt 2"
    jira.escalate(TARGET, issue_type="Bug", body=ticket(reason=reason))
    text = description_of(router)
    assert [line for line in text.splitlines() if squash(reason) in line]


def test_ac9_report_md_has_the_details_whole(
    router: respx.MockRouter, jira: JiraClient
) -> None:
    mock_happy(router)
    reason = reason_of(3000)
    output = (
        "collected 50 items\n"
        + "".join(
            f"FAILED tests/test_{i}.py::test_x - AssertionError\n" for i in range(50)
        )
        + "===== 50 failed in 2.31s ====="
    )
    jira.escalate(
        TARGET,
        issue_type="Bug",
        body=ticket(
            reason=reason,
            last_test_output=output,
            requirement_title="CSV export for monthly reports",
        ),
    )
    text = description_of(router)
    assert len(text) <= LIMIT
    assert "FAILED tests/test_1.py" not in text
    report = report_md(router)
    assert squash(reason) in squash(report), "the whole reason, not the cut one"
    assert "CSV export for monthly reports" in report
    assert [b for b in fenced_blocks(report) if output in b], (
        "the last test output, whole and with its line breaks, in a code block"
    )


# ---- AC10: attachments checked before any Jira call; comments of at most 280 characters ----


@contextlib.contextmanager
def unreadable(path: Path) -> Iterator[None]:
    """`path` stays a file but can't be read: an exclusive handle on Windows, mode 000 elsewhere.

    Real file-system state, not a patched function, so whatever the client reads with must fail. (As root on POSIX,
    mode 000 doesn't stop reading; the check below then fails loudly instead of testing nothing.)
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        # GENERIC_READ, no sharing, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
        assert handle not in (None, wintypes.HANDLE(-1).value), (
            f"CreateFileW failed ({ctypes.get_last_error()})"
        )

        def release() -> None:
            kernel32.CloseHandle(handle)

    else:
        path.chmod(0)

        def release() -> None:
            path.chmod(0o600)

    try:
        with pytest.raises(OSError):
            path.read_bytes()
        yield
    finally:
        release()


def problem_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A good file, a missing one, a folder and a file to make unreadable."""
    good = log_file(tmp_path, "attempt-1.log")
    missing = tmp_path / "attempt-2.log"
    folder = tmp_path / "attempt-3.log"
    folder.mkdir()
    locked = log_file(tmp_path, "plan.md")
    return good, missing, folder, locked


def test_ac10_check_attachments_reports_every_problem_before_any_call(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    good, missing, folder, locked = problem_files(tmp_path)
    paths = [good, missing, folder, locked]
    with unreadable(locked):
        problems = check_attachments(paths)
        again = JiraClient.check_attachments([str(p) for p in paths])
    assert [p.path for p in problems] == [missing, folder, locked]
    assert [p.path for p in again] == [missing, folder, locked]
    reasons = {p.path.name: p.reason for p in problems}
    assert "missing" in reasons["attempt-2.log"]
    assert "unreadable" in reasons["plan.md"]
    assert reasons["attempt-3.log"], "a folder is no file to attach"
    assert not router.calls


def test_ac10_escalate_refuses_naming_every_problem_and_creates_nothing(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_happy(router)
    good, missing, folder, locked = problem_files(tmp_path)
    with unreadable(locked), pytest.raises(MissingAttachments) as info:
        jira.escalate(
            TARGET,
            issue_type="Bug",
            body=ticket(),
            attachments=[good, missing, folder, locked],
        )
    exc = info.value
    assert isinstance(exc, JiraError) and not isinstance(exc, JiraUnreachable)
    assert exc.issue_key is None
    assert [p.path for p in exc.problems] == [missing, folder, locked]
    for path in (missing, folder, locked):
        assert path.name in str(exc), f"{path.name} isn't named"
    assert not router.calls, "nothing is created: no Jira call at all"
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)


def test_ac10_a_file_gone_after_the_check_never_leaves_an_unknown_issue(
    tmp_path: Path,
) -> None:
    # If a log disappears between the check and its upload, the issue exists: the caller must learn its key.
    log = log_file(tmp_path)

    def vanish(request: httpx.Request) -> httpx.Response:
        log.unlink()
        return httpx.Response(200, json=fixture("issue_write.json")["attachments"])

    fake = FakeJira(fail="attach_report", reply=vanish)
    with (
        JiraClient(
            JiraCredentials.from_env(env()), transport=httpx.MockTransport(fake)
        ) as c,
        pytest.raises(JiraError) as info,
    ):
        c.escalate(TARGET, issue_type="Bug", body=ticket(), attachments=[log])
    exc = info.value
    fake.check()
    assert exc.issue_key == "TEST-1"
    assert not isinstance(exc, JiraUnreachable)
    assert fake.steps.count("create") == 1
    assert log.name in str(exc)
    assert_raised_clean(exc)


def mock_comment(router: respx.MockRouter) -> respx.Route:
    return router.post(COMMENT).mock(
        return_value=httpx.Response(201, json=fixture("issue_write.json")["comment"])
    )


def comment_text(route: respx.Route) -> str:
    body = json.loads(route.calls.last.request.content)["body"]
    return adf_plain(body) if isinstance(body, dict) else str(body)


@pytest.mark.parametrize("length", [1, 100, 279, 280, 281, 1000, 20_000])
def test_ac10_comment_is_at_most_280_characters(
    router: respx.MockRouter, jira: JiraClient, length: int
) -> None:
    route = mock_comment(router)
    text = (
        "Zq answer: " + "the header row comes from the template in export.py; " * 400
    )[:length].rstrip() or "Z"
    result = jira.comment("TEST-1", text)
    assert route.call_count == 1
    assert len(router.calls) == 1, "no attachment was asked for"
    posted = comment_text(route)
    assert 0 < len(posted) <= LIMIT, f"comment is {len(posted)} characters"
    if len(text) <= LIMIT:
        assert posted == text
    else:
        assert posted.startswith(text[:200]), "the comment keeps the start of the text"
    assert isinstance(result, CommentResult)
    assert result.issue_key == "TEST-1"
    assert result.text == posted, "the event records what was posted"
    json.dumps(result.to_dict())


def test_ac10_comment_attaches_a_file_to_the_existing_issue(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    upload = router.post(ATTACH).mock(
        return_value=httpx.Response(
            200, json=fixture("issue_write.json")["attachments"]
        )
    )
    route = mock_comment(router)
    answer = tmp_path / "answer.md"
    answer.write_text("The header comes from the template.\n", encoding="utf-8")
    result = jira.comment("TEST-1", "Answer attached.", attachment=answer)
    assert upload.call_count == 1 and route.call_count == 1
    assert len(router.calls) == 2, "an upload and a comment; nothing is created"
    sent = upload.calls.last.request
    assert sent.headers["X-Atlassian-Token"] == "no-check"
    assert multipart_file(sent) == ("answer.md", answer.read_bytes())
    assert comment_text(route) == "Answer attached."
    for (
        call
    ) in router.calls:  # AC3: comment and attach are allowlisted, not sprint calls
        assert match_endpoint(call.request.method, call.request.url.path) is not None
    assert result.attachment is not None
    json.dumps(result.to_dict())


def test_ac10_comment_with_a_missing_attachment_sends_nothing(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    mock_comment(router)
    with pytest.raises(MissingAttachments) as info:
        jira.comment("TEST-1", "see the file", attachment=tmp_path / "answer.md")
    assert not router.calls
    assert "answer.md" in str(info.value)
    assert not isinstance(info.value, JiraUnreachable)


def test_ac10_attach_to_an_existing_issue(
    router: respx.MockRouter, jira: JiraClient, tmp_path: Path
) -> None:
    upload = router.post(ATTACH).mock(
        return_value=httpx.Response(
            200, json=fixture("issue_write.json")["attachments"]
        )
    )
    log = log_file(tmp_path)
    jira.attach("TEST-1", log)
    assert upload.call_count == 1
    assert upload.calls.last.request.headers["X-Atlassian-Token"] == "no-check"
    assert multipart_file(upload.calls.last.request) == (log.name, log.read_bytes())
    with pytest.raises(MissingAttachments):
        jira.attach("TEST-1", tmp_path / "gone.log")
    assert upload.call_count == 1, "a missing file sends nothing"


# ---- AC4 again: every step and failure kind, with the walker following every frame below ultron's ----


def hostile_status(status: int, kind: str) -> Callable[[httpx.Request], httpx.Response]:
    """An error reply that quotes the request's Authorization header and the token, in its headers and its body.

    "long-token" and "long-basic" put a secret across the 500-character cut of Jira's error text: masking after the
    cut would leave a piece of it.
    """

    def reply(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"]
        headers = {
            "WWW-Authenticate": auth,
            "Location": f"{BASE}/login?auth={auth}&token={TOKEN}",
        }
        if kind == "json":
            body = {
                "errorMessages": [f"jira says {status}", f"you sent {auth}"],
                "errors": {"token": TOKEN, auth: "bad"},
            }
            return httpx.Response(status, headers=headers, json=body)
        if kind == "html":
            page = f"<html><h1>{status}</h1><pre>Authorization: {auth}\ntoken={TOKEN}</pre></html>"
            return httpx.Response(status, headers=headers, text=page)
        if kind == "long-token":
            return httpx.Response(
                status, headers=headers, text="x" * 482 + TOKEN + "y" * 99
            )
        return httpx.Response(
            status, headers=headers, text="x" * 440 + f"Basic {BASIC}" + "y" * 99
        )

    return reply


A3_STATUSES = [301, 302, 307, 400, 401, 403, 404, 409, 413, 429, 500, 502, 503]
A3_STATUS_CASES = [(s, k) for s in A3_STATUSES for k in ("json", "html")] + [
    (s, k) for s in (302, 400, 500) for k in ("long-token", "long-basic")
]


@pytest.mark.parametrize("step", FINE_STEPS)
@pytest.mark.parametrize(("status", "kind"), A3_STATUS_CASES)
def test_a3_hostile_error_reply_at_every_step(
    step: str, status: int, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=step, reply=hostile_status(status, kind))
    if status == 400 and step in {"sprint_active", "sprint_future"}:
        # A board without sprints: the backlog, with Jira's reason recorded in the event, masked.
        result = escalate_with(fake, tmp_path)
        fake.check()
        assert result.sprint.kind == "backlog" and result.sprint.reason
        assert_graph_clean(result, "result")
        assert_clean(json.dumps(result.to_dict()), "result.to_dict()")
        assert_clean(result.sprint.describe(), "describe()")
        assert_logs_clean(caplog)
        return
    with pytest.raises(JiraError) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert exc.status == status
    assert not isinstance(exc, JiraUnreachable)
    assert fake.fine[-1] == step, "the escalation stops at the failing step"
    assert fake.steps.count("create") == (0 if step == "types" else 1)
    assert exc.issue_key == ("TEST-1" if step in FINE_AFTER_CREATE else None)
    if kind == "json":
        assert f"jira says {status}" in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


class _Leaky(Exception):
    """A send error from outside httpx that holds the request and quotes its header and the token."""

    def __init__(self, request: httpx.Request) -> None:
        auth = request.headers["Authorization"]
        super().__init__(f"send failed: Authorization: {auth} token={TOKEN}", request)
        self.request = request


A3_SEND_ERRORS: dict[str, Callable[[httpx.Request], BaseException]] = {
    "outside-httpx": _Leaky,
    "RuntimeError": lambda r: RuntimeError(f"boom {r.headers['Authorization']}", r),
    "ValueError": lambda r: ValueError(TOKEN, r.headers),
    "OSError": lambda r: OSError(13, f"denied {r.headers['Authorization']}"),
    "UnicodeEncodeError": lambda r: UnicodeEncodeError(
        "ascii", r.headers["Authorization"] + TOKEN, 0, 1, "not ascii"
    ),
    "RecursionError": lambda r: RecursionError(f"too deep {TOKEN}"),
    "InvalidURL": lambda r: httpx.InvalidURL(f"bad {r.headers['Authorization']}"),
    "CookieConflict": lambda r: httpx.CookieConflict(f"session={TOKEN}"),
}


@pytest.mark.parametrize("step", FINE_STEPS)
@pytest.mark.parametrize("error", list(A3_SEND_ERRORS))
def test_a3_hostile_send_errors_outside_httperror_at_every_step(
    step: str, error: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=step, error=A3_SEND_ERRORS[error])
    with pytest.raises(Exception) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert fake.fine[-1] == step
    if step in FINE_AFTER_CREATE:
        assert isinstance(exc, JiraError) and exc.issue_key == "TEST-1"
        assert fake.steps.count("create") == 1
    else:
        assert getattr(exc, "issue_key", None) is None
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("error", HTTPX_ERRORS, ids=lambda c: c.__name__)
def test_a3_every_httpx_error_at_the_file_upload(
    error: type[httpx.HTTPError], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The attempt-2 matrix fails the first upload, which is now report.md; this one fails the log after it.
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail="attach_file", error=lambda r: hostile_error(error, r))
    with pytest.raises(JiraError) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert fake.fine[-2:] == ["attach_report", "attach_file"]
    assert isinstance(exc, JiraUnreachable) is issubclass(error, httpx.TransportError)
    assert exc.issue_key == "TEST-1"
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


A3_UNPARSEABLE: dict[str, Callable[[httpx.Request], httpx.Response]] = {
    "html": lambda r: httpx.Response(
        200, text=f"<html>Authorization: {r.headers['Authorization']} {TOKEN}</html>"
    ),
    "not-utf8": lambda r: httpx.Response(200, content=b"\xff\xfe\x00" + TOKEN.encode()),
    "cut-json": lambda r: httpx.Response(
        201, content=b'{"key": "TEST-1", "note": "' + TOKEN.encode()
    ),
    "too-deep": lambda r: httpx.Response(200, content=b"[" * 50_000 + b"]" * 50_000),
}


@pytest.mark.parametrize("step", FINE_STEPS)
@pytest.mark.parametrize("body", list(A3_UNPARSEABLE))
def test_a3_unparseable_2xx_body_at_every_step(
    step: str, body: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=step, reply=A3_UNPARSEABLE[body])
    with pytest.raises(Exception) as info:
        escalate_with(fake, tmp_path)
    exc = info.value
    fake.check()
    assert fake.fine[-1] == step
    if step in FINE_AFTER_CREATE:
        assert isinstance(exc, JiraError) and exc.issue_key == "TEST-1"
        assert not isinstance(exc, JiraUnreachable)
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


A3_SHAPES: list[tuple[str, Any]] = [
    ("types", []),
    ("types", {"issueTypes": "Bug"}),
    ("types", {"issueTypes": [{"name": "Bug"}]}),
    ("create", {}),
    ("create", []),
    ("create", {"id": "10000"}),
    ("attach_report", {}),
    ("attach_report", "stored"),
    ("attach_file", [1, 2]),
    ("sprint_active", []),
    ("sprint_active", {"values": [{"name": "S"}]}),
    ("sprint_active", {"values": [{"id": "seven"}]}),
    ("sprint_active", {"values": "S"}),
    ("sprint_future", {"values": [{"name": "S"}]}),
    ("move", {"done": True}),
    ("move", []),
]


@pytest.mark.parametrize(("step", "data"), A3_SHAPES)
def test_a3_unexpected_2xx_shape_at_every_step(
    step: str, data: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Jira's 2xx data is trusted (no token in it), but a shape the client doesn't expect must still reach no token,
    # and after create any failure must still carry the key.
    caplog.set_level(logging.DEBUG)
    status = 201 if step == "create" else 200
    fake = FakeJira(fail=step, reply=lambda r: httpx.Response(status, json=data))
    outcome: Any
    try:
        outcome = escalate_with(fake, tmp_path)
    except Exception as exc:  # noqa: BLE001 - any type: the check is what it carries
        outcome = exc
    fake.check()
    if isinstance(outcome, BaseException):
        if step in FINE_AFTER_CREATE:
            assert isinstance(outcome, JiraError) and outcome.issue_key == "TEST-1"
            assert outcome.__cause__ is None and outcome.__context__ is None
        assert_raised_clean(outcome)
    else:
        assert_graph_clean(outcome, "result")
    assert_logs_clean(caplog)


def _public_calls(tmp_path: Path) -> dict[str, tuple[str, Callable[[JiraClient], Any]]]:
    log = log_file(tmp_path)
    return {
        "issue_types": ("types", lambda c: c.issue_types("TEST")),
        "resolve_issue_type": (
            "types",
            lambda c: c.resolve_issue_type("TEST", "Feature"),
        ),
        "create_issue": (
            "create",
            lambda c: c.create_issue(
                project="TEST",
                issue_type_id="10017",
                summary="[ultron] R3/T5 Add the export button",
                description=to_adf(ticket()),
                epic="TEST-100",
            ),
        ),
        "get_issue": ("get", lambda c: c.get_issue("TEST-1", ["summary"])),
        "delete_issue": ("delete", lambda c: c.delete_issue("TEST-1")),
        "attach": ("attach_file", lambda c: c.attach("TEST-1", log)),
        "comment": ("comment", lambda c: c.comment("TEST-1", "the answer is attached")),
        "comment_upload": (
            "attach_file",
            lambda c: c.comment("TEST-1", "the answer is attached", attachment=log),
        ),
        "choose_sprint": ("sprint_active", lambda c: c.choose_sprint(1)),
        "move_to_sprint": ("move", lambda c: c.move_to_sprint(7, ["TEST-1"])),
    }


PUBLIC_CALLS = [
    "issue_types",
    "resolve_issue_type",
    "create_issue",
    "get_issue",
    "delete_issue",
    "attach",
    "comment",
    "comment_upload",
    "choose_sprint",
    "move_to_sprint",
]
A3_FAILURES: dict[str, Callable[[httpx.Request], httpx.Response | BaseException]] = {
    "ConnectError": lambda r: hostile_error(httpx.ConnectError, r),
    "ReadTimeout": lambda r: hostile_error(httpx.ReadTimeout, r),
    "DecodingError": lambda r: hostile_error(httpx.DecodingError, r),
    "outside-httpx": _Leaky,
    "500": hostile_status(500, "json"),
    "302": hostile_status(302, "html"),
    "200-not-json": A3_UNPARSEABLE["html"],
}


@pytest.mark.parametrize("failure", list(A3_FAILURES))
@pytest.mark.parametrize("call", PUBLIC_CALLS)
def test_a3_public_method_errors_reach_no_token(
    call: str, failure: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    step, run = _public_calls(tmp_path)[call]
    fake = FakeJira(fail=step, reply=raising(A3_FAILURES[failure]))
    with (
        JiraClient(
            JiraCredentials.from_env(env()), transport=httpx.MockTransport(fake)
        ) as c,
        pytest.raises(JiraError) as info,
    ):
        run(c)
    exc = info.value
    fake.check()
    assert fake.fine[-1] == step
    assert isinstance(exc, JiraUnreachable) is (
        failure in {"ConnectError", "ReadTimeout"}
    )
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("at", ["attach_file", "comment"])
@pytest.mark.parametrize("error", HTTPX_ERRORS, ids=lambda c: c.__name__)
def test_a3_every_httpx_error_in_a_comment(
    error: type[httpx.HTTPError],
    at: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeJira(fail=at, error=lambda r: hostile_error(error, r))
    with (
        JiraClient(
            JiraCredentials.from_env(env()), transport=httpx.MockTransport(fake)
        ) as c,
        pytest.raises(JiraError) as info,
    ):
        c.comment("TEST-1", "the answer is attached", attachment=log_file(tmp_path))
    exc = info.value
    fake.check()
    assert fake.fine[-1] == at
    assert isinstance(exc, JiraUnreachable) is issubclass(error, httpx.TransportError)
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


# ---- AC4 again: configuration errors (from_env, JIRA_BASE_URL, proxies), with the token in os.environ ----

A3_BAD_URLS = {
    "http": "http://example.atlassian.net",
    "no-scheme": "example.atlassian.net",
    "ftp": "ftp://example.atlassian.net",
    "empty-host": "https://",
    "no-host": "https:///rest",
    "bad-ipv6": "https://[::1",
    "nul": "https://exa\x00mple.atlassian.net",
    "user-password": "https://someone:pw@example.atlassian.net",
    "token-in-url": f"https://{EMAIL}:{TOKEN}@example.atlassian.net",
    "token-as-user": f"https://{TOKEN}@example.atlassian.net",
}
A3_CONFIG_CASES = [
    (name, source, entry)
    for name in A3_BAD_URLS
    for source in ("mapping", "os.environ")
    for entry in ("credentials", "client")
    if not (name == "nul" and source == "os.environ")  # os.environ can't hold a NUL
]


@pytest.mark.parametrize(("url", "source", "entry"), A3_CONFIG_CASES)
def test_a3_bad_base_url_is_refused_naming_it_and_reaches_no_token(
    url: str,
    source: str,
    entry: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    for k, v in env().items():
        monkeypatch.setenv(k, v)  # the token is in os.environ either way
    if source == "os.environ":
        monkeypatch.setenv("JIRA_BASE_URL", A3_BAD_URLS[url])
    build = JiraCredentials.from_env if entry == "credentials" else JiraClient.from_env
    with pytest.raises(JiraConfigError) as info:
        if source == "mapping":
            build(env(JIRA_BASE_URL=A3_BAD_URLS[url]))
        else:
            build()
    exc = info.value
    assert "JIRA_BASE_URL" in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("name", ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_a3_missing_variable_is_named_through_the_client_and_reaches_no_token(
    name: str, value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in env().items():
        monkeypatch.setenv(k, v)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(JiraConfigError, match=name) as info:
        JiraClient.from_env()
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert_raised_clean(info.value)


A3_BAD_PROXIES = {
    "malformed": ("HTTPS_PROXY", f"::bad::{TOKEN}"),
    "scheme": ("HTTPS_PROXY", "ftp://proxy.example.com"),
    "all-with-secret": ("ALL_PROXY", f"ftp://{EMAIL}:{TOKEN}@proxy.example.com"),
}


@pytest.mark.parametrize("entry", ["init", "from_env"])
@pytest.mark.parametrize("proxy", list(A3_BAD_PROXIES))
def test_a3_bad_proxy_setting_is_refused_and_reaches_no_token(
    proxy: str,
    entry: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    for k, v in env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(*A3_BAD_PROXIES[proxy])
    with pytest.raises(JiraConfigError) as info:
        if entry == "init":
            JiraClient(JiraCredentials.from_env())
        else:
            JiraClient.from_env()
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert_raised_clean(exc)
    assert_logs_clean(caplog)


def test_a3_client_refuses_credentials_it_cannot_use() -> None:
    usable = JiraCredentials.from_env(env())
    for bad in (
        dataclasses.replace(usable, base_url="https://[::1"),  # past from_env's checks
        JiraCredentials.from_env(
            env(JIRA_API_TOKEN=TOKEN + "\udcff")
        ),  # can't be encoded
    ):
        with pytest.raises(JiraConfigError) as info:
            JiraClient(bad)
        assert info.value.__cause__ is None and info.value.__context__ is None
        assert_raised_clean(info.value)


# ---- Masking (developer's claim, checked here): the token in the caller's content is never sent or recorded ----


def secret_ticket() -> TicketBody:
    leak = f"JIRA_API_TOKEN={TOKEN} Authorization: Basic {BASIC}"
    return ticket(
        title=f"Add the export button {TOKEN}",
        reason=f"all attempts failed; the test printed {leak}",
        acceptance_criteria=[f"AC1: exports CSV ({leak})", "AC2: header row matches"],
        last_test_output=f"FAILED tests/test_export.py::test_csv\nenv: {leak}\n",
        attempts=[
            AttemptRow(1, "R2", "fail", f"csv empty {leak}"),
            AttemptRow(2, f"R3 {TOKEN}", f"fail {TOKEN}", "header wrong"),
        ],
        requirement_title=f"CSV export {TOKEN}",
    )


def secret_log(tmp_path: Path) -> Path:
    log = tmp_path / "attempt-1.log"
    log.write_bytes(
        f"$ env\nJIRA_API_TOKEN={TOKEN}\nAuthorization: Basic {BASIC}\nFAILED x\n".encode()
    )
    return log


def test_a3_the_token_in_the_callers_content_is_never_sent_or_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    log = secret_log(tmp_path)
    fake = FakeJira()
    with JiraClient(
        JiraCredentials.from_env(env()), transport=httpx.MockTransport(fake)
    ) as c:
        result = c.escalate(
            TARGET, issue_type="Feature", body=secret_ticket(), attachments=[log]
        )
        note = c.comment(
            "TEST-1",
            f"answer: the run printed {TOKEN} and Basic {BASIC}",
            attachment=log,
        )
    fake.check()
    assert fake.fine == [
        "types",
        "create",
        "attach_report",
        "attach_file",
        "sprint_active",
        "move",
        "attach_file",
        "comment",
    ]
    for step, sent in zip(fake.fine, fake.bodies, strict=True):
        assert_clean(sent.decode("latin-1"), f"what was sent at {step}")
    # Sent, but masked: the content itself still went.
    fields = json.loads(fake.bodies[1])["fields"]
    assert "***" in fields["summary"]
    description = adf_plain(fields["description"])
    assert "***" in description and len(description) <= LIMIT
    report = multipart_file(fake.requests[2])[1].decode()
    assert "FAILED tests/test_export.py::test_csv" in report and "***" in report
    assert "JIRA_API_TOKEN=***" in multipart_file(fake.requests[3])[1].decode()
    assert "***" in adf_plain(json.loads(fake.bodies[7])["body"])
    # The events and the logs.
    assert_graph_clean(result, "result")
    assert_graph_clean(note, "comment result")
    assert_clean(json.dumps(result.to_dict()) + json.dumps(note.to_dict()), "events")
    assert_logs_clean(caplog)


@pytest.mark.parametrize("offset", range(96, 262, 8))
def test_a3_a_secret_across_the_reasons_cut_leaves_no_piece(
    router: respx.MockRouter, jira: JiraClient, offset: int
) -> None:
    # The reason is cut to fit 280 characters. Masked first, no piece of the token is left wherever the cut falls.
    mock_happy(router)
    reason = "x" * offset + TOKEN + " " + f"Basic {BASIC}" + " y" * 200
    jira.escalate(TARGET, issue_type="Bug", body=ticket(reason=reason))
    text = description_of(router)
    assert len(text) <= LIMIT
    assert_clean(text, "description")
    assert_clean(report_md(router), "report.md")


@pytest.mark.parametrize("offset", range(230, 290, 6))
def test_a3_a_secret_across_the_comments_cut_leaves_no_piece(
    router: respx.MockRouter, jira: JiraClient, offset: int
) -> None:
    route = mock_comment(router)
    result = jira.comment("TEST-1", "x" * offset + TOKEN + " y" * 200)
    posted = comment_text(route)
    assert len(posted) <= LIMIT
    assert_clean(posted, "comment")
    assert_graph_clean(result, "comment result")


def test_a3_real_httpcore_wire_and_logs_with_secrets_in_the_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Through the real httpx -> httpcore -> h11 path: the token goes over the wire only inside the Authorization
    # header (base64), once per request, and never in a body; DEBUG logs of every library stay clean.
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "ultron"):
        caplog.set_level(logging.DEBUG, logger=name)
    w, s = fixture("issue_write.json"), fixture("sprints.json")
    backend = _RecordingBackend(
        [
            wire(200, fixture("issuetypes_ak.json")["response"]),
            wire(201, w["create"]),
            wire(200, w["attachments"]),
            wire(200, w["attachments"]),
            wire(200, s["active"]),
            wire(204),
            wire(200, w["attachments"]),
            wire(201, w["comment"]),
        ]
    )
    log = secret_log(tmp_path)
    with real_path_client(backend) as c:
        result = c.escalate(
            TARGET, issue_type="Feature", body=secret_ticket(), attachments=[log]
        )
        note = c.comment("TEST-1", f"answer: the run printed {TOKEN}", attachment=log)
    written = bytes(backend.written)
    requests = written.count(b" HTTP/1.1\r\n")
    assert requests == 8
    assert written.count(f"Basic {BASIC}".encode()) == requests, (
        "the header, once per request"
    )
    assert_clean(
        written.replace(f"Basic {BASIC}".encode(), b"").decode("latin-1"),
        "the wire, Authorization headers aside",
    )
    assert written.count(b"***") >= 4, "the content went, masked"
    assert_logs_clean(caplog)
    assert any(r.name.startswith("httpcore") for r in caplog.records)
    assert_graph_clean(result, "result")
    assert_graph_clean(note, "comment result")
    assert_clean(json.dumps(result.to_dict()) + json.dumps(note.to_dict()), "events")
