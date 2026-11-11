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


def test_ac1_bump_returns_str_and_resets_lower_parts():
    assert v.bump("3.4.5", "major") == "4.0.0"
    assert v.bump("3.4.5", "minor") == "3.5.0"
    assert v.bump("3.4.5", "patch") == "3.4.6"
    assert isinstance(v.bump("1.0.0", "patch"), str)


def test_ac1_bump_pre_1_0():
    assert v.bump("0.1.0", "minor") == "0.2.0"
    assert v.bump("0.9.9", "major") == "1.0.0"


def test_ac2_current_orders_by_semver_not_string_or_input_order():
    assert v.current_version(["1.9.0", "1.10.0", "1.2.0"]) == "1.10.0"
    assert v.current_version(["2.0.0", "10.0.0", "9.9.9"]) == "10.0.0"
    assert v.current_version(["1.0.10", "1.0.9"]) == "1.0.10"
    assert v.current_version(["0.0.0"]) == "0.0.0"
    assert v.current_version(("1.0.0", "0.9.0")) == "1.0.0"


def test_ac3_release_bump_all_orderings():
    import itertools

    for perm in itertools.permutations(["patch", "minor", "major"]):
        assert v.release_bump(list(perm)) == "major"
    assert v.release_bump(["minor", "minor"]) == "minor"
    assert v.release_bump(iter(["patch", "minor"])) == "minor"


def test_ac4_maintenance_default_is_patch_and_valid_bump():
    assert v.default_bump("maintenance") == "patch"
    assert v.release_bump([v.default_bump("maintenance")]) == "patch"


def test_ac5_error_is_valueerror_naming_input():
    for bad in ["1.2", "v1.2.3"]:
        with pytest.raises(ValueError) as e:
            v.parse_version(bad)
        assert bad in str(e.value)


def test_ac5_valid_versions_parse():
    assert v.parse_version("1.2.3") == (1, 2, 3)
    assert v.parse_version("0.0.0") == (0, 0, 0)
    assert v.parse_version("10.20.30") == (10, 20, 30)
