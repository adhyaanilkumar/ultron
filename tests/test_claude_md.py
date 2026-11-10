from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text() -> str:
    return (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_status_line_updated() -> None:
    assert "pre-implementation" not in _text()


def test_commands_section_lists_targets_and_success_lines() -> None:
    text = _text()
    assert "## Commands" in text
    section = text.split("## Commands", 1)[1].split("\n## ", 1)[0]
    for target, success in (
        ("make build", "Build succeeded"),
        ("make lint", "Lint succeeded"),
        ("make test", "All tests passed"),
    ):
        assert target in section
        assert success in section
