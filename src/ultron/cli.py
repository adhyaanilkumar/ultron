"""The `ultron` console entry point."""

import argparse
import sys

from ultron import __version__
from ultron.look.render import (
    colour_enabled,
    force_utf8,
    marker,
    mascot_lines,
    unicode_ok,
    wordmark,
)


def _look() -> int:
    """Hidden preview: print the mascot, the wordmark and the markers."""
    force_utf8()
    colour = colour_enabled(sys.stdout)
    unicode = unicode_ok(sys.stdout)
    for line in mascot_lines(colour=colour, unicode=unicode):
        print(line)
    print()
    print(wordmark(colour=colour))
    print(
        f"{marker('ok', colour, unicode)} approved  "
        f"{marker('waiting', colour, unicode)} waiting on you  "
        f"{marker('failed', colour, unicode)} failed  "
        f"{marker('running', colour, unicode)} running"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["look"]:
        # Hidden on purpose: not listed in --help.
        return _look()
    parser = argparse.ArgumentParser(prog="ultron")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
