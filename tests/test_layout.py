import importlib

import pytest

COMPONENTS = [
    "mcp",
    "engine",
    "runner",
    "agents",
    "broker",
    "store",
    "git",
    "intake",
    "jira",
    "maintain",
    "eval",
    "bin",
]


@pytest.mark.parametrize("name", COMPONENTS)
def test_component_imports(name: str) -> None:
    module = importlib.import_module(f"ultron.{name}")
    assert module.__name__ == f"ultron.{name}"
