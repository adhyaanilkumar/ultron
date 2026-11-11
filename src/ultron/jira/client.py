"""A small Jira Cloud REST client for escalation tickets (spec §12, D5).

Credentials come only from the environment (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) and are never written to
files, logs, exceptions or results. The client creates an issue under the configured epic, attaches files, and moves
the issue into the board's active sprint (else the first future sprint, else it stays in the backlog). It never
creates, starts, closes or edits a sprint: every call goes through `ENDPOINTS`, which has no such endpoint.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx

from ultron.config.models import Jira
from ultron.jira.body import TicketBody, ticket_summary, to_adf

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


@dataclass(frozen=True)
class JiraCredentials:
    base_url: str
    email: str
    token: _Secret = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JiraCredentials:
        """Read the three `JIRA_*` variables; a missing or empty one raises `JiraConfigError` naming it."""
        source = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for name in ENV_VARS:
            value = (source.get(name) or "").strip()
            if not value:
                raise JiraConfigError(
                    f"{name} is not set. Jira escalation needs {', '.join(ENV_VARS)} in the environment; "
                    "ultron never reads Jira credentials from files."
                )
            values[name] = value
        base_url = values["JIRA_BASE_URL"].rstrip("/")
        if not base_url.startswith("https://"):
            raise JiraConfigError(
                "JIRA_BASE_URL must start with https:// (for example https://your-site.atlassian.net)"
            )
        return cls(base_url, values["JIRA_EMAIL"], _Secret(values["JIRA_API_TOKEN"]))


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
    """Where the issue went: the active sprint, the first future sprint, or the backlog."""

    kind: SprintKind
    sprint_id: int | None = None
    sprint_name: str | None = None

    def describe(self) -> str:
        if self.kind == "backlog":
            return "backlog (the board has no active or future sprint)"
        return f"{self.kind} sprint {self.sprint_name} (id {self.sprint_id})"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.sprint_id, "name": self.sprint_name}


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


def fallback_note(project: str, requested: str) -> str:
    return (
        f"ultron asked for issue type {requested}, but project {project} has no {requested} type, "
        f"so this ticket was filed as a {FALLBACK_TYPE}."
    )


def _labels(extra: Sequence[str]) -> list[str]:
    out: list[str] = []
    for label in (DEFAULT_LABEL, *extra):
        clean = "-".join(label.split())  # Jira labels can't contain spaces
        if clean and clean not in out:
            out.append(clean)
    return out


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:MAX_ERROR_TEXT]
    if isinstance(data, dict):
        parts = [str(m) for m in data.get("errorMessages") or []]
        errors = data.get("errors") or {}
        if isinstance(errors, dict):
            parts += [f"{k}: {v}" for k, v in errors.items()]
        if parts:
            return "; ".join(parts)[:MAX_ERROR_TEXT]
    return response.text[:MAX_ERROR_TEXT]


class JiraClient:
    """Jira Cloud over HTTPS with Basic auth (email and API token)."""

    def __init__(
        self,
        credentials: JiraCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = credentials.base_url
        self._http = httpx.Client(
            base_url=credentials.base_url,
            auth=httpx.BasicAuth(credentials.email, credentials.token.reveal()),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, **kwargs: Any
    ) -> JiraClient:
        return cls(JiraCredentials.from_env(environ), **kwargs)

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
    ) -> httpx.Response:
        endpoint = ENDPOINTS.get(name)
        if endpoint is None:
            raise JiraError(f"{name!r} is not an allowed endpoint")
        path = endpoint.template.format(
            **{k: quote(str(v), safe="") for k, v in (path_args or {}).items()}
        )
        method = endpoint.method
        log.debug("jira %s %s", method, path)
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            # Re-raised without the original: it holds the request, and the request holds the auth header.
            raise JiraUnreachable(
                f"Jira {method} {path} unreachable: {type(exc).__name__}",
                method=method,
                path=path,
            ) from None
        if response.is_error:
            raise JiraError(
                f"Jira {method} {path} failed with {response.status_code}: {_error_text(response)}",
                method=method,
                path=path,
                status=response.status_code,
            )
        return response

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
            ).json()
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
        data = self._request("issue_create", json={"fields": fields}).json()
        return str(data["key"])

    def get_issue(self, key: str, fields: Sequence[str] = ()) -> dict[str, Any]:
        params = {"fields": ",".join(fields)} if fields else None
        data: dict[str, Any] = self._request(
            "issue_get", {"key": key}, params=params
        ).json()
        return data

    def delete_issue(self, key: str) -> None:
        """Delete an issue. Used only to clean up the owner's live test issue."""
        self._request("issue_delete", {"key": key})

    def attach(self, key: str, path: Path) -> str:
        """Upload one file to the issue; returns the stored file name."""
        content = path.read_bytes()
        data = self._request(
            "issue_attach",
            {"key": key},
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (path.name, content, "application/octet-stream")},
        ).json()
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return str(data[0].get("filename", path.name))
        return path.name

    # -- sprints ---------------------------------------------------------------------------------------------

    def _first_sprint(self, board: int, state: str) -> dict[str, Any] | None:
        data = self._request(
            "board_sprints",
            {"board_id": board},
            params={"state": state, "maxResults": PAGE_SIZE},
        ).json()
        values = data.get("values") or []
        return dict(values[0]) if values else None

    def choose_sprint(self, board: int) -> SprintChoice:
        """The board's active sprint, else its first future sprint, else the backlog. Reads only."""
        for state in ("active", "future"):
            sprint = self._first_sprint(board, state)
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
        """Create the §12 ticket, attach the files and place it in a sprint (or leave it in the backlog)."""
        chosen, note = self.resolve_issue_type(target.project, issue_type)
        key = self.create_issue(
            project=target.project,
            issue_type_id=chosen.id,
            summary=ticket_summary(body.requirement, body.task, body.title),
            description=to_adf(body, notes=[note] if note else []),
            epic=target.epic,
            labels=labels,
        )
        log.info("jira created %s as %s", key, chosen.name)
        try:
            names = tuple(self.attach(key, path) for path in attachments)
            sprint = self.choose_sprint(target.board)
            if sprint.sprint_id is not None:
                self.move_to_sprint(sprint.sprint_id, [key])
        except JiraError as exc:
            exc.issue_key = key
            raise
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
