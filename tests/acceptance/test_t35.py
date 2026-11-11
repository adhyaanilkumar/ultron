"""Acceptance tests for T35 (AC1-AC5), written independently of the developer."""

import io
import os
import re
import subprocess
from pathlib import Path

import pyte
import pytest

from ultron.look import palette, render

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "sdlc" / "v0" / "spec.md").read_text(encoding="utf-8")
SECTION = SPEC[SPEC.index("## 18. Look") : SPEC.index("## 19.")]
UV_DIR = r"C:\Users\Anil Kumar ML\.local\bin"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _map() -> list[str]:
    block = re.search(r"```\n((?:[.A-Z]{20}\n){28})```", SECTION)
    assert block, "cell map not found"
    rows = block.group(1).split("\n")[:28]
    return rows


def _codes() -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(
        r"\| ((?:`\w`(?:, )?)+) \| [^|]+ \| [^|`]*`(#[0-9A-Fa-f]{6})`", SECTION
    ):
        for c in re.findall(r"`(\w)`", m.group(1)):
            out[c] = m.group(2).lower()
    return out


def _screen(text: str, cols: int = 80, rows: int = 40) -> pyte.Screen:
    """Stateful rendering: colour state carries across cells and lines (lesson L1)."""
    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed(text.replace("\n", "\r\n"))
    return screen


def _mascot_screen(tail: str = "") -> pyte.Screen:
    lines = render.mascot_lines(colour=True, unicode=True)
    return _screen("\n".join(lines) + "\n" + tail)


def test_ac1_mascot_cells_match_map() -> None:
    rows = _map()
    codes = _codes()
    assert set(codes) == set("HCSLEF")
    assert len(render.mascot_lines(colour=True, unicode=True)) == 14
    screen = _mascot_screen()
    up, down = "▀", "▄"
    for i in range(14):
        assert screen.buffer[i][20].data == " "
        top, bottom = rows[2 * i], rows[2 * i + 1]
        for x in range(20):
            t, b = top[x], bottom[x]
            c = screen.buffer[i][x]
            where = (i, x, t, b, c.data, c.fg, c.bg)
            if t == "." and b == ".":
                assert c.data == " " and c.bg == "default", where
            elif t != "." and b != ".":
                assert c.data == up, where
                assert c.fg == codes[t][1:], where
                assert c.bg == codes[b][1:], where
            elif b == ".":
                assert c.data == up and c.fg == codes[t][1:], where
                assert c.bg == "default", where
            else:
                assert c.data == down and c.fg == codes[b][1:], where
                assert c.bg == "default", where


def test_ac6_transparent_cells_default_background_and_mirror() -> None:
    rows = _map()
    screen = _mascot_screen()
    checked = 0
    for i in range(14):
        top, bottom = rows[2 * i], rows[2 * i + 1]
        for x in range(20):
            if top[x] == "." and bottom[x] == ".":
                c = screen.buffer[i][x]
                assert (c.bg, c.fg) == ("default", "default"), (i, x)
        for x in range(10):
            m = 19 - x
            if (top[x], bottom[x]) == (top[m], bottom[m]):
                a, b = screen.buffer[i][x], screen.buffer[i][m]
                assert (a.data, a.fg, a.bg) == (b.data, b.fg, b.bg), (i, x, m)
                checked += 1
    assert checked > 100


def test_ac7_reset_after_mascot_and_wordmark() -> None:
    screen = _mascot_screen(tail="after")
    for i in range(14):
        c = screen.buffer[i][20]
        assert (c.fg, c.bg) == ("default", "default"), i
    for c in (screen.buffer[14][0], screen.buffer[14][4]):
        assert c.data in "after" and (c.fg, c.bg) == ("default", "default")
    ws = _screen(render.wordmark(colour=True) + "\nnext")
    assert "".join(ws.buffer[0][x].data for x in range(11)) == "U L T R O N"
    u = ws.buffer[0][0]
    assert u.bold and u.fg == "e5484d"
    end = ws.buffer[0][11]
    assert (end.fg, end.bg, end.bold) == ("default", "default", False)
    n = ws.buffer[1][0]
    assert n.data == "n" and (n.fg, n.bg, n.bold) == ("default", "default", False)


