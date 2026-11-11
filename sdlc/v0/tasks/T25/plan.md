# T25 plan: Jira Cloud client (issues, types, sprints, attachments)
Author: developer agent (R3, attempt 1). Task: `tasks.md` §T25. Spec: §12, D5. Depends on T04 (`Config.jira`:
`project`, `board`, `epic`).

## Owner instruction during planning (2026-11-11)
The owner said, through the engine: **don't use project AD or its board for testing, not even read-only.** A separate
test project and board come later. So:
- Fixtures are built from the shapes in Atlassian's public REST API docs, with placeholder keys (`TEST`, `TEST-1`,
  board `1`, `https://example.atlassian.net`). Nothing read from AD is committed. Before the instruction arrived I had
  read AD's issue-type list, Story field metadata, sprint values on a few issues and epic AD-378 through Rovo (read
  only). That data is discarded: none of it is in a file, a fixture or a test.
- Project key, board and epic come only from config (`[jira]` in `ultron.toml`) or the caller. No AD value is in the
  code or tests.
- Live checks (AC6, AC7) are owner steps against "the test project from `ultron.toml`".

A second owner update the same day named the test project: **AK** on the same site. Rovo may read AK (read only);
AD stays off limits. What Rovo showed for AK (2026-11-11):
- Issue types: Subtask, Epic, Story, Bug, Task. **No Feature**, so AC1's fallback case uses AK's real list.
- Story create fields: summary, issuetype, project, reporter (default) required; `parent`, `labels`, `description`,
  `attachment` and a Sprint custom field are settable. Team-managed (`simplified`).
- **No issues yet, so no epic**, and no issue is in any sprint. Rovo has no board or sprint endpoint, so AK's board
  id and whether it has an active or future sprint can't be read through it.

So the fixtures are:
- `issuetypes_ak.json`: AK's real issue-type list (URLs rewritten to `https://example.atlassian.net`, no names,
  emails or account ids). `issuetypes_with_feature.json` is the same list plus a Feature entry, marked derived.
- Sprint lists and the create/attach responses: shaped from Atlassian's public REST docs, marked `source: docs`.
- Every fixture has a `_capture` header (date, source, notes). AC8 is met only for the issue-type metadata. For the
  sprint list it's an **owner step**: Rovo can't read AK's board.

Owner steps found: create an epic in AK, put its key and AK's board id in the test `ultron.toml`, start (or plan) a
sprint on AK's board, then run AC6/AC7.

## Files that change
- `pyproject.toml`, `uv.lock`: `httpx` becomes a runtime dependency (already in the lock through `mcp`); `respx` is
  added to `dev`. Both free.
- `src/ultron/jira/__init__.py`: public names.
- `src/ultron/jira/client.py`: `JiraCredentials.from_env()`, `JiraClient` and its errors, `ENDPOINTS` (the only
  REST endpoints the client may call), `SprintChoice`, `CreatedIssue`, `EscalationResult`.
- `src/ultron/jira/body.py`: the §12 ticket body (`TicketBody`, `AttemptRow`), the title
  `[ultron] R<n>/T<k> <title>`, and its Atlassian Document Format (ADF) rendering: headings, a criteria list, an
  attempt table, and the last test output as a code block.
- `tests/jira/__init__.py`, `tests/jira/fixtures/*.json`,
  `tests/jira/test_client.py`, `tests/jira/test_body.py`, `tests/jira/test_live.py` (owner only; skipped unless
  `-m live` is given).

## Design
- **Credentials:** `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, read from the environment only. A missing or
  empty one raises `JiraConfigError` naming it. The base URL must be `https://`. The token sits in a `_Secret` whose
  `repr`/`str` is `***`; it goes only into httpx `BasicAuth`. Errors carry method, path, status and Jira's error
  text, never headers. httpx transport errors are re-raised as `JiraUnreachable` with the type and path only, so T26
  can queue a pending retry.
- **Endpoints (`ENDPOINTS`):** issue-type metadata `GET /rest/api/3/issue/createmeta/{project}/issuetypes`; create
  `POST /rest/api/3/issue`; read `GET /rest/api/3/issue/{key}`; delete `DELETE /rest/api/3/issue/{key}` (live test
  cleanup only); attach `POST /rest/api/3/issue/{key}/attachments`; sprints `GET /rest/agile/1.0/board/{board}/sprint`;
  move `POST /rest/agile/1.0/sprint/{id}/issue`. Every request goes through one `_request(name, ...)` that refuses a
  name outside `ENDPOINTS`. None of them create, update, start, close or delete a sprint.
- **Issue type:** Story, Feature or Bug. If the project has no Feature, the issue is a Story and the description opens
  with a note saying so. A missing Story or Bug raises `JiraError` (the spec names a fallback for Feature only).
