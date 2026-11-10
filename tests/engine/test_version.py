"""T14: release version parsing, bumps and defaults (spec §13, D12)."""

from __future__ import annotations

import pytest

from ultron.engine.version import (
    bump,
    current_version,
    default_bump,
    format_version,
    parse_version,
    release_bump,
)


# AC1: bump resets the lower parts.
@pytest.mark.parametrize(
    ("start", "kind", "expected"),
    [
        ("1.1.0", "minor", "1.2.0"),
        ("1.1.0", "major", "2.0.0"),
        ("1.1.0", "patch", "1.1.1"),
        ("1.1.7", "minor", "1.2.0"),
        ("1.2.3", "major", "2.0.0"),
        ("0.0.0", "patch", "0.0.1"),
        ("0.0.0", "minor", "0.1.0"),
    ],
)
def test_bump(start: str, kind: str, expected: str) -> None:
    assert bump(start, kind) == expected


def test_parse_and_format_round_trip() -> None:
    assert parse_version("1.10.2") == (1, 10, 2)
    assert format_version((1, 10, 2)) == "1.10.2"


# AC2: no releases means 0.0.0.
def test_current_version_empty_is_0_0_0() -> None:
    assert current_version([]) == "0.0.0"


def test_current_version_is_highest_by_semver() -> None:
    # Compared as numbers, not text: 1.10.0 is newer than 1.9.0.
    assert current_version(["1.9.0", "1.10.0", "1.2.3"]) == "1.10.0"


# AC3: a release takes its largest bump.
def test_release_bump_takes_largest() -> None:
    assert release_bump(["patch", "minor", "patch"]) == "minor"


def test_release_bump_major_wins() -> None:
    assert release_bump(["patch", "major", "minor"]) == "major"


def test_release_bump_all_patch() -> None:
    assert release_bump(["patch", "patch"]) == "patch"


def test_release_bump_empty_is_an_error() -> None:
    with pytest.raises(ValueError):
        release_bump([])


# AC4: maintenance defaults to patch.
def test_default_bump_maintenance_is_patch() -> None:
    assert default_bump("maintenance") == "patch"


def test_default_bump_feature_has_no_default() -> None:
    assert default_bump("feature") is None


# AC5: invalid versions raise a clear error that names the input.
@pytest.mark.parametrize(
    "bad", ["1.2", "v1.2.3", "1.2.3.4", "01.2.3", "", "1.2.x", " 1.2.3"]
)
def test_parse_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        parse_version(bad)
    assert repr(bad) in str(info.value)
