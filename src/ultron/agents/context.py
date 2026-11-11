"""The context builder: what each ultron agent is told (spec 11.3, D22).

The engine builds every prompt from files, the same way every time. `build_context(role, inputs)` returns the
prompt, the prompt's version and the list of files that went into it, each with its SHA-256, so the call record can
say exactly what the agent saw.

Everything that is not ultron's own template (files, diffs, test output, comments, the target repo's CLAUDE.md and
AGENTS.md) is untrusted data. It is wrapped in a fence whose marker carries the content's own hash, so the content
cannot close its fence, and the template states that fenced text is data, not instructions (NFR4).

The testing role is built from its task's criteria, the diff and repo instructions only. It never reads the
developer's plan, transcripts or attempt reports, and it ignores retry input.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultron.agents.calllog import CallContext
from ultron.agents.prompts import (
    INTENT_TEMPLATE,
    INTENT_TEMPLATE_PATH,
    REPO_INSTRUCTIONS_HEADING,
    TEMPLATES,
)

__all__ = [
    "BuiltContext",
    "ContextError",
    "ContextFile",
    "ContextInputs",
    "Retry",
    "build_context",
    "record_path",
]

INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md")
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".ultron",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
MAX_FILE_BYTES = 200_000
MAX_INSTRUCTION_FILES = 50
_TRUNCATED = "\n[ultron: file cut at 200000 bytes]\n"
_SECTION_TRUNCATED = "\n[ultron: task section cut at 200000 bytes]\n"

# C0 control characters (tab, LF and CR excepted), DEL and the C1 controls. A NUL breaks subprocess adapters, which
# pass the prompt as a C string, and escape sequences (ESC, or the 8-bit CSI U+009B) could rewrite a terminal that
# shows the prompt. U+0085 is also a line break.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# A fence label (a path) must stay on its BEGIN line and read as it is: no control characters, no line or paragraph
# separators and no bidi controls (ALM, LRM, RLM, embeddings, overrides and isolates). Paths never need them.
_LABEL_UNSAFE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)

_ROLES_WITH_REPO_INSTRUCTIONS = frozenset(
    {"spec", "decomposition", "developer", "testing", "maintenance"}
)


class ContextError(ValueError):
    """A file or input the role needs is missing or not allowed."""


@dataclass(frozen=True)
class Retry:
    """What the engine hands a developer on a retry or a revision. All of it is data."""

    failing_output: str | None = None
    stop_reason: str | None = None
    gate_comments: str | None = None


@dataclass(frozen=True)
class ContextInputs:
    repo: Path
    """The repo (or worktree) root. Repo instructions are read from here."""
    requirement_dir: Path
    """`sdlc/requirements/R<n>`: holds request.md, intent.md, research.md, spec.md, tasks.md and tasks/T<k>/."""
    repo_instructions: bool = True
    task: str | None = None
    phase: Literal["plan", "code"] = "code"
    dependency_files: Sequence[Path] = ()
    """Files the task's dependencies changed (the engine computes them)."""
    retry: Retry | None = None
    gate_comments: str | None = None
    """The user's comments and answers from the spec or tasks gate, for spec and decomposition."""
    diff: str | None = None
    current_version: str = "0.0.0"
    evidence: str | None = None
    release: str | None = None
    release_task_files: Sequence[Path] = ()


@dataclass(frozen=True)
class ContextFile:
    path: str
    sha256: str
    kind: str
    truncated: bool = False


@dataclass(frozen=True)
class BuiltContext:
    role: str
    prompt: str
    prompt_version: str
    files: tuple[ContextFile, ...]

    omitted_files: tuple[str, ...] = ()
    """Repo instruction files found past the cap of 50 and left out of the prompt (paths only, never read)."""

    def files_record(self) -> list[dict[str, str | bool]]:
        """What the call record keeps: each included file's path, SHA-256, kind and whether it was cut.

        Every value is valid UTF-8, so the event can always be written. A path that is not valid Unicode also gets a
        `path_b64` key (see `record_path`).
        """
        return [
            {
                **record_path(f.path),
                "sha256": f.sha256,
                "kind": f.kind,
                "truncated": f.truncated,
            }
            for f in self.files
        ]

    def call_context(
        self,
        requirement: str,
        *,
        task: str | None = None,
        attempt: int | None = None,
        run: str | None = None,
    ) -> CallContext:
        """A `CallContext` for `LoggingRunner` that carries the version and files into the `agent_call` event."""
        return CallContext(
            requirement,
            task=task,
            attempt=attempt,
            run=run,
            prompt_version=self.prompt_version,
            context_files=tuple(self.files_record()),
            context_omitted=tuple(_shown(p) for p in self.omitted_files),
        )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shown(path: str) -> str:
    """`path` as valid UTF-8: each lone surrogate becomes `\\udcXX` text. A valid-Unicode path is unchanged."""
    return path.encode("utf-8", errors="backslashreplace").decode("utf-8")