- **Epic:** `fields.parent = {"key": epic}`.
- **Sprint:** `state=active` first, else `state=future` (first one in board order), else backlog (no call). The
  result's `SprintChoice.kind` is `active`, `future` or `backlog` and names the sprint. Pages are followed via
  `isLast`.
- **Board:** a numeric board id from config (`jira.board`); anything else raises `JiraConfigError`.
- **`escalate(...)`:** resolve type → create → attach each file → choose sprint → move. Returns `EscalationResult`
  (key, requested and actual type, fallback flag, sprint choice, attachment names) with `to_dict()` for T26's event.

## Order of work
1. Tests first (red): fixtures, body tests, client tests for AC1–AC5, AC8's header check.
2. `body.py`, then `client.py` (green).
3. `test_live.py` for the owner.
4. `make build`, `make lint`, `make test`.

## Proof per criterion (by `verify_with`)
| AC | verify_with | Proof | Expected |
|---|---|---|---|
| AC1 | `pytest` + respx | `test_client.py::test_feature_falls_back_to_story`: type list without Feature; the create request's type is Story and its description has the fallback note | met |
| AC2 | `pytest` | `test_client.py::test_sprint_choice[active/future/backlog]`, and the move call happens only for active/future | met |
| AC3 | `pytest` | `test_client.py::test_endpoints_used`: the requests in a full `escalate` run are a subset of `ENDPOINTS`; no endpoint matches a sprint create/update/start/close/delete; the source has no `/rest/` path outside `ENDPOINTS` | met |
| AC4 | `pytest` | `test_client.py::test_missing_env[...]` per variable; `::test_token_never_leaks` with a sentinel token: DEBUG log capture (httpx, httpcore, ultron), exception text on 401 and on a connection error, `repr` of client/credentials, and `EscalationResult.to_dict()` | met |
| AC5 | `pytest` | `test_client.py::test_attachment_header`: `X-Atlassian-Token: no-check`, multipart `file` field | met |
| AC6 | `live-call` · `owner` | `uv run pytest -m live tests/jira/test_live.py` with the `JIRA_*` variables set and `ULTRON_JIRA_TEST_PROJECT`, `ULTRON_JIRA_TEST_BOARD`, `ULTRON_JIRA_TEST_EPIC` naming the test project from `ultron.toml`. Creates one issue, checks it, deletes it in `finally` | owner |
| AC7 | `screenshot-browser` · `owner` | same command with `ULTRON_JIRA_LIVE_PAUSE_SECONDS=180` and `-s`: it prints the issue URL and waits before deleting, so Claude in Chrome can screenshot the issue | owner |
| AC8 | `live-call` · `pytest` | `test_client.py::test_fixtures_have_capture_header`. AC1's issue types are AK's real list (Rovo, read only); AC2's sprint lists are docs-shaped because Rovo can't read AK's board | partly met; sprint capture is an owner step |

## Changes during build
- No `tests/jira/conftest.py`: T05's acceptance test forbids `pytest_*` hooks in any conftest (so nothing can skip
  tests silently). The `live` marker is registered in `pyproject.toml` (`markers`) instead, and `test_live.py` skips
  itself unless the `-m` expression selects `live`.
- The live test reads the target from `ULTRON_JIRA_TEST_CONFIG` (path to the test project's `ultron.toml`, loaded by
  T04's `load_config`), not from separate env vars, so project, board and epic come only from config.
- The sprint choice reads only the first page of each state (`maxResults=50`); only the first sprint is needed.
- One `make test` run failed T05's `test_ac2_engine_lock_path_non_ascii_paths_and_payloads` (a multi-process lock
  test) under load; it passed alone and on the next full run. Not touched by T25.

## Risks
1. **Docs shapes vs. real responses.** Team-managed and company-managed projects differ (for example, the Sprint
   field). The client uses the Agile move endpoint, not the Sprint custom field, so it doesn't need that field's id.
   The live test is the check.
2. **ADF.** Jira v3 needs ADF for the description; plain text or Markdown would show as raw text (AC7). Only standard
   node types are used (heading, paragraph, bulletList, table, codeBlock). The table is the riskiest; the live test
   reads the issue back.
3. **Lockfile.** `uv.lock` changes, so it may conflict on merge (L6); regenerate rather than hand-merge.
4. **Token leaks through libraries.** httpx and httpcore DEBUG logs don't print header values today; the log-capture
   test catches a regression, including the Base64 Basic form.

## Attempt 2 (R4)
Attempt 1 failed at the testing agent: 2 of 85 acceptance tests in `tests/acceptance/test_t25.py` (commit 217c939).

