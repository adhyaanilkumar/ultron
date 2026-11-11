"""T15 AC5: the interface module imports nothing from any adapter."""

import ast
import sys
from pathlib import Path

from ultron.agents import interface


def test_interface_imports_only_the_standard_library() -> None:
    tree = ast.parse(Path(interface.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            roots.add((node.module or "").split(".")[0])
    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)
