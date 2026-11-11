"""A small Jira Cloud REST client for escalation tickets (spec §12, D5, D27).

Credentials come only from the environment (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) and are never written to
files, logs, exceptions or results. The client checks the attachments before any call, creates an issue under the
configured epic with a description of at most 280 characters, attaches `report.md` and the files, and moves the issue
into the board's active sprint (else the first future sprint, else it stays in the backlog). It can also comment on an
existing issue. It never creates, starts, closes or edits a sprint: every call goes through `ENDPOINTS`, which has no
such endpoint.

No exception the client raises can reach the token. httpx's client, auth, request and response objects all carry the
Authorization header, so they, the token and `os.environ` live only in frames that always return (`_read_env`,
`_http_client`, `_exchange`): no traceback ever holds those frames. Errors are raised after any `except` block, from
frames that hold plain values only, so they have no cause or context either.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx

from ultron.config.models import Jira
from ultron.jira.body import (
    MAX_COMMENT_CHARS,
    REPORT_NAME,
    TicketBody,
    cut,
    one_line,
    render_report,
    short_doc,
    ticket_summary,
    to_adf,
)

log = logging.getLogger(__name__)

ENV_VARS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")

IssueTypeName = Literal["Story", "Feature", "Bug"]
SprintKind = Literal["active", "future", "backlog"]

FALLBACK_TYPE = "Story"
DEFAULT_LABEL = "ultron"
PAGE_SIZE = 50
MAX_ERROR_TEXT = 500


class JiraConfigError(Exception):
    """Jira isn't configured: a missing environment variable or `[jira]` setting. The message names it."""


class JiraError(Exception):
    """A Jira call failed. Carries the method, path and status, never credentials or headers.

    `issue_key` is set when the issue was already created before a later step failed, so the caller doesn't file it
    twice.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        path: str = "",
        status: int | None = None,
        issue_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.status = status
        self.issue_key = issue_key


class JiraUnreachable(JiraError):
    """Jira couldn't be reached (connection, TLS or timeout). The escalation should be retried later."""


@dataclass(frozen=True)
class AttachmentProblem:
    """A file that can't be attached, and why: "missing", "not a file" or "unreadable: <error type>"."""

    path: Path
    reason: str


class MissingAttachments(JiraError):
    """Files missing or unreadable, found before anything was sent (spec §12, D27). Not worth a retry.

    `problems` names every one. The engine sends the task to `call_for_edit` to have them added, then files again.
    """

    def __init__(
        self, problems: Sequence[AttachmentProblem], *, effect: str = "nothing was sent"
    ) -> None:
        self.problems = tuple(problems)
        listed = "; ".join(f"{p.path} ({p.reason})" for p in self.problems)
        count = len(self.problems)
        super().__init__(
            f"{count} attachment{'s' if count != 1 else ''} missing or unreadable, so {effect}: {listed}"
        )


def _why_unreadable(path: Path) -> str:
    """Why `path` can't be attached, or "" when it can be read."""
    try:
        if not path.exists():
            return "missing"
        if not path.is_file():
            return "not a file"
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return f"unreadable: {type(exc).__name__}"
    return ""


def check_attachments(paths: Iterable[Path | str]) -> list[AttachmentProblem]:
    """Every file in `paths` that is missing or can't be read, with why. It makes no Jira call (spec §12, D27)."""
    problems: list[AttachmentProblem] = []
    for raw in paths:
        path = Path(raw)
        reason = _why_unreadable(path)
        if reason:
            problems.append(AttachmentProblem(path, reason))
    return problems


