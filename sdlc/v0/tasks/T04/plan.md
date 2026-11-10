# T04 plan: `ultron.toml` configuration (schema, defaults, presets, validation)
Author: developer agent (Claude). Rung: R1. Spec: §8, §9.1, §10.1, §11.3, §11.4, §12, NFR3. Depends on T01 (done).

## Files that change
New (all under `src/ultron/config/`, a package inside the T01 layout; `config` is not in the §5.1 list, so it is a choice for the plan gate):
- `__init__.py`: re-exports `load_config`, `Config`, `ConfigWarning`, `ConfigError`.
- `models.py`: pydantic v2 models, `extra="forbid"` everywhere (gives AC7). Sections: `Commands`, `Deploy`/`Maintain`, `Gates`, `Agents` (per role: adapter, model, effort), `Ladder` (R0-R5 + `max_rung`), `Caps`, `Thresholds` (denied actions 5, no-progress 10 min), `Scout` (`max_tool_calls` 15, `budget_cap` (named so no field matches "token")), `Broker` (`ask_limit` 3), `Jira` (project, board, epic; no credentials), `Paths`.
- `defaults.py`: spec constants (D16 roles, §11.4 ladder, presets table, platform path defaults).
- `loader.py`: `load_config(path, overrides=None)` reads TOML with stdlib `tomllib`, merges a per-requirement override dict (deep merge) over the repo file, then validates once. Returns `LoadedConfig(config, warnings)`.
- `warnings.py`: `ConfigWarning` dataclass (gate, from, to, message) for AC6.
- `tests/config/{__init__,test_defaults,test_gates,test_deploy,test_paths,test_schema}.py`.
- `sdlc/v0/tasks/T04/plan.md` (this file).
Edited: `pyproject.toml` (+ `pydantic` dependency; ₹0), `uv.lock` (regenerated). `CLAUDE.md` unchanged.

## Design notes
- Gate table: `intent/spec/plan/result` human, `tasks/escalation/maintain` auto, `merge/deploy/rollback` human. A `preset` key (`strict|default|unattended`) expands first, then explicit per-gate values win. A model validator rejects `auto` for the locked three and names the gate. The check runs after override merge, so it covers both sources.
- Warnings: comparing each changeable gate against the default-human set (intent, spec, plan, result); `auto` there gives a warning. Presets `unattended` also warns (recorded, as the spec says "switching a default-human gate").
- `deploy` set and `rollback` empty is an error; the reverse is fine.
- `max_attempts` 1-10 (default 3); `max_parallel` default 2 (must be >= 1).
- Paths: Windows `C:\git\.ultron-work` / `C:\git\.ultron`; elsewhere `~/.ultron-work` / `~/.ultron`. A space in the resolved path is an error suggesting a path without spaces. Defaults on Linux are checked too (home with a space is rejected, not silently accepted).
- Caps: `per_requirement_usd`, `per_evaluation_run_usd`. Spec gives no numbers (FR18), so I need defaults. Proposed: 5.00 and 50.00 USD, marked as a choice for the owner.
- Other defaults from spec: `tag_format` `v{version}`, `rollback_rehearsal_days` 30, `maintain.interval` 30 min, `intake_research` `local`, `repo_instructions` true, scout 15 tool calls.

## Order of work (TDD, one commit per step is not required; the testing agent writes the tests)
1. Add `pydantic`, `uv lock`, `uv sync`.
2. `defaults.py` and `models.py` for the minimal `[commands] test` case (AC1).
3. Gates, presets, locks and the warnings object (AC2, AC4, AC6).
4. Deploy/rollback rule (AC3), path validation (AC5), unknown-key rejection (AC7).
5. `loader.py` with override merge; wire tests; run `make build lint test`.

## Risks
- Locked-gate check bypassed by an override or preset: validate after merge and after preset expansion, with tests for both routes.
- Spec gaps: cap amounts, Linux path defaults, scout token cap, threshold key names are my choices; listed above for the plan gate.
- `max_rung` and ladder: validate rung names R0-R5 only and that `max_rung` exists in the ladder.
- Windows path detection should use `sys.platform`, injectable for tests so the Linux defaults are tested on Windows.
- Strict mypy with pydantic: use the pydantic mypy plugin only if needed.

## Proof
| AC | Proof |
|---|---|
| AC1 | `tests/config/test_defaults.py`: minimal file loads; asserts each default (§8, §10.1, §11.4, §9.1) |
| AC2 | `tests/config/test_gates.py`: parametrised merge/deploy/rollback = auto, in file and in override; message names the gate |
| AC3 | `tests/config/test_deploy.py`: deploy without rollback errors; rollback only loads |
| AC4 | `test_schema.py` for 0, 11, -1 rejected, 1 and 10 accepted; `test_gates.py` for the three presets vs the §8 table |
| AC5 | `test_paths.py`: space in work folder and home rejected, message suggests a path without spaces; Linux defaults via patched platform |
| AC6 | `test_gates.py`: intent/spec/plan/result to auto returns `ConfigWarning` objects; defaults give none |
| AC7 | `test_schema.py`: no field named like token/password/secret in the schema (walk `model_fields`); `jira.api_token` rejected |
| All | `make build`, `make lint`, `make test` end in their success lines |

## Rung
R1 kept (rubric total 4: Files 1, Change 0, Risk 1, Ambiguity 1, Algorithmic 0). No reason to change after reading the code.
