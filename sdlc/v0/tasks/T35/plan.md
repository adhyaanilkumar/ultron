# T35 plan: terminal look (mascot, wordmark, palette)

Rung: R1 (rerun after the tasks.md revision; the original score was R0, total 2). Files 1 (5 files, see below), Change type 0 (new; one small edit to `cli.py`), Risk 0 (cosmetic),
Ambiguity 0 (cell map and colours are given), Algorithmic 1 (pairing rows into half-blocks), Test surface 0.
If the reviewer scores the `cli.py` edit as "edits existing", the total is 3 and the rung becomes R1. I'd accept that.

## Files that change
New:
- `src/ultron/look/__init__.py`: docstring only.
- `src/ultron/look/palette.py`: every §18 colour as a named constant with its hex value, and the cell-code to colour map
  (`H`, `C`/`S`, `L`, `E`, `F`).
- `src/ultron/look/render.py`: the 20×28 cell map copied from §18, half-block pairing, ASCII fallback, wordmark, markers,
  `colour_enabled(stream)`, `unicode_ok(stream)`, `force_utf8()`.
- `tests/look/test_look.py`: all proofs below.
Edited:
- `src/ultron/cli.py`: hidden `look` command. It calls `force_utf8()` on Windows and prints
  the mascot, wordmark and marker sample. `--version` keeps working.
  Departure from the plan: `look` is matched on `argv == ["look"]` before argparse, not as an argparse subparser with
  `help=argparse.SUPPRESS`. argparse still shows `{look}` in the usage line, so `--help` would reveal it.
Not touched: gate and runner call sites (T38, T46 use this API later).

## API for later callers
- `mascot_lines(colour: bool, unicode: bool) -> list[str]`: always 14 lines.
- `wordmark(colour: bool) -> str`, `marker(name, colour, unicode) -> str`, `paint(text, fg, bold=False) -> str`.
- `colour_enabled(stream) -> bool`: False if `NO_COLOR` is set to a non-empty value, or the stream isn't a TTY.
- `unicode_ok(stream) -> bool`: tries to encode every glyph used (`▀ ▄ ✔ ⏳ ✖ ●`) with `stream.encoding`. False on
  `UnicodeEncodeError` or a missing encoding.

