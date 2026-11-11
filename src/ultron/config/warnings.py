"""Warning objects returned by the config loader (spec section 8, "Warning")."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigWarning:
    """A default-human gate switched to auto. The caller records it."""

    gate: str
    from_value: str
    to_value: str
    message: str
