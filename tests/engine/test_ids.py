"""T08 AC5: requirement ids are `R-<yyyymmdd>-<4 hex>`, from the local date, and collision-checked."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from ultron.engine.ids import REQUIREMENT_ID_RE, new_requirement_id

PATTERN = re.compile(r"^R-\d{8}-[0-9a-f]{4}$")


def _tokens(*values: str) -> Iterator[str]:
    yield from values


def test_new_ids_match_the_pattern(tmp_path: Path) -> None:
    for _ in range(50):
        rid = new_requirement_id(tmp_path)
        assert PATTERN.fullmatch(rid)
        assert REQUIREMENT_ID_RE.fullmatch(rid)


def test_id_uses_the_local_date(tmp_path: Path) -> None:
    before = datetime.now().strftime("%Y%m%d")
    rid = new_requirement_id(tmp_path)
    after = datetime.now().strftime("%Y%m%d")
    assert rid.split("-")[1] in {before, after}


def test_id_uses_the_given_date(tmp_path: Path) -> None:
    rid = new_requirement_id(tmp_path, now=datetime(2026, 11, 9, 23, 59))
    assert rid.startswith("R-20261109-")


def test_missing_requirements_folder_has_no_collisions(tmp_path: Path) -> None:
    rid = new_requirement_id(
        tmp_path / "nope", now=datetime(2026, 1, 2), token=lambda: "a3f2"
    )
    assert rid == "R-20260102-a3f2"


def test_forced_collision_gives_a_different_id(tmp_path: Path) -> None:
    (tmp_path / "R-20261109-a3f2").mkdir()
    tokens = _tokens("a3f2", "a3f2", "00ff")
    rid = new_requirement_id(
        tmp_path, now=datetime(2026, 11, 9), token=lambda: next(tokens)
    )
    assert rid == "R-20261109-00ff"
    assert PATTERN.fullmatch(rid)


def test_a_file_with_the_same_name_also_collides(tmp_path: Path) -> None:
    (tmp_path / "R-20261109-a3f2").write_text("x")
    tokens = _tokens("a3f2", "b4c5")
    rid = new_requirement_id(
        tmp_path, now=datetime(2026, 11, 9), token=lambda: next(tokens)
    )
    assert rid == "R-20261109-b4c5"


def test_a_different_day_does_not_collide(tmp_path: Path) -> None:
    (tmp_path / "R-20261108-a3f2").mkdir()
    rid = new_requirement_id(tmp_path, now=datetime(2026, 11, 9), token=lambda: "a3f2")
    assert rid == "R-20261109-a3f2"


def test_gives_up_when_every_candidate_is_taken(tmp_path: Path) -> None:
    (tmp_path / "R-20261109-a3f2").mkdir()
    with pytest.raises(RuntimeError, match="R-20261109"):
        new_requirement_id(
            tmp_path, now=datetime(2026, 11, 9), token=lambda: "a3f2", tries=5
        )


def test_a_bad_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token"):
        new_requirement_id(tmp_path, token=lambda: "XYZ!")


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("R-20261109-a3f2", True),
        ("R-20261109-A3F2", False),
        ("R-2026119-a3f2", False),
        ("R-20261109-a3f", False),
        ("R-20261109-a3f2\n", False),
        ("r-20261109-a3f2", False),
    ],
)
def test_pattern(text: str, ok: bool) -> None:
    assert (REQUIREMENT_ID_RE.fullmatch(text) is not None) is ok