def record_path(path: str) -> dict[str, str]:
    """How the call record keeps `path`: always valid UTF-8 (so logging never fails on a hostile name), and reversible.

    A valid-Unicode path is kept exactly: `{"path": path}`. A name that is not valid Unicode reaches Python as a lone
    surrogate (an unpaired UTF-16 unit on Windows, a non-UTF-8 byte via `surrogateescape` on POSIX). Then `path` shows
    each surrogate as `\\udcXX` text and `path_b64` holds the exact path, base64 of its UTF-8 with surrogates passed
    through: `base64.b64decode(path_b64).decode("utf-8", "surrogatepass")` gives the path back.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        exact = path.encode("utf-8", errors="surrogatepass")
        return {
            "path": _shown(path),
            "path_b64": base64.b64encode(exact).decode("ascii"),
        }
    return {"path": path}


def _clean(text: str) -> str:
    """Text safe to put in a prompt: no NUL or other C0/C1 control character, and valid UTF-8 (no lone surrogate)."""
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    return _CONTROL.sub("\ufffd", text)


def _clean_label(label: str) -> str:
    """A fence label stays on its BEGIN line and reads as it is: line breaks, controls and bidi controls become `?`."""
    return _clean(_LABEL_UNSAFE.sub("?", label))


def _cut(text: str, marker: str) -> tuple[str, bool]:
    """`text` cut to MAX_FILE_BYTES of UTF-8, with `marker` appended when it was cut."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= MAX_FILE_BYTES:
        return text, False
    return data[:MAX_FILE_BYTES].decode("utf-8", errors="ignore") + marker, True


def _fence(label: str, text: str) -> str:
    """Wrap untrusted `text` in a fence. The mark is the SHA-256 prefix of the cleaned text, as the agent sees it."""
    label = _clean_label(label)
    text = _clean(text)
    mark = _sha(text.encode("utf-8"))[:16]
    body = text if text.endswith("\n") else text + "\n"
    return (
        f"<<<BEGIN DATA {label} sha256={mark}>>>\n{body}<<<END DATA sha256={mark}>>>\n"
    )