class _Secret:
    """Holds the token. Its repr and str never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__


def _read_env(source: Mapping[str, str]) -> tuple[str, str, _Secret] | str:
    """The base URL, email and token from `source`, or a message saying what's missing or wrong. It never raises.

    Its frame holds the token and the environment, so the caller raises the error, from a frame that holds neither.
    """
    values: dict[str, str] = {}
    for name in ENV_VARS:
        value = (source.get(name) or "").strip()
        if not value:
            return (
                f"{name} is not set. Jira escalation needs {', '.join(ENV_VARS)} in the environment; "
                "ultron never reads Jira credentials from files."
            )
        values[name] = value
    base_url = values["JIRA_BASE_URL"].rstrip("/")
    example = "for example https://your-site.atlassian.net"
    if not base_url.startswith("https://"):
        return f"JIRA_BASE_URL must start with https:// ({example})"
    try:
        url = httpx.URL(base_url)
    except Exception as exc:  # noqa: BLE001 - only the type: the message can quote parts of the URL
        return f"JIRA_BASE_URL is not a valid URL ({type(exc).__name__}; {example})"
    if not url.host:
        return f"JIRA_BASE_URL has no host ({example})"
    if url.userinfo:
        return f"JIRA_BASE_URL must not hold a user or password; the token goes in JIRA_API_TOKEN ({example})"
    return base_url, values["JIRA_EMAIL"], _Secret(values["JIRA_API_TOKEN"])


@dataclass(frozen=True)
class JiraCredentials:
    base_url: str
    email: str
    token: _Secret = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JiraCredentials:
        """Read the three `JIRA_*` variables; a missing or empty one raises `JiraConfigError` naming it.

        `_read_env` does the reading, so this frame, which raises, holds neither the token nor `os.environ`.
        """
        found = _read_env(os.environ if environ is None else environ)
        # A mapping passed in holds the token: the error below must not keep it in this frame.
        del environ
        if isinstance(found, str):
            raise JiraConfigError(found)
        return cls(*found)


@dataclass(frozen=True)
class JiraTarget:
    """Where a repo's tickets go: the `[jira]` table of its `ultron.toml`."""

    project: str
    board: int
    epic: str

    @classmethod
    def from_config(cls, jira: Jira) -> JiraTarget:
        for name in ("project", "board", "epic"):
            if not getattr(jira, name):
                raise JiraConfigError(f"jira.{name} is not set in ultron.toml")
        assert jira.project and jira.board and jira.epic  # for the type checker
        if not jira.board.strip().isdigit():
            raise JiraConfigError(
                f"jira.board must be the board's numeric id (the number in its URL), not {jira.board!r}"
            )
        return cls(jira.project, int(jira.board), jira.epic)


@dataclass(frozen=True)
class Endpoint:
    method: str
    template: str

    def pattern(self) -> re.Pattern[str]:
        parts = re.split(r"\{[a-z_]+\}", self.template)
        return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "$")


# Every REST call the client can make. None of them creates, updates, starts, closes or deletes a sprint.
ENDPOINTS: dict[str, Endpoint] = {
    "issue_types": Endpoint("GET", "/rest/api/3/issue/createmeta/{project}/issuetypes"),
    "issue_create": Endpoint("POST", "/rest/api/3/issue"),
    "issue_get": Endpoint("GET", "/rest/api/3/issue/{key}"),
    "issue_delete": Endpoint("DELETE", "/rest/api/3/issue/{key}"),
    "issue_attach": Endpoint("POST", "/rest/api/3/issue/{key}/attachments"),
    "issue_comment": Endpoint("POST", "/rest/api/3/issue/{key}/comment"),
    "board_sprints": Endpoint("GET", "/rest/agile/1.0/board/{board_id}/sprint"),
    "sprint_move_issues": Endpoint("POST", "/rest/agile/1.0/sprint/{sprint_id}/issue"),
}


def match_endpoint(method: str, path: str) -> str | None:
    """The `ENDPOINTS` name for a request, or None if the client may not make it."""
    for name, endpoint in ENDPOINTS.items():
        if endpoint.method == method.upper() and endpoint.pattern().match(path):
            return name
    return None


@dataclass(frozen=True)
class IssueType:
    id: str
    name: str


