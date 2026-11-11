"""Render the ultron mascot, wordmark and markers for a terminal, with colour and ASCII fallbacks."""

import io
import os
import sys
from typing import Final, TextIO

from ultron.look import palette

# Spec section 18 cell map, 20 columns by 28 rows. '.' is transparent.
MASCOT_MAP: Final[tuple[str, ...]] = (
    ".......HHHHHH.......",
    ".....HHHLLLLHHH.....",
    "....HHHHHHHHHHHH....",
    "...HHHHHHHHHHHHHH...",
    "..HCHHHHHHHHHHHHCH..",
    "S.HHCHHHHHHHHHHCHH.S",
    "S.HHCHHHHHHHHHHCHH.S",
    "S.HHCHHHHHHHHHHCHH.S",
    "S.HHCHHHHHHHHHHCHH.S",
    "S.HHCHHHHHHHHHHCHH.S",
    "S.HCHHHHHHHHHHHHCH.S",
    "S.HHHHHHHHHHHHHHHH.S",
    "HHHEHHHHHHHHHHHHEHHH",
    "HHHHEEHHHHHHHHEEHHHH",
    "HHHHEEEEHHHHEEEEHHHH",
    "HHHHHHEEEHHEEEHHHHHH",
    "HHHHHHHHCHHCHHHHHHHH",
    ".HHHHFHHCHHCHHFHHHH.",
    ".HFHFFFHHHHHHFFFHFH.",
    ".HFHFFFFHHHHFFFFHFH.",
    "..HHFFFFHFFHFFFFHH..",
    "...HHFFFFFFFFFFHH...",
    "....HFFFHFFHFFFH....",
    "....HHFFHHHHFFHH....",
    ".....HHHHHHHHHH.....",
    "......HHHHHHHH......",
    ".......HHHHHH.......",
    "........HHHH........",
)

# ASCII fallback: one character per pair of rows, highest priority code wins.
_ASCII_PRIORITY: Final = "ELCFHS."
_ASCII_CHAR: Final[dict[str, str]] = {
    "E": "@",
    "L": "*",
    "C": "+",
    "F": ":",
    "H": "#",
    "S": "|",
    ".": " ",
}

# Markers: glyph and ASCII stand-in, with colour.
_MARKERS: Final[dict[str, tuple[str, str, str]]] = {
    "ok": ("✔", "+", palette.APPROVED),
    "waiting": ("⏳", "~", palette.WAITING),
    "failed": ("✖", "x", palette.FAILURE),
    "running": ("●", "*", palette.RUNNING),
}

_WORDMARK: Final = "U L T R O N"
_UNICODE_GLYPHS: Final = "▀▄✔⏳✖●"
_RESET: Final = "\x1b[0m"
_DEFAULT_BG: Final = "\x1b[49m"


def _fg(hex_colour: str) -> str:
    r, g, b = _rgb(hex_colour)
    return f"\x1b[38;2;{r};{g};{b}m"


def _bg(hex_colour: str) -> str:
    r, g, b = _rgb(hex_colour)
    return f"\x1b[48;2;{r};{g};{b}m"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def paint(text: str, fg: str, bold: bool = False) -> str:
    """Wrap text in a foreground colour (and bold, if asked). Callers decide whether colour is on."""
    prefix = "\x1b[1m" if bold else ""
    return f"{prefix}{_fg(fg)}{text}{_RESET}"


def _unicode_line(top: str, bottom: str, colour: bool) -> str:
    parts: list[str] = []
    for t, b in zip(top, bottom, strict=True):
        if t == "." and b == ".":
            # Reset first: a bare space would take the previous cell's background.
            parts.append(f"{_RESET} " if colour else " ")
        elif not colour:
            # Plain mode: draw the shape only, so the pixels still read as a block.
            parts.append("▄" if t == "." else "▀")
        elif t != "." and b != ".":
            parts.append(
                f"{_fg(palette.CELL_COLOURS[t])}{_bg(palette.CELL_COLOURS[b])}▀"
            )
        elif t == ".":
            parts.append(f"{_fg(palette.CELL_COLOURS[b])}{_DEFAULT_BG}▄")
        else:
            parts.append(f"{_fg(palette.CELL_COLOURS[t])}{_DEFAULT_BG}▀")
    line = "".join(parts)
    return line + _RESET if colour else line


def _ascii_line(top: str, bottom: str, colour: bool) -> str:
    parts: list[str] = []
    for t, b in zip(top, bottom, strict=True):
        code = next(c for c in _ASCII_PRIORITY if c in (t, b))
        char = _ASCII_CHAR[code]
        if colour and code != ".":
            parts.append(f"{_fg(palette.CELL_COLOURS[code])}{char}")
        else:
            parts.append(char)
    line = "".join(parts)
    return line + _RESET if colour else line


def mascot_lines(colour: bool, unicode: bool) -> list[str]:
    """Return the mascot as 14 terminal lines of 20 cells each."""
    lines: list[str] = []
    for i in range(14):
        top, bottom = MASCOT_MAP[2 * i], MASCOT_MAP[2 * i + 1]
        if unicode:
            lines.append(_unicode_line(top, bottom, colour))
        else:
            lines.append(_ascii_line(top, bottom, colour))
    return lines


def wordmark(colour: bool) -> str:
    """Return the U L T R O N wordmark, bold crimson when colour is on."""
    if not colour:
        return _WORDMARK
    return paint(_WORDMARK, palette.CRIMSON, bold=True)


def marker(name: str, colour: bool, unicode: bool) -> str:
    """Return a status marker: ok, waiting, failed or running."""
    if name not in _MARKERS:
        raise ValueError(f"unknown marker: {name}")
    glyph, ascii_glyph, fg = _MARKERS[name]
    chosen = glyph if unicode else ascii_glyph
    return paint(chosen, fg) if colour else chosen


def colour_enabled(stream: TextIO) -> bool:
    """True when colour should be printed: NO_COLOR is unset or empty, and the stream is a terminal."""
    if os.environ.get("NO_COLOR", ""):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty is not None and isatty())


def unicode_ok(stream: TextIO) -> bool:
    """True when the stream's encoding can print every glyph the look uses."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        _UNICODE_GLYPHS.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def force_utf8() -> None:
    """On Windows, switch stdout to UTF-8 and, on a console, turn on virtual-terminal colour.

    Failures are ignored: unicode_ok and colour_enabled then decide the output.
    """
    if sys.platform != "win32":
        return
    stdout = sys.stdout
    try:
        if isinstance(stdout, io.TextIOWrapper):
            stdout.reconfigure(encoding="utf-8")
        if stdout.isatty():
            _enable_virtual_terminal()
    except (AttributeError, ValueError, OSError):
        pass


def _enable_virtual_terminal() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    std_output_handle = -11
    enable_vt = 0x0004
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(std_output_handle)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | enable_vt)