class _Builder:
    def __init__(self, role: str, inputs: ContextInputs) -> None:
        self.role = role
        self.inputs = inputs
        self.repo = Path(inputs.repo).resolve()
        self.req = Path(inputs.requirement_dir).resolve()
        self.files: dict[str, ContextFile] = {}
        self.omitted: list[str] = []
        self.sections: list[str] = []

    # -- reading

    def rel(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.repo).as_posix()
        except ValueError:
            return resolved.as_posix()

    def read_raw(self, path: Path) -> tuple[str, bytes]:
        """Read all of `path` (inside the repo) without recording it. Returns (label, bytes)."""
        path = Path(path)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.repo):
            raise ContextError(f"{path} is outside the repository {self.repo}")
        if not resolved.is_file():
            raise ContextError(f"{self.rel(path)} does not exist")
        return self.rel(path), resolved.read_bytes()

    def record(self, label: str, raw: bytes, kind: str, truncated: bool) -> None:
        self.files.setdefault(label, ContextFile(label, _sha(raw), kind, truncated))

    def read(self, path: Path, kind: str) -> tuple[str, str]:
        """Read `path` (inside the repo), record it, return (label, text cut at MAX_FILE_BYTES)."""
        label, raw = self.read_raw(path)
        truncated = len(raw) > MAX_FILE_BYTES
        text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += _TRUNCATED
        self.record(label, raw, kind, truncated)
        return label, text

    def requirement_file(self, name: str, *, required: bool = True) -> Path | None:
        path = self.req / name
        if path.is_file():
            return path
        if required:
            raise ContextError(f"{self.rel(path)} does not exist")
        return None

    # -- sections

    def data(self, title: str, label: str, text: str, note: str = "") -> None:
        head = f"## {title}\n" + (f"{note}\n" if note else "")
        self.sections.append(head + _fence(label, text))

    def file_section(
        self, title: str, path: Path, kind: str = "requirement", note: str = ""
    ) -> None:
        label, text = self.read(path, kind)
        self.data(title, label, text, note)

    def task_section(self, title: str) -> None:
        task = self.inputs.task
        if not task:
            raise ContextError(f"role {self.role} needs a task id")
        path = self.requirement_file("tasks.md")
        assert path is not None
        # Search the whole file: a task can sit past the size cap. Only the section is cut, if it is too long.
        label, raw = self.read_raw(path)
        found = _task_block(raw.decode("utf-8", errors="replace"), task)
        if found is None:
            raise ContextError(f"task {task} is not in {label}")
        section, truncated = _cut(found, _SECTION_TRUNCATED)
        self.record(label, raw, "requirement", truncated)
        self.data(title, f"{label}#{task}", section)

    def optional_text(self, title: str, label: str, text: str | None) -> None:
        if text and text.strip():
            self.data(title, label, text)

    def repo_instructions(self) -> None:
        if not self.inputs.repo_instructions:
            return
        if self.role not in _ROLES_WITH_REPO_INSTRUCTIONS:
            return
        found = discover_repo_instructions(self.repo, limit=None)
        if not found:
            return
        kept, rest = found[:MAX_INSTRUCTION_FILES], found[MAX_INSTRUCTION_FILES:]
        parts = [f"## {REPO_INSTRUCTIONS_HEADING}\n"]
        for path in kept:
            label, text = self.read(path, "repo_instruction")
            parts.append(_fence(label, text))
        if rest:
            self.omitted.extend(self.rel(p) for p in rest)
            parts.append(
                f"[ultron: {len(rest)} more repository instruction files were found and left out; "
                f"only the first {MAX_INSTRUCTION_FILES}, shallowest first, are shown]\n"
            )
        self.sections.append("".join(parts))

    def finish(self) -> BuiltContext:
        template = TEMPLATES[self.role]
        prompt = template.text + "\n# Context\n\n" + "\n".join(self.sections)
        return BuiltContext(
            role=self.role,
            prompt=prompt,
            prompt_version=template.version,
            files=tuple(self.files.values()),
            omitted_files=tuple(self.omitted),
        )


# The heading after a task's own: one to three `#` at a line start, then a space, a tab or the line's end. LF, CRLF
# and a lone CR all end a line.
_NEXT_HEADING = re.compile(r"(?<=[\r\n])#{1,3}(?=[ \t\r\n]|\Z)")


def _task_block(tasks_md: str, task: str) -> str | None:
    """The `### <task> ...` section of tasks.md, up to the next heading. `T2` does not match `T20`.

    A leading BOM is ignored, and LF, CRLF and a lone CR all end a line: Windows tools write both.
    """
    text = tasks_md.removeprefix("\ufeff")
    head = re.compile(
        rf"(?:\A|(?<=[\r\n]))#{{1,3}}[ \t]+{re.escape(task)}(?![0-9A-Za-z])"
    )
    match = head.search(text)
    if match is None:
        return None
    nxt = _NEXT_HEADING.search(text, match.end())
    end = nxt.start() if nxt else len(text)
    return text[match.start() : end].rstrip() + "\n"


def discover_repo_instructions(
    repo: Path, limit: int | None = MAX_INSTRUCTION_FILES
) -> list[Path]:
    """Root and nested `CLAUDE.md` and `AGENTS.md` under `repo`, shallowest first.

    Symlinks and tool folders are skipped. At most `limit` paths are returned (all of them when `limit` is None).
    """
    root = Path(repo).resolve()
    found: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in names:
            if name not in INSTRUCTION_NAMES:
                continue
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                continue
            found.append(path)
    found.sort(
        key=lambda p: (len(p.relative_to(root).parts), p.relative_to(root).as_posix())
    )
    return found if limit is None else found[:limit]


# -- the seven roles


def _scout(b: _Builder) -> None:
    path = b.requirement_file("request.md")
    assert path is not None
    b.file_section("The request", path)
    b.sections.append(
        "## Access\nRead-only Read, Grep and Glob over the repository and "
        "sdlc/requirements. Nothing else.\n"
    )


