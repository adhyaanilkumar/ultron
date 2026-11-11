# T18 plan: context builder, role prompts and output schemas

Rung R2, attempt 1. Spec §11.3 (D22), §7.1, §7.3, §13, §11 (untrusted data), FR11, NFR4.

## Files that change
New:
- `src/ultron/agents/prompts.py`: one `PromptTemplate(role, version, text)` per role (intake_scout, intake, spec, decomposition, developer, testing, maintenance), the `REPO_INSTRUCTIONS_HEADING` constant (exact text from §11.3) and the bundled intent template. Version string is `<role>/v<n>`.
- `src/ultron/agents/schemas.py`: one pydantic output model per role, `output_schema(role)` (JSON schema handed to `AgentRunner.run`) and `validate_output(role, data)`. Reuses `Task` from `engine/task_graph.py` (T11) for the decomposition output; nothing is redefined (L9).
- `src/ultron/agents/context.py`: `build_context(role, inputs) -> BuiltContext` (prompt, prompt version, included files with SHA-256), repo-instruction discovery, task-section extraction from `tasks.md`.
- `tests/agents/test_context.py`, `tests/agents/test_schemas.py`.
Edited (additive, optional fields only):
- `src/ultron/agents/calllog.py`: `CallContext` gets `prompt_version` and `context_files` (both optional); `LoggingRunner` copies them into the `agent_call` event data. With neither set the event is byte-for-byte what T15 wrote. T15's builder `events.agent_call` is untouched.

## Order of work
1. Tests first (red): contexts per role, switch on/off + exact heading, hashes, testing isolation, schemas good/bad, version in event.
2. `prompts.py`, `schemas.py`, `context.py`, then the `calllog.py` hook.
3. `make build`, `make lint`, `make test`.

## Design
- Files per role come only from §11.3: scout `request.md`; intake `request.md` + intent template (bundled, recorded as `ultron:intent-template`) + `research.md` if present; spec `intent.md` + `research.md` (labelled non-binding) + repo instructions; decomposition `intent.md` + `spec.md` + repo instructions; developer `spec.md` + its task section of `tasks.md` + `plan.md` + dependency-changed files + repo instructions (+ retry text); testing its task section of `tasks.md` + diff + repo instructions; maintenance evidence + release + release tasks + repo instructions.
- Repo instructions: root and nested `CLAUDE.md` and `AGENTS.md` under the repo, only when `repo_instructions` is true and only for the five roles whose §11.3 row lists them. Symlinks are skipped (a hostile repo could link to a secret), as are `.git`, `node_modules`, `.venv`, `.ultron`. Large files are cut at 200 000 bytes with a marker; the hash is of the whole file.
- Untrusted text (repo files, diffs, test output, comments, research) is wrapped in a fence whose marker contains the SHA-256 prefix of the content, so the content cannot close its own fence. A fixed rule in every template says fenced text is data.
- Testing isolation is structural: the testing builder never reads `plan.md`, transcripts or attempt reports, and takes no retry or reasoning input. The test puts a unique marker in the developer transcript, plan.md and an attempt report and asserts it is absent.
- Spec template carries the §7.3 rule (human-steps checklist and INR cost table) and the §13 bump proposal. `SpecOutput` enforces them structurally: `infrastructure=true` needs non-empty `human_steps` and `cost_table`.
- Schemas are pydantic with `extra="forbid"`; no new dependency (no `jsonschema`).

## Interpretations (spec is silent)
- Output carries content, engine writes files: read-only or tool-less roles (scout, intake) cannot write, so every role returns its artifact text in the JSON. Same for spec, decomposition, maintenance.
- Developer has two phases (`plan`, `code`). `plan.md` is included if it exists: "approved plan" in the code phase, "previous draft to revise" in the plan phase.
- "Files its dependencies changed" arrives as a list of paths from the engine (T11/T26 compute them); the builder reads them from the worktree.
- "Failing test output, stop reason, gate comments", the diff for testing, and maintenance evidence/release arrive as text from the engine, labelled as data. They are not in the file list (only files have a path and hash).
- The hash for a `tasks.md` task section is the hash of the whole file read; only the task's section enters the prompt.
- Paths are recorded repo-relative in POSIX form when inside the repo, else absolute.
- Testing agent tests go to `tests/acceptance/` (the folder FR11 protects from developers); stated in both prompts.
- Bundled prompts live in Python constants (no package-data risk with `uv_build`).

## Risks
- Prompt wording is a judgement call (rubric Ambiguity 1); tests assert required clauses, not prose.
- `calllog.py` is T15's file: change is additive and covered by T15's existing tests staying green.
- Pydantic `Task` nesting makes the decomposition JSON schema large; acceptable.

## Proof per criterion
- AC1: `tests/agents/test_context.py::test_exact_files_per_role` (7 parametrised cases, exact set of paths for a fixture requirement, switch on and off).
- AC2: `test_repo_instructions_on_and_off`: exact heading, nested CLAUDE.md and AGENTS.md present on, absent off; symlink skipped.
- AC3: `test_call_record_has_path_and_sha256`: SHA-256 recomputed from disk.
- AC4: `test_testing_never_sees_developer_reasoning`.
- AC5: `tests/agents/test_schemas.py`: good and bad sample per role, plus infrastructure rule.
- AC6: `test_prompt_version_reaches_agent_call_event`: runs `LoggingRunner` with a fake runner, reads the event.

## Attempt 2 (R3)
Attempt 1 failed 3 of 118 acceptance tests (commit 4e4d27f). Causes and fixes, same files as above (`context.py`, `prompts.py`, additive `calllog.py`):
1. **Task past 200 000 bytes of tasks.md not found.** `read()` cut the file before `_task_block` searched it. Fix: the task section reads the whole file, finds the section, then cuts only the section if it is over 200 000 bytes (marked in the prompt, `truncated` true in the record). The hash stays over the whole file.
2. **No rule for telling the real end of a fence.** Rule 1 in the common template now says a fence ends only at the END line that repeats the opening `sha256` mark; any other END line is data. The prompt version stays `<role>/v1`: v1 has never shipped, and the acceptance tests pin `developer/v1`.
3. **NUL bytes reached the prompt.** Every fenced text is cleaned before fencing: NUL and other C0 control characters (except tab, LF, CR) and DEL become U+FFFD, and lone surrogates are replaced, so the prompt is always valid UTF-8 a subprocess can carry. The fence mark is over the cleaned text; the recorded hash is over the raw bytes. Fence labels (paths) get the same cleaning plus line breaks, so a hostile folder name cannot add a line outside a fence.
Also (tester's observations):
- `files_record()` entries carry `truncated` (bool). `CallContext.context_files` is typed to allow it.
- Instruction files past the first 50 are no longer dropped silently: `BuiltContext.omitted_files` lists them, the prompt says how many were left out, and `LoggingRunner` writes them as `context_omitted` in the `agent_call` event (only when there are any).

Order: tests reproducing each defect in `tests/agents/test_context.py` (red), then the fixes, then `make build`, `make lint`, `make test`.
Proof: `test_task_section_found_past_200kb`, `test_long_task_section_is_cut_and_flagged`, `test_every_template_states_the_matching_sha256_end_rule`, `test_nul_and_control_bytes_never_reach_the_prompt`, `test_hostile_label_cannot_break_a_line`, `test_files_record_has_truncated_flag`, `test_instruction_files_past_the_cap_are_recorded_as_omitted`, and all of `tests/acceptance/test_t18.py`.
