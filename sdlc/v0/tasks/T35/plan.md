# T35 plan: terminal look (mascot, wordmark, palette)

Rung: R0 (total 2). Files 1 (5 files, see below), Change type 0 (new; one small edit to `cli.py`), Risk 0 (cosmetic),
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

## Order of work
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

## Proof per AC
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