@dataclass(frozen=True)
class SprintChoice:
    """Where the issue went: the active sprint, the first future sprint, or the backlog.

    `reason` says why it's the backlog when that isn't simply "no sprint" (a board without sprints, such as kanban).
    """

    kind: SprintKind
    sprint_id: int | None = None
    sprint_name: str | None = None
    reason: str | None = None

    def describe(self) -> str:
        if self.kind == "backlog":
            return (
                f"backlog ({self.reason or 'the board has no active or future sprint'})"
            )
        return f"{self.kind} sprint {self.sprint_name} (id {self.sprint_id})"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "id": self.sprint_id,
            "name": self.sprint_name,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class EscalationResult:
    key: str
    url: str
    requested_type: IssueTypeName
    issue_type: str
    fell_back: bool
    sprint: SprintChoice
    attachments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """For the event log (T26). Holds no credentials."""
        return {
            "key": self.key,
            "url": self.url,
            "requested_type": self.requested_type,
            "issue_type": self.issue_type,
            "fell_back": self.fell_back,
            "sprint": self.sprint.to_dict(),
            "attachments": list(self.attachments),
        }


@dataclass(frozen=True)
class CommentResult:
    """A comment `JiraClient.comment` added, for the event log. Holds no credentials."""

    issue_key: str
    comment_id: str
    text: str
    attachment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "comment_id": self.comment_id,
            "text": self.text,
            "attachment": self.attachment,
        }


def fallback_note(project: str, requested: str) -> str:
    """Said in the ticket when Feature falls back to Story; short, since the description is at most 280 characters."""
    return (
        f"filed as a {FALLBACK_TYPE} because project {project} has no {requested} type."
    )


def _labels(extra: Sequence[str]) -> list[str]:
    out: list[str] = []
    for label in (DEFAULT_LABEL, *extra):
        clean = "-".join(label.split())  # Jira labels can't contain spaces
        if clean and clean not in out:
            out.append(clean)
    return out


def _error_text(response: httpx.Response, hide: Callable[[str], str]) -> str:
    """Jira's error messages, else the start of the body. Masked before it's cut, so no piece of a secret is left."""
    text = response.text
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        parts = [str(m) for m in data.get("errorMessages") or []]
        errors = data.get("errors") or {}
        if isinstance(errors, dict):
            parts += [f"{k}: {v}" for k, v in errors.items()]
        if parts:
            text = "; ".join(parts)
    return hide(text)[:MAX_ERROR_TEXT]


@dataclass(frozen=True)
class _Reply:
    """What `_exchange` keeps of one HTTP exchange: plain values, never an httpx object."""

    status: int | None = None
    data: Any = None
    failure: str = ""
    unreachable: bool = False


def _reply(response: httpx.Response, hide: Callable[[str], str]) -> _Reply:
    """The status and either Jira's error text or the parsed JSON body; the response itself goes no further."""
    status = response.status_code
    if not response.is_success:  # redirects aren't followed, so a 3xx fails too
        return _Reply(
            status, failure=f"failed with {status}: {_error_text(response, hide)}"
        )
    if not response.content:
        return _Reply(status)  # 204 from move and delete
    try:
        data = response.json()
    except ValueError:  # JSONDecodeError or UnicodeDecodeError
        return _Reply(status, failure=f"answered {status} with a body that isn't JSON")
    return _Reply(status, data)


def _masks(credentials: JiraCredentials) -> tuple[_Secret, _Secret]:
    """The token, and the Basic-auth value httpx sends for it: what `JiraClient._hide` masks in text from Jira."""
    pair = f"{credentials.email}:{credentials.token.reveal()}".encode(errors="replace")
    return credentials.token, _Secret(base64.b64encode(pair).decode())


