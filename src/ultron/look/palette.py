"""Colours from spec section 18, as hex strings."""

from typing import Final

# Mascot parts.
GUNMETAL: Final = "#7A828A"  # H: head
DARK_GUNMETAL: Final = "#5A6168"  # C, S: creases and side fins
SHINE: Final = "#b3b3b3"  # L
EYE: Final = "#941212"  # E
GAP: Final = "#262626"  # F: mouth and cheek gaps

# Text.
CRIMSON: Final = "#E5484D"  # wordmark, bold
MAIN_TEXT: Final = "#c9c9c9"
EMPHASIS: Final = "#f2f2f2"
DIM_TEXT: Final = "#7A828A"
FAILURE: Final = "#941212"  # the cross only; the words stay EMPHASIS
RUNNING: Final = "#D97757"  # Claude orange
APPROVED: Final = "#3FB950"
WAITING: Final = "#E3B341"

# Mascot cell code to colour. "." is transparent and has no entry.
CELL_COLOURS: Final[dict[str, str]] = {
    "H": GUNMETAL,
    "C": DARK_GUNMETAL,
    "S": DARK_GUNMETAL,
    "L": SHINE,
    "E": EYE,
    "F": GAP,
}
