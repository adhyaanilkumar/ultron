import pytest

from ultron.engine import version as v


@pytest.mark.parametrize(
    "start,kind,want",
    [
        ("1.1.0", "minor", "1.2.0"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("0.0.0", "patch", "0.0.1"),
        ("1.9.9", "minor", "1.10.0"),
        ("9.9.9", "major", "10.0.0"),
    ],
)
def test_ac1_bump(start, kind, want):
    assert v.bump(start, kind) == want


def test_ac2_no_releases():
    assert v.current_version([]) == "0.0.0"
    assert v.current_version(iter([])) == "0.0.0"


def test_ac2_current_with_releases():
    assert v.current_version(["1.0.0", "1.2.0", "1.10.0", "1.9.0"]) == "1.10.0"
    assert v.current_version(["0.1.0"]) == "0.1.0"


@pytest.mark.parametrize(
    "kinds,want",
    [
        (["patch", "minor", "patch"], "minor"),
        (["patch", "patch"], "patch"),
        (["minor", "major", "patch"], "major"),
        (["major"], "major"),
    ],
)
def test_ac3_release_bump(kinds, want):
    assert v.release_bump(kinds) == want


def test_ac3_release_bump_empty_errors():
    with pytest.raises(ValueError):
        v.release_bump([])


def test_ac4_maintenance_default_patch():
    assert v.default_bump("maintenance") == "patch"


@pytest.mark.parametrize(
    "bad",
    [
        "1.2",
        "v1.2.3",
        "1.2.3.4",
        "",
        "a.b.c",
        "1.2.x",
        "-1.2.3",
        " 1.2.3",
        "1.2.3\n",
        "01.2.3",
        "1..3",
    ],
)
def test_ac5_invalid(bad):
    with pytest.raises(ValueError) as e:
        v.parse_version(bad)
    assert repr(bad) in str(e.value) or "version" in str(e.value)
    with pytest.raises(ValueError):
        v.bump(bad, "patch")
    with pytest.raises(ValueError):
        v.current_version(["1.0.0", bad])