def _intake(b: _Builder) -> None:
    path = b.requirement_file("request.md")
    assert path is not None
    b.file_section("The request", path)
    b.data("The intent template", INTENT_TEMPLATE_PATH, INTENT_TEMPLATE)
    b.files[INTENT_TEMPLATE_PATH] = ContextFile(
        INTENT_TEMPLATE_PATH, _sha(INTENT_TEMPLATE.encode("utf-8")), "template"
    )
    research = b.requirement_file("research.md", required=False)
    if research is not None:
        b.file_section(
            "Research notes (facts with sources from the intake scout)", research
        )


def _spec(b: _Builder) -> None:
    b.sections.append(f"## Current version\n{b.inputs.current_version}\n")
    intent = b.requirement_file("intent.md")
    assert intent is not None
    b.file_section("The approved intent", intent)
    research = b.requirement_file("research.md", required=False)
    if research is not None:
        b.file_section(
            "Research notes (non-binding starting notes, check them against the code)",
            research,
        )
    b.optional_text(
        "Comments and answers from the spec gate",
        "gate comments",
        b.inputs.gate_comments,
    )
    b.sections.append(
        "## Access\nRead-only access to the codebase at the repository root.\n"
    )
    b.repo_instructions()


def _decomposition(b: _Builder) -> None:
    intent = b.requirement_file("intent.md")
    spec = b.requirement_file("spec.md")
    assert intent is not None and spec is not None
    b.file_section("The approved intent", intent)
    b.file_section("The approved spec", spec)
    b.optional_text(
        "Comments from the tasks gate", "gate comments", b.inputs.gate_comments
    )
    b.repo_instructions()


def _developer(b: _Builder) -> None:
    task = b.inputs.task
    if not task:
        raise ContextError("role developer needs a task id")
    code = b.inputs.phase == "code"
    b.sections.append(f"## Phase\n{b.inputs.phase}\n")
    spec = b.requirement_file("spec.md")
    assert spec is not None
    b.file_section("The approved spec", spec)
    b.task_section(f"Your task {task} and its acceptance criteria")
    plan = b.req / "tasks" / task / "plan.md"
    if plan.is_file():
        title = (
            "The approved plan (follow it)"
            if code
            else "The previous draft plan (revise it using the comments)"
        )
        b.file_section(title, plan)
    elif code:
        raise ContextError(
            f"{b.rel(plan)} does not exist; the code phase needs an approved plan"
        )
    for dep in b.inputs.dependency_files:
        label, text = b.read(Path(dep), "dependency")
        b.data("A file changed by a task this one depends on", label, text)
    retry = b.inputs.retry
    if retry is not None:
        b.optional_text(
            "Failing test output from the engine",
            "failing output",
            retry.failing_output,
        )
        b.optional_text("Why the last run stopped", "stop reason", retry.stop_reason)
        b.optional_text(
            "Comments from the user at the gate", "gate comments", retry.gate_comments
        )
    b.repo_instructions()


def _testing(b: _Builder) -> None:
    if b.inputs.diff is None:
        raise ContextError("role testing needs the diff")
    b.task_section(f"Task {b.inputs.task} and its acceptance criteria")
    b.data("The diff to judge", "diff", b.inputs.diff)
    b.repo_instructions()


def _maintenance(b: _Builder) -> None:
    if not (b.inputs.evidence and b.inputs.release):
        raise ContextError(
            "role maintenance needs the failure evidence and the release"
        )
    b.data("The failure evidence", "evidence", b.inputs.evidence)
    b.data("The release", "release", b.inputs.release)
    for path in b.inputs.release_task_files:
        label, text = b.read(Path(path), "release_tasks")
        b.data("The tasks in that release", label, text)
    b.repo_instructions()


_ROLE_BUILDERS: dict[str, Callable[[_Builder], None]] = {
    "intake_scout": _scout,
    "intake": _intake,
    "spec": _spec,
    "decomposition": _decomposition,
    "developer": _developer,
    "testing": _testing,
    "maintenance": _maintenance,
}


def build_context(role: str, inputs: ContextInputs) -> BuiltContext:
    """Build `role`'s prompt and file list from `inputs`. Raises `ContextError` when a needed file is missing."""
    try:
        fill = _ROLE_BUILDERS[role]
    except KeyError:
        raise ContextError(
            f"unknown role {role!r}; known: {', '.join(_ROLE_BUILDERS)}"
        ) from None
    builder = _Builder(role, inputs)
    fill(builder)
    return builder.finish()
