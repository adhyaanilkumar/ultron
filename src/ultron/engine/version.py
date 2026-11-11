"""Release versions: parse, compare, bump and pick the bump for a release (spec §13, D12).

Pure functions. A version is `major.minor.patch` with decimal numbers and no leading zeros.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

Bump = Literal["major", "minor", "patch"]

_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_RANK: dict[Bump, int] = {"patch": 0, "minor": 1, "major": 2}


def parse_version(text: str) -> tuple[int, int, int]:
    """Return `(major, minor, patch)` for `text`, or raise `ValueError` naming the input."""
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(
            f"invalid version {text!r}: expected major.minor.patch, e.g. 1.2.3"
        )
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def format_version(version: tuple[int, int, int]) -> str:
    """Return the `major.minor.patch` spelling of `version`."""
    major, minor, patch = version
    return f"{major}.{minor}.{patch}"


def bump(version: str, kind: Bump) -> str:
    """Apply `kind` to `version`. A major bump resets minor and patch; minor resets patch."""
    major, minor, patch = parse_version(version)
    if kind == "major":
        return format_version((major + 1, 0, 0))
    if kind == "minor":
        return format_version((major, minor + 1, 0))
    return format_version((major, minor, patch + 1))


def current_version(releases: Iterable[str]) -> str:
    """Return the highest recorded release by semver order, or `0.0.0` when there are none."""
    parsed = [parse_version(release) for release in releases]
    if not parsed:
        return "0.0.0"
    return format_version(max(parsed))


def release_bump(kinds: Iterable[Bump]) -> Bump:
    """Return the largest bump in `kinds` (major > minor > patch)."""
    ranked = list(kinds)
    if not ranked:
        raise ValueError("a release needs at least one requirement bump")
    return max(ranked, key=lambda kind: _RANK[kind])


def default_bump(requirement_kind: str) -> Bump | None:
    """Return the default bump for a requirement kind.

    Maintenance defaults to `patch`. The spec gives no default for other kinds, so they return
    `None` and the spec agent proposes the bump.
    """
    if requirement_kind == "maintenance":
        return "patch"
    return None