### Causes
1. **Token reachable through `__context__` (AC4).** `_request` raised `JiraUnreachable(...) from None` inside the
   `except` block. `from None` only sets `__suppress_context__`; the httpx error stays on `__context__`, and its
   `_request` holds the `authorization: Basic ...` header. The code comment claimed the opposite. Same audit:
   `_request` caught only `httpx.TransportError`, so `httpx.DecodingError` and `httpx.TooManyRedirects` (also
   `RequestError`s carrying the request) escaped raw, with the header.
2. **Double filing after create.** `attach()` read the file after the issue existed; a missing file raised a bare
   `FileNotFoundError` with no `issue_key`, and `escalate` only tagged `JiraError`s. Any other non-`JiraError` after
   create (a sprint without `id`, a non-JSON body) had the same gap.
3. **Kanban boards (tester's note).** `GET /rest/agile/1.0/board/{id}/sprint` answers 400 ("The board does not support
   sprints") on a board without sprints. Spec §12 says no active and no planned sprint means the backlog, so failing
   there was wrong.

### Fixes
1. Every wrapped error is raised **after** its `except` block, from a string captured inside it, so `__cause__` and
   `__context__` are both `None` and nothing ultron raises refers to an httpx object. `_request` catches
   `httpx.HTTPError`: `TransportError` becomes `JiraUnreachable` (retry later), any other `HTTPError` becomes
   `JiraError`. Both name only the method, path and error type.
2. `attach()` turns an unreadable file (`OSError`) into a `JiraError` naming the file. `escalate()` tags every
   `JiraError` after create with `issue_key`, and wraps any other `Exception` after create in a `JiraError` with
   `issue_key` (type and message only, no chain).
   - **Not done: checking attachments before create.** It would mean a missing file fails before the issue exists,
     but `test_partial_failure_missing_attachment_file_carries_issue_key` requires that case to raise with
     `issue_key == "TEST-1"`, so the issue must be created first. The acceptance test decides.
3. A 400 from the sprint lookup gives `SprintChoice("backlog", reason=...)` with Jira's message, with no further
   sprint call and no move. Other statuses (401, 403, 404, 5xx) and transport errors still fail with `issue_key`.
   `reason` appears in `describe()` and, only when set, in `to_dict()` (AC2's acceptance test pins the dict for the
   three plain cases). `sprints.json` gains a `no_sprints` response (docs: 400 for this endpoint; message text as
   Atlassian reports it for kanban boards, not captured live).

### Risks
- A 400 for another reason (not kanban) also lands in the backlog. The issue is in the backlog anyway at that point,
  and `reason` records Jira's message, so nothing is lost or hidden.
- A transport error during the create POST itself (Jira created it, the reply was lost) still has no key, so a retry
  can file twice. Needs a lookup by label or summary in T26; out of scope here.
- Exception `__traceback__` frames still hold `self` (the client and its auth). That's true of any exception raised
  in a method; `repr(client)` hides the token, and the test with `capture_locals` checks it.

### Proof
| Defect | Test (red first, then green) |
|---|---|
| 1 | `tests/jira/test_client.py::test_wrapped_errors_hold_no_auth[...]`: ConnectError, ReadTimeout, RemoteProtocolError, DecodingError, TooManyRedirects; walks `__cause__`, `__context__`, `args`, `vars()` and slots of every reachable object (not tracebacks) for the token, plain and Base64 |
| 2 | `::test_unreadable_attachment_carries_key[missing/directory]` and `::test_unexpected_sprint_response_carries_key[sprint-without-id/not-json]`: one create call, `JiraError.issue_key == "TEST-1"`, no cause or context, nothing reachable; `::test_wrapped_error_after_create_holds_no_auth` |
| 3 | `::test_board_without_sprints_goes_to_backlog`, `::test_plain_backlog_has_no_reason`, `::test_other_sprint_lookup_errors_still_fail[401/403/404/500]` |
| all | `tests/acceptance/test_t25.py` 85 of 85; `make build`, `make lint`, `make test` |

### Changes during build (attempt 2)
- Test names in the proof table were settled while writing them (above). `test_reachable_text_finds_the_header_in_a_raw_httpx_error`
  checks the walker itself: it must find the header on a raw httpx error, on a hidden `__context__`, and the token
  inside `JiraCredentials`.
- The broad `except Exception` after create needs `# noqa: BLE001` (ruff's blind-except rule); it is deliberate.

## Attempt 3 (R5)
Attempt 2 failed at the testing agent: 78 of the acceptance tests in `tests/acceptance/test_t25.py` (commit a8d63f5),
all one AC4 defect.

### Cause
`_request` raised the status `JiraError` while its frame still held `response`. The error's `__traceback__` keeps that
frame, so `exc.__traceback__` → `_request` frame → `f_locals["response"]` → `.request.headers["authorization"]` reached
`Basic <email:token>`. My walker skipped tracebacks (the risk I wrote down in attempt 2), so my tests passed.

A probe of the attempt-2 code with a walker that follows frames found four more paths of the same kind:
1. A 2xx body that isn't JSON: `JSONDecodeError` came out of httpx's `Response.json`, whose frame holds the response,
   and escaped raw before create.
2. Exceptions from the send that aren't `httpx.HTTPError` (`InvalidURL`, `CookieConflict`, the `StreamError` family,
   anything else) escaped raw, through httpx frames that hold the request.
3. `JiraCredentials.from_env` raised `JiraConfigError` (for example a non-https URL) while its frame held the token
   (`value`, `values`) and `os.environ` (`source`).
4. A malformed `JIRA_BASE_URL` or a bad proxy variable made `httpx.Client(...)` raise inside `JiraClient.__init__`,
   whose frame holds the credentials, through httpx's frame holding the `BasicAuth` header.

Also found: a 3xx reply is neither `is_error` nor a success in httpx, so it was parsed as if it were data.

### Fix
One rule: an exception that leaves the client reaches only plain values. Every httpx object, and everything else that
holds the token, lives only in a frame that always returns, so no traceback ever holds it.
1. **`_exchange` (new) is the only code that touches httpx.** It sends, then keeps the status, Jira's error text and
   the parsed JSON in a `_Reply` of plain values. It catches every `Exception` (`TransportError` means unreachable;
   anything else keeps only its type name), so it always returns. `_request` raises `JiraError` or `JiraUnreachable`
   from the `_Reply`, so its frame holds only the name, path, the caller's arguments and the `_Reply`. It returns the
   parsed JSON, so no caller ever holds a response.
2. Any status outside 2xx fails with its status. A non-empty 2xx body that isn't JSON is a `JiraError`. An empty body
   is `None` (204 from move and delete).
3. **`escalate` after create:** it raises a new error after the `except` blocks, of the same class, with the same
   method, path and status, plus `issue_key`. The message starts "Jira issue K was created, but a later step failed".
   It has no cause or context, and its traceback holds `escalate`'s frame but not the frame of the failed step.
4. **`_read_env` (new)** reads the variables and returns the credentials or an error message. `from_env` raises from a
   frame that holds neither the token nor `os.environ`. It also checks the base URL with `httpx.URL` (malformed or
   missing host means a `JiraConfigError` naming `JIRA_BASE_URL`), so a bad URL can't reach `httpx.Client`.
5. **`_http_client` (new)** builds the httpx client and returns it, or the error's type name. `__init__` deletes
   `credentials` before it raises `JiraConfigError`.

Kept from attempt 2: AC1–AC3, AC5, AC8, kanban → backlog, `issue_key` on failures after create.

### Risks
- After create, the inner traceback is dropped. The message keeps the method, path and status of the failed step.
- A `KeyboardInterrupt` during a send unwinds through `_exchange` with the request in its frame. ultron doesn't raise
  it, and a traceback prints locals only when asked.
- A mapping passed as `environ` (tests do) stays in the caller's frames and in `JiraClient.from_env`'s argument. It
  belongs to the caller. In production nothing is passed, and the client reads `os.environ` itself.
- `self` in a method's frame is the client, which must hold the credentials to send them. Walkers see it only through
  its repr, which hides them.
- Before create, a 2xx body that isn't JSON is now a `JiraError`, where attempt 2 let a raw `JSONDecodeError` escape.

### Proof
| Defect | Test (red on the attempt-2 code first, then green) |
|---|---|
| walker | `tests/jira/test_client.py`: `_reachable_text` now follows `__traceback__` frames from ultron's outermost frame inward (ultron's frames and the library frames below them). It skips the caller's frames above, sees a `JiraClient` only through its repr, and also reports any httpx or httpcore object it reaches. `test_reachable_text_finds_the_header_in_a_raw_httpx_error` gains the frame cases. |
| main | `::test_error_status_holds_no_auth[step-status]` (each step × 400/401/500; the reply echoes the header) and `::test_public_method_errors_hold_no_auth[...]` (`issue_types`, `create_issue`, `attach`, `choose_sprint`, `move_to_sprint`, `get_issue`, `delete_issue` on 500) |
| 1 | `::test_non_json_body_is_a_jira_error[types/sprints]` |
| 2 | `::test_other_send_errors_are_wrapped[...]` (`InvalidURL`, `CookieConflict`, `StreamClosed`, `RuntimeError`) before and after create |
| 3 | `::test_config_errors_hold_no_token[...]` with the variables in `os.environ` (http URL, malformed URL, no host) |
| 4 | `::test_client_setup_error_holds_no_token` (a bad `HTTPS_PROXY`) |
| 3xx | `::test_redirect_is_an_error` |
| all | `tests/acceptance/test_t25.py`, all passing; `make build`, `make lint`, `make test` |
