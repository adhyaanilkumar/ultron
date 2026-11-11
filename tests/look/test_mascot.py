"""AC1-AC3 and AC6 for the mascot, judged the way a terminal shows it (pyte keeps colour state across cells)."""

import io
import sys

import pyte
import pytest
from test_look import SPEC_COLOUR, SPEC_MAP

from ultron import cli
from ultron.look.render import mascot_lines, unicode_ok


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class NonTty(Tty):
    def isatty(self) -> bool:
        return False


def _screen(lines: list[str]) -> pyte.Screen:
    screen = pyte.Screen(40, 20)
    pyte.Stream(screen).feed("\r\n".join(lines))
    return screen


def _hex(colour: str) -> str:
    return SPEC_COLOUR[colour][1:].lower()


def test_line_and_cell_counts() -> None:
    lines = mascot_lines(colour=True, unicode=True)
    assert len(lines) == 14
    screen = _screen(lines)
    for y in range(14):
        inked = [x for x, cell in screen.buffer[y].items() if cell.data.strip()]
        assert max(inked) <= 19


def test_every_cell_colour_pair_matches_map_on_one_screen() -> None:
    screen = _screen(mascot_lines(colour=True, unicode=True))
    for y in range(14):
        top, bottom = SPEC_MAP[2 * y], SPEC_MAP[2 * y + 1]
        for x in range(20):
            t, b = top[x], bottom[x]
            cell = screen.buffer[y][x]
            where = (y, x, t, b)
            if t == "." and b == ".":
                assert (cell.data, cell.fg, cell.bg) == (" ", "default", "default"), (
                    where
                )
            elif t == ".":
                assert (cell.data, cell.fg, cell.bg) == ("▄", _hex(b), "default"), where
            elif b == ".":
                assert (cell.data, cell.fg, cell.bg) == ("▀", _hex(t), "default"), where
            else:
                assert (cell.data, cell.fg, cell.bg) == ("▀", _hex(t), _hex(b)), where


def test_each_line_is_independent_of_the_one_before() -> None:
    """Colour carried across lines must not change a line: render all lines, then each alone."""
    lines = mascot_lines(colour=True, unicode=True)
    together = _screen(lines)
    for y, line in enumerate(lines):
        alone = _screen([line])
        for x in range(20):
            a, b = together.buffer[y][x], alone.buffer[0][x]
            assert (a.data, a.fg, a.bg) == (b.data, b.fg, b.bg), (y, x)


def test_mirror_symmetry_on_screen() -> None:
    screen = _screen(mascot_lines(colour=True, unicode=True))
    for y in range(14):
        top, bottom = SPEC_MAP[2 * y], SPEC_MAP[2 * y + 1]
        for x in range(10):
            m = 19 - x
            if (top[x], bottom[x]) == (top[m], bottom[m]):
                a, b = screen.buffer[y][x], screen.buffer[y][m]
                assert (a.data, a.fg, a.bg) == (b.data, b.fg, b.bg), (y, x)


@pytest.mark.parametrize("no_color", [True, False])
def test_no_escape_codes_when_off(
    monkeypatch: pytest.MonkeyPatch, no_color: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
        out = Tty()
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
        out = NonTty()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["look"]) == 0
    assert "\x1b" not in out.getvalue()
    assert out.getvalue().strip()
    capsys.readouterr()


def test_cp1252_stream_gets_ascii_and_nothing_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    raw = io.BytesIO()

    class Cp1252Tty(io.TextIOWrapper):
        def isatty(self) -> bool:
            return True

    out = Cp1252Tty(raw, encoding="cp1252", errors="strict")
    assert unicode_ok(out) is False
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["look"]) == 0
    out.flush()
    text = raw.getvalue().decode("cp1252", errors="replace")
    assert "▀" not in text
    assert "?" not in text
