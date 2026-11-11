"""Proofs for T35 (AC1-AC4 automated, plus extras). Map and colours are copied from spec section 18."""

import io
import re
from collections.abc import Iterator

import pytest

from ultron import cli
from ultron.look import palette
from ultron.look.render import (
    colour_enabled,
    force_utf8,
    marker,
    mascot_lines,
    unicode_ok,
    wordmark,
)

# Spec section 18 cell map, copied literally.
SPEC_MAP = """\
.......HHHHHH.......
.....HHHLLLLHHH.....
....HHHHHHHHHHHH....
...HHHHHHHHHHHHHH...
..HCHHHHHHHHHHHHCH..
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HHCHHHHHHHHHHCHH.S
S.HCHHHHHHHHHHHHCH.S
S.HHHHHHHHHHHHHHHH.S
HHHEHHHHHHHHHHHHEHHH
HHHHEEHHHHHHHHEEHHHH
HHHHEEEEHHHHEEEEHHHH
HHHHHHEEEHHEEEHHHHHH
HHHHHHHHCHHCHHHHHHHH
.HHHHFHHCHHCHHFHHHH.
.HFHFFFHHHHHHFFFHFH.
.HFHFFFFHHHHFFFFHFH.
..HHFFFFHFFHFFFFHH..
...HHFFFFFFFFFFHH...
....HFFFHFFHFFFH....
....HHFFHHHHFFHH....
.....HHHHHHHHHH.....
......HHHHHHHH......
.......HHHHHH.......
........HHHH........
""".splitlines()

SPEC_COLOUR = {
    "H": "#7A828A",
    "C": "#5A6168",
    "S": "#5A6168",
    "L": "#b3b3b3",
    "E": "#941212",
    "F": "#262626",
}

SPEC_HEX = [
    "#7A828A",
    "#5A6168",
    "#b3b3b3",
    "#941212",
    "#262626",
    "#E5484D",
    "#c9c9c9",
    "#f2f2f2",
    "#7A828A",
    "#941212",
    "#D97757",
    "#3FB950",
    "#E3B341",
]

SGR = re.compile(r"\x1b\[([0-9;]*)m|(.)", re.DOTALL)


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _hex(params: list[str]) -> str:
    return "#" + "".join(f"{int(x):02x}" for x in params)


def _decode(line: str) -> list[tuple[str, str | None, str | None]]:
    """Return (glyph, fg, bg) per visible character, tracking SGR state."""
    cells: list[tuple[str, str | None, str | None]] = []
    fg: str | None = None
    bg: str | None = None
    for m in SGR.finditer(line):
        if m.group(1) is not None:
            params = m.group(1).split(";") if m.group(1) else ["0"]
            i = 0
            while i < len(params):
                p = params[i]
                if p == "0":
                    fg = bg = None
                elif p == "49":
                    bg = None
                elif p == "38" and params[i + 1] == "2":
                    fg = _hex(params[i + 2 : i + 5])
                    i += 4
                elif p == "48" and params[i + 1] == "2":
                    bg = _hex(params[i + 2 : i + 5])
                    i += 4
                i += 1
        else:
            cells.append((m.group(2), fg, bg))
    return cells


def _pairs() -> Iterator[tuple[int, int, str, str]]:
    for line_no in range(14):
        top, bottom = SPEC_MAP[2 * line_no], SPEC_MAP[2 * line_no + 1]
        for col in range(20):
            yield line_no, col, top[col], bottom[col]


def test_spec_map_shape() -> None:
    assert len(SPEC_MAP) == 28
    assert all(len(row) == 20 for row in SPEC_MAP)


def test_mascot_cells() -> None:
    lines = mascot_lines(colour=True, unicode=True)
    assert len(lines) == 14
    cells = [_decode(line) for line in lines]
    assert all(len(line) == 20 for line in cells)
    for line_no, col, top, bottom in _pairs():
        glyph, fg, bg = cells[line_no][col]
        if top == "." and bottom == ".":
            assert glyph == " "
        elif top != "." and bottom != ".":
            assert (glyph, fg, bg) == (
                "▀",
                SPEC_COLOUR[top].lower(),
                SPEC_COLOUR[bottom].lower(),
            )
        elif top == ".":
            assert (glyph, fg, bg) == ("▄", SPEC_COLOUR[bottom].lower(), None)
        else:
            assert (glyph, fg, bg) == ("▀", SPEC_COLOUR[top].lower(), None)


