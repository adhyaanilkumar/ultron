"""Requirement ids: `R-<yyyymmdd>-<4 hex>`, for example `R-20261109-a3f2` (spec §7.2).

The date is the local date. The id is also the name of the requirement's folder, so a candidate is checked
against the folders that already exist and a clash draws a new one.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

REQUIREMENT_ID_RE = re.compile(r"R-[0-9]{8}-[0-9a-f]{4}")
"""Match with `fullmatch`. Digits are ASCII only, hex is lower case."""

_TOKEN_RE = re.compile(r"[0-9a-f]{4}")
DEFAULT_TRIES = 1000


def _random_token() -> str:
    return secrets.token_hex(2)


def new_requirement_id(
    requirements_dir: Path,
    *,
    now: datetime | None = None,
    token: Callable[[], str] = _random_token,
    tries: int = DEFAULT_TRIES,
) -> str:
    """Return a new id that is not already an entry in `requirements_dir`.

    `now` (default: the local time) and `token` (default: 4 random hex digits) can be given for tests.
    Raises `RuntimeError` when `tries` candidates in a row are taken, and `ValueError` when `token` isn't 4 hex
    digits.
    """
    day = (now if now is not None else datetime.now()).strftime("%Y%m%d")
    for _ in range(tries):
        suffix = token()
        if _TOKEN_RE.fullmatch(suffix) is None:
            raise ValueError(f"token must be 4 lower-case hex digits, got {suffix!r}")
        candidate = f"R-{day}-{suffix}"
        if not (requirements_dir / candidate).exists():
            return candidate
    raise RuntimeError(
        f"no free requirement id for R-{day} in {requirements_dir} after {tries} tries"
    )