def test_look_command_output_through_pyte_when_coloured() -> None:
    """Run the hidden command on a faked TTY and render its real output."""
    import sys

    from ultron import cli

    class Tty(io.StringIO):
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    out = Tty()
    old = sys.stdout
    os.environ.pop("NO_COLOR", None)
    sys.stdout = out
    try:
        assert cli.main(["look"]) == 0
    finally:
        sys.stdout = old
    screen = _screen(out.getvalue() + "tail")
    rows = _map()
    codes = _codes()
    for i in range(14):
        for x in range(20):
            t, b = rows[2 * i][x], rows[2 * i + 1][x]
            c = screen.buffer[i][x]
            if t == "." and b == ".":
                assert c.bg == "default", (i, x)
            elif t != "." and b != ".":
                assert (c.fg, c.bg) == (codes[t][1:], codes[b][1:]), (i, x)


def test_ac1_plain_mascot_is_14_lines_of_20() -> None:
    for uni in (True, False):
        lines = render.mascot_lines(colour=False, unicode=uni)
        assert len(lines) == 14
        assert all(len(ln) == 20 for ln in lines)


def test_ac2_no_escape_codes_without_colour() -> None:
    for uni in (True, False):
        out = "\n".join(render.mascot_lines(colour=False, unicode=uni))
        out += render.wordmark(colour=False)
        for name in ("ok", "waiting", "failed", "running"):
            out += render.marker(name, colour=False, unicode=uni)
        assert "\x1b" not in out


def test_ac2_colour_enabled_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert render.colour_enabled(Tty()) is True
    assert render.colour_enabled(io.StringIO()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert render.colour_enabled(Tty()) is False


def test_ac2_look_command_piped_has_no_escapes() -> None:
    env = dict(os.environ)
    env.pop("UV", None)
    env["PATH"] = UV_DIR + os.pathsep + env.get("PATH", "")
    env["NO_COLOR"] = "1"
    r = subprocess.run(
        ["uv", "run", "ultron", "look"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr
    assert b"\x1b" not in r.stdout
    assert r.stdout.strip()


def test_ac3_ascii_fallback_when_encoding_cannot_print() -> None:
    class Ascii(io.StringIO):
        encoding = "ascii"

    class Cp1252(io.StringIO):
        encoding = "cp1252"

    for s in (Ascii(), Cp1252()):
        assert render.unicode_ok(s) is False
    for colour in (False, True):
        lines = render.mascot_lines(colour=colour, unicode=False)
        assert len(lines) == 14
        text = ANSI.sub("", "\n".join(lines))
        text.encode("ascii")
        assert "▀" not in text and "▄" not in text
        for name in ("ok", "waiting", "failed", "running"):
            ANSI.sub("", render.marker(name, colour=colour, unicode=False)).encode(
                "ascii"
            )
    assert render.unicode_ok(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) is True


def test_ac4_palette_exposes_all_spec_hex_values() -> None:
    spec_hex = {h.lower() for h in re.findall(r"`(#[0-9A-Fa-f]{6})`", SECTION)}
    assert len(spec_hex) >= 10
    exposed = {
        v.lower()
        for k, v in vars(palette).items()
        if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v)
    }
    assert spec_hex <= exposed, spec_hex - exposed
    assert {v.lower() for v in palette.CELL_COLOURS.values()} == {
        h for h in _codes().values()
    }


def test_wordmark_bold_crimson_and_markers_coloured() -> None:
    w = render.wordmark(colour=True)
    assert "\x1b[1m" in w and "229;72;77" in w
    assert ANSI.sub("", w) == "U L T R O N"
    expect = {
        "ok": ("✔", "63;185;80"),
        "waiting": ("⏳", "227;179;65"),
        "failed": ("✖", "148;18;18"),
        "running": ("●", "217;119;87"),
    }
    for name, (glyph, rgb) in expect.items():
        m = render.marker(name, colour=True, unicode=True)
        assert glyph in m and rgb in m


def test_ac5_owner_look_command_exits_zero() -> None:
    """[owner] visual check pending; here only that the command runs and exits 0."""
    env = dict(os.environ)
    env.pop("UV", None)
    env["PATH"] = UV_DIR + os.pathsep + env.get("PATH", "")
    r = subprocess.run(
        ["uv", "run", "ultron", "look"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
    )
    assert r.returncode == 0