def test_mascot_plain_is_14_by_20() -> None:
    lines = mascot_lines(colour=False, unicode=True)
    assert len(lines) == 14
    assert all(len(line) == 20 for line in lines)
    assert all("\x1b" not in line for line in lines)


def test_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    tty = FakeTTY()
    colour = colour_enabled(tty)
    assert colour is False
    assert colour_enabled(io.StringIO()) is False
    output = "\n".join(mascot_lines(colour, unicode_ok(tty)))
    output += wordmark(colour) + marker("ok", colour, unicode_ok(tty))
    assert "\x1b" not in output


def test_no_color_empty_value_keeps_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    assert colour_enabled(FakeTTY()) is True


def test_non_tty_has_no_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert colour_enabled(io.StringIO()) is False
    assert colour_enabled(FakeTTY()) is True


def test_ascii_fallback() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    assert unicode_ok(stream) is False
    lines = mascot_lines(colour=False, unicode=unicode_ok(stream))
    assert len(lines) == 14
    assert all(len(line) == 20 for line in lines)
    assert all(ord(ch) < 128 for line in lines for ch in line)
    # Row pair 12/13, column 3: E above H gives '@'.
    assert lines[6][3] == "@"
    # Row pair 10/11, column 0: S over S gives '|'.
    assert lines[5][0] == "|"
    # Row pair 0/1, column 7: H over H gives '#'.
    assert lines[0][7] == "#"
    # Both rows transparent gives a space.
    assert lines[0][0] == " "


def test_unicode_ok_utf8() -> None:
    assert unicode_ok(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) is True
    # No encoding attribute at all: treated as not printable.
    assert unicode_ok(io.StringIO()) is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GUNMETAL", "#7A828A"),
        ("DARK_GUNMETAL", "#5A6168"),
        ("SHINE", "#b3b3b3"),
        ("EYE", "#941212"),
        ("GAP", "#262626"),
        ("CRIMSON", "#E5484D"),
        ("MAIN_TEXT", "#c9c9c9"),
        ("EMPHASIS", "#f2f2f2"),
        ("DIM_TEXT", "#7A828A"),
        ("FAILURE", "#941212"),
        ("RUNNING", "#D97757"),
        ("APPROVED", "#3FB950"),
        ("WAITING", "#E3B341"),
    ],
)
def test_palette_hex(name: str, value: str) -> None:
    assert getattr(palette, name) == value


def test_palette_covers_spec_hex_list() -> None:
    values = [getattr(palette, name) for name in dir(palette) if name.isupper()]
    exposed = {value.lower() for value in values if isinstance(value, str)}
    assert {h.lower() for h in SPEC_HEX} <= exposed


def test_wordmark_and_markers() -> None:
    assert wordmark(colour=False) == "U L T R O N"
    styled = wordmark(colour=True)
    assert "U L T R O N" in styled
    assert "\x1b[1m" in styled
    assert "\x1b[38;2;229;72;77m" in styled
    assert marker("ok", False, True) == "✔"
    assert marker("waiting", False, True) == "⏳"
    assert marker("failed", False, True) == "✖"
    assert marker("running", False, True) == "●"
    assert marker("ok", False, False) == "+"
    assert marker("waiting", False, False) == "~"
    assert marker("failed", False, False) == "x"
    assert marker("running", False, False) == "*"
    assert "\x1b[38;2;148;18;18m" in marker("failed", True, True)
    with pytest.raises(ValueError):
        marker("nope", False, True)


def test_force_utf8_does_not_raise() -> None:
    force_utf8()


def test_look_is_hidden_but_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "look" not in capsys.readouterr().out

    assert cli.main(["look"]) == 0
    out = capsys.readouterr().out
    assert "U L T R O N" in out
    assert "\x1b" not in out
