"""Acceptance tests for T35 (AC1-AC5), written independently of the developer."""

import io
import os
import re
import subprocess
from pathlib import Path

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


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _cells(
    line: str,
) -> list[tuple[str, tuple[int, int, int] | None, tuple[int, int, int] | None]]:
    fg = bg = None
    cells = []
    pos = 0
    for m in ANSI.finditer(line):
        for ch in line[pos : m.start()]:
            cells.append((ch, fg, bg))
        params = m.group(0)[2:-1]
        nums = [int(x) for x in params.split(";") if x != ""] or [0]
        i = 0
        while i < len(nums):
            n = nums[i]
            if n == 0:
                fg = bg = None
            elif n == 38:
                fg = (nums[i + 2], nums[i + 3], nums[i + 4])
                i += 4
            elif n == 48:
                bg = (nums[i + 2], nums[i + 3], nums[i + 4])
                i += 4
            elif n == 39:
                fg = None
            elif n == 49:
                bg = None
            i += 1
        pos = m.end()
    for ch in line[pos:]:
        cells.append((ch, fg, bg))
    return cells


def test_ac1_mascot_cells_match_map() -> None:
    rows = _map()
    codes = _codes()
    assert set(codes) == set("HCSLEF")
    lines = render.mascot_lines(colour=True, unicode=True)
    assert len(lines) == 14
    for i, line in enumerate(lines):
        cells = _cells(line)
        assert len(cells) == 20, (i, len(cells))
        top, bottom = rows[2 * i], rows[2 * i + 1]
        for x, (ch, fg, bg) in enumerate(cells):
            t, b = top[x], bottom[x]
            where = (i, x, t, b, ch, fg, bg)
            if t == "." and b == ".":
                assert ch == " ", where
            elif t != "." and b != ".":
                assert ch == "▀", where
                assert fg == _hex_to_rgb(codes[t]), where
                assert bg == _hex_to_rgb(codes[b]), where
            elif b == ".":
                assert ch == "▀" and fg == _hex_to_rgb(codes[t]) and bg is None, where
            else:
                assert ch == "▄" and fg == _hex_to_rgb(codes[b]) and bg is None, where


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
