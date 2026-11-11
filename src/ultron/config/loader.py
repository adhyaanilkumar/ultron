"""Load ultron.toml, merge a per-requirement override, validate once."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultron.config import defaults as d
from ultron.config.models import Config
from ultron.config.warnings import ConfigWarning


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    warnings: list[ConfigWarning]


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


def gate_warnings(config: Config) -> list[ConfigWarning]:
    found: list[ConfigWarning] = []
    for gate in d.DEFAULT_HUMAN_GATES:
        if config.gates.value(gate) == "auto":
            found.append(
                ConfigWarning(
                    gate=gate,
                    from_value="human",
                    to_value="auto",
                    message=f"gate '{gate}' is human by default and is now auto",
                )
            )
    return found


def build_config(
    raw: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    *,
    windows: bool | None = None,
    home: Path | None = None,
) -> LoadedConfig:
    data = deep_merge(raw, overrides or {})
    paths = dict(data.get("paths") or {})
    paths.setdefault("work_folder", d.default_work_folder(windows, home))
    paths.setdefault("ultron_home", d.default_ultron_home(windows, home))
    data["paths"] = paths
    config = Config.model_validate(data)
    return LoadedConfig(config, gate_warnings(config))


def load_config(
    path: str | Path,
    overrides: dict[str, Any] | None = None,
    *,
    windows: bool | None = None,
    home: Path | None = None,
) -> LoadedConfig:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return build_config(raw, overrides, windows=windows, home=home)
