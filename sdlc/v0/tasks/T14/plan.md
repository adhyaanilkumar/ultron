# T14 plan: release version proposal and bumps

Task: spec §13, D12. Pure functions for `major.minor.patch` versions. Rung R0 (rubric total 0, see tasks.md T14).

## Files that change
New:
- `src/ultron/engine/version.py`: the module (`parse_version`, `format_version`, `bump`, `current_version`, `release_bump`, `default_bump`, `Bump` type). The `engine` package already exists from T01.
- `tests/engine/test_version.py`: tests for AC1 to AC5. `tests/engine/` has no `__init__.py`, like the other test folders.
- `sdlc/v0/tasks/T14/plan.md`: this file.

Edited: none. No new dependencies, so `uv.lock` and `pyproject.toml` stay as they are.

## Interface (decisions in this plan)
- `Bump = Literal["major", "minor", "patch"]`. Mypy strict, no `Any`.
- `parse_version(text: str) -> tuple[int, int, int]`. Accepts exactly `N.N.N` with decimal digits (`re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")`). Anything else raises `ValueError` naming the bad input. Leading zeros are rejected so each version has one spelling. This is a choice; the spec does not say.
- `format_version(v: tuple[int, int, int]) -> str`.
- `bump(version: str, kind: Bump) -> str`. `major` resets minor and patch; `minor` resets patch; `patch` only increments.
- `current_version(releases: Iterable[str]) -> str`. Returns the highest version by semver comparison, or `"0.0.0"` when there are none. The spec says "newest recorded release". I read that as the highest version. If the history is ever out of order, this matters, so I'm flagging it.
- `release_bump(kinds: Iterable[Bump]) -> Bump`. Largest of the bumps (major > minor > patch). An empty input raises `ValueError`, because a release with no requirements has no bump.
- `default_bump(requirement_kind: str) -> Bump | None`. Returns `"patch"` for `"maintenance"`. Returns `None` for any other kind, because the spec gives no default for feature requirements (the spec agent proposes that bump, §13). Using `None` avoids inventing a default the spec doesn't state. The owner can ask for a different default.

## Order of work
1. Write `tests/engine/test_version.py` first, one test per AC, and run `make test`. Expect it to fail on import (`ModuleNotFoundError: ultron.engine.version`).
2. Write `src/ultron/engine/version.py` to pass the tests.
3. Run `make lint`: ruff check, ruff format check, and mypy strict on `src`. Fix any typing issues in the module, not in the tests.
4. Run `make test`, then `make build`.

## Risks
- Spec wording "newest recorded release" could mean "most recently recorded" rather than "highest version". For normal releases the two are the same. I'll use the highest version and say so in the report.
- `default_bump` returning `None` for non-maintenance requirements is an interpretation. If the owner wants a default (for example `minor`), it's a one-line change plus a test.
- Leading-zero rejection is stricter than the AC requires. AC5 only lists `1.2` and `v1.2.3`, so this stays inside AC5.
- Windows and PowerShell: the commands run through `make` with `UV` set as in CLAUDE.md. Unset the `UV` env var first, as the brief says.

## Proof (acceptance criteria to tests)
| AC | Proof |
|---|---|
| AC1 `bump("1.1.0","minor") == "1.2.0"`; major and patch reset lower parts | `tests/engine/test_version.py`: parametrized `bump` cases, including `major` and `patch` resets (`1.1.0`→`2.0.0`, `1.1.0`→`1.1.1`) |
| AC2 no releases gives `0.0.0` | `test_current_version_empty_is_0_0_0`: `current_version([]) == "0.0.0"` |
| AC3 `release_bump(["patch","minor","patch"]) == "minor"` | `test_release_bump_takes_largest`, with that exact list, plus a major case and the empty-input error |
| AC4 maintenance default is `patch` | `test_default_bump_maintenance_is_patch`; also checks a feature kind returns `None` |
| AC5 invalid versions raise a clear error | `test_parse_rejects_invalid`, parametrized over `"1.2"`, `"v1.2.3"`, `"1.2.3.4"`, `"01.2.3"`, `""`; checks `ValueError` and that the message contains the input |
| Whole task | `make test` ends with `All tests passed`; `make lint` ends with `Lint succeeded`; `make build` ends with `Build succeeded` |

## Rerun after the tasks.md revision (R0)

The owner sent the work back to decomposition. The revision (commit 395e051) added `verify_with` to each criterion and `mcp: none` to the task. The criterion wording is unchanged. Checked against the attempt-1 code.

Changes in this rerun:
- `tests/acceptance/test_t14.py` (written by the testing agent, not by me) now exists. It covers AC1 to AC5 with more cases: the bump table, invalid inputs such as `1.2.3\n`, `-1.2.3`, `1..3`, and the errors from `bump` and `current_version`. Every test passed on the attempt-1 code (`uv run pytest tests/acceptance/test_t14.py tests/engine`: 49 passed).
- No code change is planned. The attempt-1 code already meets every criterion, and the task says to keep it.

Files that change: this `plan.md` only. If lint or build fails, the fix goes in `src/ultron/engine/version.py` and the rerun plan is updated in the same commit.

Proof, per criterion, using its `verify_with`:
| AC | verify_with | Proof on the attempt-1 code |
|---|---|---|
| AC1 bump resets lower parts | `pytest` on `tests/engine/test_version.py` | `test_bump` cases, and `tests/acceptance/test_t14.py::test_ac1_bump` |
| AC2 no releases gives `0.0.0` | `pytest` on `tests/engine/test_version.py` | `test_current_version_empty_is_0_0_0` |
| AC3 `release_bump` takes largest | `pytest` on `tests/engine/test_version.py` | `test_release_bump_takes_largest` |
| AC4 maintenance default `patch` | `pytest` on `tests/engine/test_version.py` | `test_default_bump_maintenance_is_patch` |
| AC5 invalid versions raise | `pytest` on `tests/engine/test_version.py` | `test_parse_rejects_invalid`, plus the acceptance file's invalid list |
| Whole task | `make build`, `make lint`, `make test` | Run from the worktree root; results go in the report |

Risks:
- The acceptance file's `test_ac5_invalid` also passes `"1.2.3\n"`. `re.fullmatch` rejects it, so no change is needed.
- Ruff and mypy also check `tests/acceptance/` (the whole repo is linted). If the testing agent's file breaks lint, that's a finding for the testing agent, not a reason to edit it.
- The `mcp: none` line means no MCP check applies to T14.
