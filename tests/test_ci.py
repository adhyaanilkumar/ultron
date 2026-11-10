from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_runs_on_pull_request() -> None:
    workflow = _workflow()
    # YAML 1.1 parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert "pull_request" in triggers


def test_matrix_covers_windows_and_linux() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    oses: set[str] = set()
    for job in jobs.values():
        matrix = job["strategy"]["matrix"]
        oses.update(matrix["os"])
    assert {"windows-latest", "ubuntu-latest"} <= oses


def test_runs_build_lint_and_test() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    runs: list[str] = []
    for job in jobs.values():
        for step in job["steps"]:
            if "run" in step:
                runs.append(step["run"].strip())
    for target in ("make build", "make lint", "make test"):
        assert any(r == target for r in runs), f"missing step: {target}"