def _http_client(
    credentials: JiraCredentials,
    transport: httpx.BaseTransport | None,
    timeout: float,
) -> httpx.Client | str:
    """The httpx client, or the type name of the error that stopped it (a bad proxy variable, say). It never raises.

    That error's traceback holds the credentials (here) and the Basic-auth header (in httpx's frames), so only its type
    name leaves this frame.
    """
    try:
        return httpx.Client(
            base_url=credentials.base_url,
            auth=httpx.BasicAuth(credentials.email, credentials.token.reveal()),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return type(exc).__name__


class JiraClient:
    """Jira Cloud over HTTPS with Basic auth (email and API token)."""

    # Also usable as `client.check_attachments(paths)`; it makes no Jira call either way.
    check_attachments = staticmethod(check_attachments)

    def __init__(
        self,
        credentials: JiraCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = credentials.base_url
        self._secrets = _masks(credentials)
        http = _http_client(credentials, transport, timeout)
        # The credentials hold the token: the error below must not keep them in this frame.
        del credentials
        if isinstance(http, str):
            raise JiraConfigError(
                f"the Jira client can't be set up ({http}); check JIRA_BASE_URL and any proxy variables"
            )
        self._http = http

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, **kwargs: Any
    ) -> JiraClient:
        """A client for the `JIRA_*` variables. A config error is raised anew here, after `environ` is dropped."""
        failure = ""
        try:
            return cls(JiraCredentials.from_env(environ), **kwargs)
        except JiraConfigError as exc:
            failure = str(exc)
        # As in JiraCredentials.from_env: a mapping passed in holds the token.
        del environ
        raise JiraConfigError(failure)

    def __repr__(self) -> str:
        return f"JiraClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport -------------------------------------------------------------------------------------------

    def _request(
        self,
        name: str,
        path_args: Mapping[str, str | int] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Make one call from `ENDPOINTS` and return its parsed JSON body (None when the body is empty).

        A failure raises `JiraUnreachable` (connection, TLS, timeout: retry later) or `JiraError`, from this frame, which
        holds no httpx object: `_exchange` hands back plain values only.
        """
        endpoint = ENDPOINTS.get(name)
        if endpoint is None:
            raise JiraError(f"{name!r} is not an allowed endpoint")
        path = endpoint.template.format(
            **{k: quote(str(v), safe="") for k, v in (path_args or {}).items()}
        )
        method = endpoint.method
        log.debug("jira %s %s", method, path)
        reply = self._exchange(method, path, kwargs)
        if reply.failure:
            error = JiraUnreachable if reply.unreachable else JiraError
            raise error(
                f"Jira {method} {path} {reply.failure}",
                method=method,
                path=path,
                status=reply.status,
            )
        return reply.data

    def _exchange(self, method: str, path: str, kwargs: Mapping[str, Any]) -> _Reply:
        """Send one request and keep only plain values from it. Only a BaseException (Ctrl+C) gets out.

        The request, the response and every httpx error hold the Authorization header, and `raise ... from None` would
        still keep an error as `__context__`. They exist only in this frame and the httpx frames below it, and since it
        always returns, no traceback ever holds them. An error leaves as its type name only, because its message can
        quote the request.
        """
        try:
            return _reply(self._http.request(method, path, **kwargs), self._hide)
        except httpx.TransportError as exc:
            return _Reply(
                failure=f"unreachable: {type(exc).__name__}", unreachable=True
            )
        except Exception as exc:  # noqa: BLE001 - not only httpx.HTTPError: any of them may hold the request
            return _Reply(failure=f"failed: {type(exc).__name__}")

    def _hide(self, text: str) -> str:
        """`text` with the token and the Basic-auth value masked.

        Used on text from Jira (a server or proxy may quote the request) and on everything sent to it: agents and the
        target repo's tests may see the environment, so a log or test output can carry the token.
        """
        for secret in self._secrets:
            text = text.replace(secret.reveal(), "***")
        return text

    def _hide_bytes(self, data: bytes) -> bytes:
        for secret in self._secrets:
            data = data.replace(secret.reveal().encode(), b"***")
        return data

    def _hide_body(self, body: TicketBody) -> TicketBody:
        """`body` with every text masked, before anything in it is cut, rendered or sent."""
        hide = self._hide
        return replace(
            body,
            requirement=hide(body.requirement),
            task=hide(body.task),
            title=hide(body.title),
            reason=hide(body.reason),
            acceptance_criteria=tuple(hide(c) for c in body.acceptance_criteria),
            last_test_output=hide(body.last_test_output),
            attempts=tuple(
                replace(
                    row,
                    rung=hide(row.rung),
                    outcome=hide(row.outcome),
                    summary=hide(row.summary),
                )
                for row in body.attempts
            ),
            requirement_title=hide(body.requirement_title),
        )

    # -- issue types -----------------------------------------------------------------------------------------

    def issue_types(self, project: str) -> list[IssueType]:
        """The issue types that can be created in `project`."""
        found: list[IssueType] = []
        start = 0
        while True:
            data = self._request(
                "issue_types",
                {"project": project},
                params={"startAt": start, "maxResults": PAGE_SIZE},
            )
            page = data.get("issueTypes") or []
            found += [IssueType(str(t["id"]), str(t["name"])) for t in page]
            start += len(page)
            if not page or start >= int(data.get("total", start)):
                return found

    def resolve_issue_type(
        self, project: str, wanted: IssueTypeName
    ) -> tuple[IssueType, str | None]:
        """The type to create and, when Feature fell back to Story, the note saying so."""
        by_name = {t.name.casefold(): t for t in self.issue_types(project)}
        hit = by_name.get(wanted.casefold())
        if hit is not None:
            return hit, None
        if wanted == "Feature":
            story = by_name.get(FALLBACK_TYPE.casefold())
            if story is not None:
                return story, fallback_note(project, wanted)
        names = ", ".join(sorted(t.name for t in by_name.values()))
        raise JiraError(
            f"project {project} has no {wanted} issue type (it has: {names})"
        )

    # -- issues ----------------------------------------------------------------------------------------------

    def create_issue(
        self,
        *,
        project: str,
        issue_type_id: str,
        summary: str,
        description: dict[str, Any],
        epic: str,
        labels: Sequence[str] = (),
    ) -> str:
        """Create the issue under `epic` and return its key."""
        fields = {
            "project": {"key": project},
            "issuetype": {"id": issue_type_id},
            "summary": summary,
            "description": description,
            "parent": {"key": epic},
            "labels": _labels(labels),
        }
        data = self._request("issue_create", json={"fields": fields})
        return str(data["key"])

    def get_issue(self, key: str, fields: Sequence[str] = ()) -> dict[str, Any]:
        params = {"fields": ",".join(fields)} if fields else None
        data: dict[str, Any] = self._request("issue_get", {"key": key}, params=params)
        return data

    def delete_issue(self, key: str) -> None:
        """Delete an issue. Used only to clean up the owner's live test issue."""
        self._request("issue_delete", {"key": key})

    def attach(self, key: str, path: Path) -> str:
        """Upload one file to an existing issue; returns the stored file name.

        A missing or unreadable file raises `MissingAttachments` before anything is sent.
        """
        path = Path(path)
        problems = check_attachments([path])
        content = b""
        if not problems:
            try:
                content = path.read_bytes()
            except OSError as exc:  # it changed after the check
                problems = [
                    AttachmentProblem(path, f"unreadable: {type(exc).__name__}")
                ]
        if problems:
            raise MissingAttachments(problems)
        return self._upload(key, path.name, content)

    def _upload(
        self,
        key: str,
        name: str,
        content: bytes,
        mime: str = "application/octet-stream",
    ) -> str:
        data = self._request(
            "issue_attach",
            {"key": key},
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (name, self._hide_bytes(content), mime)},
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("filename", name))
        return name

    def comment(
        self, issue_key: str, text: str, attachment: Path | None = None
    ) -> CommentResult:
        """Add a comment of at most 280 characters to an existing issue, attaching `attachment` first if given (D27).

        The text becomes one line, and longer text is cut to 280 characters ending in "…": details belong in the
        attachment. The file is checked before any Jira call, so a missing one raises `MissingAttachments` and changes
        nothing.
        """
        posted = cut(one_line(self._hide(text)), MAX_COMMENT_CHARS)
        if not posted:
            raise ValueError("a Jira comment needs text")
        name: str | None = None
        if attachment is not None:
            problems = check_attachments([attachment])
            if problems:
                raise MissingAttachments(problems, effect="no comment was added")
            name = self.attach(issue_key, Path(attachment))
        data = self._request(
            "issue_comment", {"key": issue_key}, json={"body": short_doc([posted])}
        )
        comment_id = str(data.get("id", "")) if isinstance(data, dict) else ""
        return CommentResult(issue_key, comment_id, posted, name)

    # -- sprints ---------------------------------------------------------------------------------------------

    def _first_sprint(self, board: int, state: str) -> dict[str, Any] | None:
        data = self._request(
            "board_sprints",
            {"board_id": board},
            params={"state": state, "maxResults": PAGE_SIZE},
        )
        values = data.get("values") or []
        return dict(values[0]) if values else None

    def choose_sprint(self, board: int) -> SprintChoice:
        """The board's active sprint, else its first future sprint, else the backlog. Reads only.

        A board without sprints (kanban) answers the lookup with 400; that is the backlog too (spec §12).
        """
        for state in ("active", "future"):
            try:
                sprint = self._first_sprint(board, state)
            except JiraError as exc:
                if exc.status != 400 or isinstance(exc, JiraUnreachable):
                    raise
                return SprintChoice(
                    "backlog", reason=f"board {board} has no sprints: {exc}"
                )
            if sprint is not None:
                kind: SprintKind = "active" if state == "active" else "future"
                return SprintChoice(
                    kind, int(sprint["id"]), str(sprint.get("name", ""))
                )
        return SprintChoice("backlog")

    def move_to_sprint(self, sprint_id: int, keys: Sequence[str]) -> None:
        """Move issues into an existing sprint. Doesn't change the sprint itself."""
        self._request(
            "sprint_move_issues", {"sprint_id": sprint_id}, json={"issues": list(keys)}
        )

    # -- the whole escalation --------------------------------------------------------------------------------

    def escalate(
        self,
        target: JiraTarget,
        *,
        issue_type: IssueTypeName,
        body: TicketBody,
        labels: Sequence[str] = (),
        attachments: Sequence[Path] = (),
    ) -> EscalationResult:
        """File the §12 ticket (D27) and place it in a sprint, or leave it in the backlog.

        The files are checked first: a missing or unreadable one raises `MissingAttachments` before any Jira call, so
        nothing is created. The description is at most 280 characters; `report.md`, built from `body`, is attached
        first, then the files in the order given (so don't pass a report.md of your own).
        """
        files = [Path(p) for p in attachments]
        problems = check_attachments(files)
        if problems:
            raise MissingAttachments(problems, effect="no ticket was created")
        body = self._hide_body(body)
        chosen, note = self.resolve_issue_type(target.project, issue_type)
        notes = [note] if note else []
        report = render_report(body, notes).encode()
        key = self.create_issue(
            project=target.project,
            issue_type_id=chosen.id,
            summary=ticket_summary(body.requirement, body.task, body.title),
            description=to_adf(body, notes),
            epic=target.epic,
            labels=labels,
        )
        log.info("jira created %s as %s", key, chosen.name)
        # From here on the issue exists: every failure must carry its key, or a retry files it twice. The error is a
        # new one, raised after the `except` blocks: no cause or context, and no frame of the failed step behind it.
        prefix = f"Jira issue {key} was created, but a later step failed"
        error: JiraError
        try:
            names, sprint = self._attach_and_place(key, target.board, report, files)
        except JiraError as exc:
            kind = JiraUnreachable if isinstance(exc, JiraUnreachable) else JiraError
            error = kind(
                f"{prefix}: {exc}",
                method=exc.method,
                path=exc.path,
                status=exc.status,
                issue_key=key,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here must still report the key
            error = JiraError(
                f"{prefix}: {type(exc).__name__}: {self._hide(str(exc))[:MAX_ERROR_TEXT]}",
                issue_key=key,
            )
        else:
            log.info("jira %s placed in %s", key, sprint.describe())
            return EscalationResult(
                key=key,
                url=f"{self._base_url}/browse/{key}",
                requested_type=issue_type,
                issue_type=chosen.name,
                fell_back=note is not None,
                sprint=sprint,
                attachments=names,
            )
        raise error

    def _attach_and_place(
        self, key: str, board: int, report: bytes, files: Sequence[Path]
    ) -> tuple[tuple[str, ...], SprintChoice]:
        names = [self._upload(key, REPORT_NAME, report, "text/markdown")]
        names += [self.attach(key, path) for path in files]
        sprint = self.choose_sprint(board)
        if sprint.sprint_id is not None:
            self.move_to_sprint(sprint.sprint_id, [key])
        return tuple(names), sprint