## Rerun after the tasks.md revision (2026-11-11)
The owner sent T35 back to decomposition. The existing code is the starting point and is kept. Attempt 1 had the colour
bleed (a transparent cell printed a space under the previous cell's background). Attempt 2 fixed it in `render.py`
(a reset before a blank cell, `\x1b[49m` before half-blocks with a transparent half), but tests decoded cells in
isolation or only by hand. This rerun proves it through `pyte` (lesson L1).

### Files that change
- `tests/look/test_mascot.py` (new): the file `tasks.md` names for AC1 to AC3. Holds the `pyte` screen checks and the
  line/cell counts.
- `tests/look/test_terminal_render.py`: already has the `pyte` checks for AC6 and AC7; kept, tightened if a gap shows.
- `tests/look/test_look.py`: kept (AC4 palette, wordmark, markers, hidden command).
- `src/ultron/look/render.py`, `src/ultron/cli.py`: change only if a new test finds a defect.
Not touched: `tests/acceptance/`, `tasks.md`, `spec.md`, `decisions.md`.

### Order of work
1. Write `tests/look/test_mascot.py` first: feed the coloured mascot through one `pyte.Screen` and compare every cell's
   glyph, fg and bg with the map pair; a mirror check per line; counts; `NO_COLOR` and non-TTY; cp1252.
2. Run it. If red, fix `render.py` (never the test).
3. `make build`, `make lint`, `make test`.
4. Owner steps (AC5, AC8) are listed, not run.

### Proof per criterion
| AC | verify_with | Proof |
|---|---|---|
| AC1 | `terminal-render` + `pytest` | `test_mascot.py`: one `pyte` screen, colour carried across cells and lines. For all 14x20 cells the glyph, fg and bg match the pair from map rows 2i and 2i+1 (a transparent half means the default bg). Also 14 lines of 20 cells. |
| AC2 | `pytest` + `command` | `test_mascot.py`: `NO_COLOR=1` on a TTY stream, and a non-TTY stream: no `\x1b`. `command`: `uv run ultron look` piped, with and without `NO_COLOR`, has no ESC byte; I run this and check. |
| AC3 | `pytest` | `test_mascot.py`: stream with `cp1252`: ASCII mascot, no exception. |
| AC4 | `pytest` | `test_look.py::test_palette_hex` (kept). |
| AC5 | `owner` + `screenshot-terminal` | Owner step. Not run by me. |
| AC6 | `terminal-render` | `test_terminal_render.py`: transparent cells have default bg; mirror compare per line. |
| AC7 | `terminal-render` | `test_terminal_render.py`: the cell after each line and the text after the output have default colours. |
| AC8 | `owner` + `screenshot-terminal` | Owner step in a PowerShell 5.1 window. Not run by me. The code path is `force_utf8` plus the ASCII fallback; automation covers cp1252 only. |

`mcp` (Claude in Chrome screenshot of a `pyte` HTML grid) is the testing agent's tool, not the developer's.
## Original order of work (attempt 1, kept for history)
1. Write `tests/look/test_look.py` first, with the §18 map, the hex values and the expected cell pairs copied as literals.
   Run `make test`. Expect red (ImportError).
2. `palette.py`: constants and the code-to-colour map.
3. `render.py`:
   - Pairing: line i uses map rows 2i (top) and 2i+1 (bottom). Both coloured: `▀`, fg=top, bg=bottom. Top transparent
     (`.`): `▄`, fg=bottom. Bottom transparent: `▀`, fg=top, no bg. Both transparent: a space, no colour.
   - Colour: 24-bit SGR (`\x1b[38;2;r;g;bm`, `\x1b[48;2;r;g;bm`), reset `\x1b[0m` at the end of each line.
   - Wordmark `U L T R O N`: bold (`\x1b[1m`) crimson `#E5484D`.
   - ASCII fallback: one char per pair, taking the highest-priority code of the two rows (E > L > C > F > H > S > `.`).
     Chars: E `@`, L `*`, C `+`, F `:`, H `#`, S `|`, `.` space. Markers: `+` ok, `~` waiting, `x` failed, `*` running.
4. `cli.py`: add the hidden `look` subcommand. `force_utf8()` reconfigures stdout to UTF-8 on win32 inside try/except. On
   win32 with a console, it also turns on virtual-terminal processing via ctypes (see risk 1).
5. Run `uv run ultron look > out.txt` (no colour) and check the output by eye. Then `make build`, `make lint`, `make test`.
   Paste the success lines.
6. [owner] AC5: `uv run ultron look` in Windows Terminal.

## Original proof per AC
| AC | Proof |
|---|---|
| AC1 | `test_look.py::test_mascot_cells`: render with colour on and unicode on. Assert 14 lines of 20 cells. For each cell, decode the glyph and (fg, bg), and compare with the pair from map rows 2i and 2i+1. |
| AC2 | `test_look.py::test_no_ansi`: with `NO_COLOR=1` (monkeypatched) and a TTY-faking stream, and with a plain `StringIO`, the output has no `\x1b`. |
| AC3 | `test_look.py::test_ascii_fallback`: a `TextIOWrapper(BytesIO, encoding="cp1252")` gives `unicode_ok` False. The mascot is the 14×20 ASCII set, all chars ASCII, no exception. |
| AC4 | `test_look.py::test_palette_hex`: palette name to hex equals the literal list from §18 (every row of both tables). |
| AC5 | [owner] `uv run ultron look` in Windows Terminal: shape, colours and wordmark look right. |
| Extra | `test_look.py::test_wordmark_and_markers` (wordmark text and SGR; markers plain in no-colour mode). `force_utf8` on a non-Windows stream doesn't raise. |

Lint: `ruff check`, `ruff format --check`, `mypy --strict` on `src/ultron/look` and `cli.py`. Build: `uv build` with the new package.

## Risks
1. **Classic console (PowerShell 5.1 window).** It doesn't process VT sequences unless virtual-terminal mode is enabled.
   The plan enables it with ctypes on win32 when stdout is a console. If that fails, colour is off. The AC5 check is in
   Windows Terminal only. T03 covers the classic console.
2. **Spec is silent on several points,** so these are my choices, to confirm at the plan gate: transparent `.` pixels
   (no colour, space when both rows are transparent); the ASCII mascot and marker characters; `NO_COLOR` as any non-empty
   value (no-color.org) rather than only `=1`.
3. **Wide glyphs.** ⏳ (and sometimes ✔) take two cells in some fonts and can shift the columns of a line. Automation can't
   catch this. It's a visual check under AC5.
4. **Half-block seams.** Whether `▀`/`▄` leave hairline gaps depends on the font and the terminal. The owner's AC5 check is
   the only proof.
5. **`reconfigure` can fail** on streams that aren't a `TextIOWrapper` (for example pytest's capture). That is inside
   try/except, and `unicode_ok` then decides the output.
6. **Hidden command.** `look` must stay out of `--help`. The test confirms it's absent from the help text and that
   `ultron look` still runs.
