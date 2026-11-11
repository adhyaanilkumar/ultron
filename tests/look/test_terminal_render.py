"""AC6 and AC7: render `ultron look` through a terminal emulator (pyte), colour state carried across cells."""

import io
import sys

import pyte
import pytest
from test_look import SPEC_COLOUR, SPEC_MAP

from ultron import cli
from ultron.look.render import marker, mascot_lines, wordmark

COLS, ROWS = 80, 24


class _Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


def look_screen(monkeypatch: pytest.MonkeyPatch, tail: str = "") -> pyte.Screen:
    """Run `ultron look` with a coloured TTY, then feed `tail` and return the emulated screen."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = _Tty()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["look"]) == 0
    screen = pyte.Screen(COLS, ROWS)
    pyte.Stream(screen).feed((out.getvalue() + tail).replace("\n", "\r\n"))
    return screen


def test_transparent_cells_have_default_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = look_screen(monkeypatch)
    for line in range(14):
        top, bottom = SPEC_MAP[2 * line], SPEC_MAP[2 * line + 1]
        for col in range(20):
            t, b = top[col], bottom[col]
            cell = screen.buffer[line][col]
            if t == "." and b == ".":
                assert (cell.data, cell.bg) == (" ", "default"), (line, col)
            elif t == "." or b == ".":
                assert cell.bg == "default", (line, col)
            else:
                assert cell.bg == SPEC_COLOUR[b][1:].lower(), (line, col)


def test_mascot_mirror_symmetric_where_map_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = look_screen(monkeypatch)
    checked = 0
    for line in range(14):
        top, bottom = SPEC_MAP[2 * line], SPEC_MAP[2 * line + 1]
        for col in range(10):
            mirror = 19 - col
            if (top[col], bottom[col]) != (top[mirror], bottom[mirror]):
                continue
            a, b = screen.buffer[line][col], screen.buffer[line][mirror]
            assert (a.data, a.fg, a.bg) == (b.data, b.fg, b.bg), (line, col)
            checked += 1
    assert checked > 100


def test_lines_end_with_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = look_screen(monkeypatch, tail="after")
    for line in range(14):
        cell = screen.buffer[line][20]
        assert (cell.fg, cell.bg) == ("default", "default"), line
    # Text printed after the whole output has default colours too.
    after = screen.buffer[screen.cursor.y][0]
    assert (after.data, after.fg, after.bg, after.bold) == (
        "a",
        "default",
        "default",
        False,
    )


def test_wordmark_and_marker_do_not_bleed() -> None:
    for text in (wordmark(colour=True), marker("ok", True, True)):
        screen = pyte.Screen(40, 2)
        pyte.Stream(screen).feed(text + " x")
        last = max(screen.buffer[0])
        assert screen.buffer[0][last].fg == "default"
        assert screen.buffer[0][last].bg == "default"


def test_mascot_lines_still_reset_each_line() -> None:
    assert all(line.endswith("\x1b[0m") for line in mascot_lines(True, True))
