# MCP research for ultron V0 tasks

Author: intake scout (Haiku 5.5 medium, D16). Date: 2026-11-10. Facts and sources only; no recommendations. Search results were mostly third-party directories, so items marked "unverified" need checking on the official page before use.

## Summary
1. Official MCP servers relevant to the tasks: git (`mcp-server-git`, early development), the MCP Python SDK, and the MCP Inspector. The official SQLite server is archived.
2. Already connected here and relevant: Atlassian Rovo (Jira, Cloud only, beta per Atlassian's support page) and Claude in Chrome (browser screenshots and console, for T02/T48/T51-type checks).
3. Not connected here but found: GitHub's official MCP server, Docker MCP Toolkit (beta), Microsoft Playwright MCP, and community PTY/terminal and Task Scheduler servers.
4. Form elicitation (T02, T38) is in the 2025-11-25 MCP spec. Client support is reported only by third-party docs; Claude Code is reported as supporting it and claude.ai as not.
5. Every candidate found is free or open source. No paid tier was found that needs an INR conversion. Docker Desktop (T45) is ₹0 for personal use per tasks.md; I did not verify its licence.

## Category 1: Python stack, packaging, CI and releases
Tasks: T01, T42 (also T14, T31 release records)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| GitHub MCP Server | GitHub (listed as "GitHub Official") | https://github.com/github/github-mcp-server | Issues, PRs, releases, Actions; could read CI runs for T01/T42 checks | No | Free, MIT, Go; hosted endpoint https://api.githubcopilot.com/mcp/ | Active per listings; star counts vary by snapshot | Token scopes decide write access (not verified which scopes are needed) |
| No candidate found | | | For `uv`, ruff, mypy, pytest, Makefile and GitHub Actions, the CLIs and `gh` already cover these tasks | | | | |

Sources: https://github.com/github/github-mcp-server ; https://hub.docker.com/mcp/server/github-official/manual (listed by Docker as official)

## Category 2: Git layer, state branch and merges
Tasks: T06, T07, T23, T30 (also T43 integration test on real git)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| mcp-server-git | Model Context Protocol (official reference) | https://pypi.org/project/mcp_server_git ; https://glama.ai/mcp/servers/%40modelcontextprotocol/git | Status, diff, log, branches, staging, commit (per third-party README). Install: `uvx mcp-server-git` | No | Free, open source | README says early development; tools may change | Without `--repository` it reaches any repo the process can access. Whether it exposes checkout or reset was not verified. |
| Git MCP servers (community) | various | https://glama.ai/mcp/servers/@MementoRC/mcp-git | Similar git operations | No | Free | Not checked | Not checked |

Note: T06 requires the git layer to wrap the `git` CLI (no GitPython). The official server is a separate host-side tool; it does not replace T06. No candidate found that does merge-with-conflict-abort (T06 AC2) better than `git merge --abort` run through the CLI.

Sources: https://pypi.org/project/mcp_server_git ; https://mcpservers.org/servers/modelcontextprotocol/git

## Category 3: Event log, SQLite cache and activity file
Tasks: T05, T09, T28

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| mcp-server-sqlite | Model Context Protocol (official reference, archived) | https://pypi.org/project/mcp-server-sqlite/ ; https://glama.ai/mcp/servers/@modelcontextprotocol/servers-archived/blob/9be4674d1ddf8c469e6461a27a337eeb65f76c2e/src/sqlite/README.md | Query and manage a local SQLite file. Last release 2025.4.25 | No | Free | PyPI says archived; no new releases expected | Read/write access to the given file |
| sqlite-mcp-server (Go) | nipunap (community) | https://github.com/nipunap/sqlite-mcp-server | SQLite tools | No | Free | Not checked | Not checked |
| @berthojoris/mcp-sqlite-server | community | https://glama.ai/mcp/servers/@berthojoris/sqlite-mcp/blob/e2e8e1ce1b8cbef73292928db4083d795de7f2c5/README.md | Read/write with permission controls (per listing) | No | Free | Not checked | Permission model per listing; not verified |

Note: T09 says `state.db` is written only by the cache module, so an MCP that writes to SQLite would conflict with that rule. The SQLite MCP servers above are for inspecting the file.

Sources: https://pypi.org/project/mcp-server-sqlite/ ; https://github.com/nipunap/sqlite-mcp-server

## Category 4: MCP server, Claude Code plugin, hooks and elicitation (host side)
Tasks: T02, T36, T37, T38, T40, T41, T42, T48

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| MCP Python SDK (`mcp`) | Model Context Protocol | https://pypi.org/project/mcp ; docs https://py.sdk.modelcontextprotocol.io/v2/ | The server SDK for T36/T37/T38. Docs say an unpinned install gives v1.x; v2 docs exist | No | Free, MIT | Newest version differs by source (1.30.0, 2.2.0, 2.3.0 seen); unverified | None noted |
| MCP Inspector | Model Context Protocol | https://modelcontextprotocol.io/docs/tools/inspector | `npx @modelcontextprotocol/inspector <cmd>`: lists tools, shows schemas, runs tools with custom inputs, logs JSON-RPC. Per a third-party guide it renders elicitation requests as forms | No (run locally via npx) | Free | Official docs at 2025-11-25 version | Runs a local server; no credentials needed for the test |
| MCP spec: elicitation | Model Context Protocol | https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation | Form mode (JSON schema) and URL mode. Spec says sensitive data goes through URL mode, not form. Empty `elicitation: {}` is form-only | n/a | n/a | 2026-07-28 release candidate changes capability per request | Relevant to T38 AC1 (no decision-carrying form args) |

Client support for elicitation (third-party, unverified against vendor notes):

| Client | Reported support | Source |
|---|---|---|
| Claude Code | Reported as supported; Claude.ai reported as not supported | https://django-stateless-mcp.readthedocs.io/en/stable/recipes/elicitation-forms-and-confirmations/ |
| Cursor | Since v1.5 (Aug 2025), form mode only | same |
| Codex CLI | Server-driven elicitation merged April 2026 | same |
| ChatGPT web (developer mode) | No documented support | same |

Note for T02: the source above is a secondary summary. T02's spike is the only verification of Claude Code's behaviour on the owner's machine.

Sources: https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation ; https://django-stateless-mcp.readthedocs.io/en/stable/recipes/elicitation-forms-and-confirmations/

## Category 5: Agent SDK and CLI adapters
Tasks: T03, T15, T16, T17, T18, T19, T33, T34

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| claude-agent-sdk (Python) | Anthropic | https://pypi.org/project/claude-agent-sdk/ ; README https://cdn.jsdelivr.net/gh/anthropics/claude-agent-sdk-python@main/README.md | Agent loop in Python: `query()` and `ClaudeSDKClient`; `allowed_tools` and `disallowed_tools`; hooks; in-process SDK MCP servers; bundled CLI; errors `CLINotFoundError`, `CLIConnectionError`, `ProcessError`. Needs Python 3.10+ | No (it is a library, not an MCP server) | Free library; usage uses the subscription login (spec D3); cost per T03 | Many versions on PyPI (0.1.62 to 0.2.131 seen); latest not confirmed | Hooks and permission callbacks are the engine's enforcement points (T16) |
| Claude Docs MCP | Anthropic (connected here) | not searched | Docs lookup | Yes | n/a | n/a | n/a |

Note: the SDK's in-process MCP support is the mechanism T19 uses for the broker; no separate MCP server is needed for that.

Sources: https://cdn.jsdelivr.net/gh/anthropics/claude-agent-sdk-python@main/README.md ; https://pypi.org/project/claude-agent-sdk/

## Category 6: Jira client and escalation
Tasks: T25, T26 (also T43 Jira faked, T48 real Story)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| Atlassian Rovo MCP Server | Atlassian | https://support.atlassian.com/rovo/docs/atlassian-remote-mcp-server/ ; https://github.com/atlassian/atlassian-mcp-server | Hosted Jira/Confluence tools; endpoint https://mcp.atlassian.com/v1/mcp (one guide uses /v1/mcp/authv2, so check the docs). OAuth 2.1 browser sign-in, or API token | Yes (Atlassian Rovo, connected) | Free per one vendor guide (no paid plan documented); third-party cites 500 calls/hour Free tier, 1,000 Standard | Atlassian support page labels it beta; other sources say GA | Cloud only. Not Data Center. It is a host-side tool; ultron's engine client (D5, httpx REST) is separate, so this server cannot be what the engine calls |
| sooperset/mcp-atlassian | community (listings name it sooperset) | repo name per listings; https://lobehub.com/en/mcp/sooperset-jira-mcp | Jira and Confluence tools; Cloud and Data Center (PAT, Server/DC 8.14+) | No | Free; MIT per listings | Versions cited inconsistently (v0.21.1, v0.23.1) | Needs an API token or PAT; the token would sit in the MCP config. Not verified against the repo's LICENSE |
| atlassian-jira-mcp | community (PyPI) | https://pypi.org/project/atlassian-jira-mcp/ | Dual Cloud and Data Center auth, JQL, workflows | No | Free | Several versions on PyPI (2.0.0 to 2.5.0) | Credentials in config; not checked |

Note on Jira sprints (T25 AC3, CLAUDE.md): none of the candidates above was checked for sprint endpoints. The spec forbids sprint create/start/close, which is a property of ultron's own client, not a candidate's.

Sources: https://support.atlassian.com/rovo/docs/atlassian-remote-mcp-server/ ; https://lobehub.com/en/mcp/sooperset-jira-mcp ; https://mcp.directory/blog/jira-mcp-complete-guide-2026

## Category 7: Evaluation, Docker and SWE-bench containers
Tasks: T44, T45, T46, T47, T49, T50

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| Docker MCP Toolkit and Catalog | Docker | https://docs.docker.com/ai/mcp-catalog-and-toolkit/toolkit.md ; https://docs.docker.com/ai/mcp-catalog-and-toolkit/catalog/ | Runs containerised MCP servers via a gateway; catalog images are built and signed by Docker. Requires Docker Desktop 4.62+ per Docker's docs | No | Free per docs; Docker Desktop licence not verified | Toolkit is marked Beta in Docker's docs | Geekflare says the free gateway uses one shared token with no per-user access control |
| SWE-bench harness (not an MCP) | SWE-bench project | https://www.swebench.com/SWE-bench/reference/harness/ ; https://www.swebench.com/SWE-bench/guides/docker_setup/ | Grades a diff in per-instance Docker images (T45) | n/a | Free; needs local disk | n/a | Official figure: at least 120 GB free for any cache level; 16 GB RAM and 8+ CPU cores recommended. `--cache_level=env` or `base` reduces storage. Third-party: about 130 GB for the full Verified set |

Note: I did not search for a dedicated SWE-bench MCP server. The harness is a CLI and needs Docker directly (T45 AC2). The Docker MCP Toolkit is not needed to run it.

Sources: https://www.swebench.com/SWE-bench/guides/docker_setup/ ; https://docs.docker.com/ai/mcp-catalog-and-toolkit/toolkit.md ; https://geekflare.com/open-source/docker-mcp-gateway/

## Category 8: Browser and screenshots (demo and visual checks)
Tasks: T02 (screenshot of elicitation form), T35 (visual look), T48 (demo screenshots), T51 (README images)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| Playwright MCP | Microsoft | https://github.com/microsoft/playwright-mcp ; https://glama.ai/mcp/servers/@microsoft/playwright-mcp/blob/main/README.md | Default mode works on accessibility snapshots; `--vision` switches to screenshots and coordinate tools. Includes a screenshot tool (per listing) | No | Free, open source | Official Microsoft repo | Drives a real browser; runs in a sandbox per its docs (not verified) |
| Claude in Chrome | Anthropic (connected here) | not searched | Browser automation, screenshots, console messages | Yes | n/a | n/a | Uses the owner's real Chrome sessions |
| mcp-browser-screenshot (meirroth) | community | named in a search listing; URL not verified | browser_screenshot, browser_click, browser_type, browser_eval (per listing) | No | Free | Not checked | Not checked |

Note: these tools capture web pages. Terminal output (T35, T39 `ultron gate`) and the Claude Code window are not web pages; no candidate found captures a Windows Terminal window (desktop screenshots are not in this search).

Sources: https://github.com/microsoft/playwright-mcp

## Category 9: Terminal output, PTY and TTY checks
Tasks: T03 (TTY check), T27 (coloured console log), T35 (terminal look), T39 (`ultron gate` fallback)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| NPCterm | community (docs.rs crate) | https://docs.rs/crate/npcterm/1.3.1/source/README.md | Headless ANSI/VT100 emulator plus PTY, 17 tools over stdio | No | Free | Versions 1.3.1, 1.3.2 on docs.rs | Gives a host model a shell, which conflicts with T39's rule that the model cannot run `ultron gate`, if it were installed in the host |
| terminal-mcp | community | https://docs.rs/crate/terminal-mcp/0.1.4 | PTY mode; `shell_snapshot` returns the rendered screen after ANSI interpretation | No | Free | 0.1.4 | As above |
| pty-mcp | community (PyPI) | https://pypi.org/project/pty-mcp/ | PTY sessions; raw output includes ANSI codes | No | Free | Not checked | As above |
| mcp-shell | eja | https://github.com/eja/mcp-shell | PTY-based interactive shell | No | Free | Not checked | As above |

No official MCP server for terminal output was found. ANSI rendering for T35 is a Python-side concern (the mascot renderer); none of the candidates replaces it.

Sources: https://docs.rs/crate/npcterm/1.3.1/source/README.md ; https://pypi.org/project/pty-mcp/ ; https://github.com/eja/mcp-shell

## Category 10: Windows Task Scheduler (maintain schedule)
Tasks: T33 (AC4, AC5), T34 (schedule check)

| Name | Publisher | Link | Relevant capability | Connected here | Cost | Maintenance | Security caveat |
|---|---|---|---|---|---|---|---|
| Task Scheduler Studio | community (lobehub listing) | https://lobehub.com/mcp/markhopper24-task-scheduler-studio | Local MCP server `Tasker.Mcp.exe` over stdio; uses the Task Scheduler V2 COM API, so tasks appear in `schtasks` | No | Free (not verified) | Not checked | Highest-privilege tasks need admin rights |
| assiststudio-runner | fieldcure (community) | https://claudemarketplaces.com/mcp/io.github.fieldcure/assiststudio-runner | Seven tools: create, update, delete, list, run; maps cron to `schtasks` | No | Free | Not checked | Scheduled runs call back into the tool through `exec` |
| Windows Operations MCP / Windows Command Line MCP | community | https://glama.ai/mcp/servers/sandraschi/windows-operations-mcp/tools/windows_automation ; https://glama.ai/mcp/servers/@alxspiker/Windows-Command-Line-MCP-Server/tools/get_scheduled_tasks | Calls `schtasks.exe` to list, create, delete, run | No | Free | Not checked | Shell-out, not native API |

Note: T33 builds the `schtasks` command line in ultron's own code (AC4 unit test). None of these servers is needed for that.

Sources: https://lobehub.com/mcp/markhopper24-task-scheduler-studio ; https://claudemarketplaces.com/mcp/io.github.fieldcure/assiststudio-runner

## Category 11: Pure logic, config, validation and tests
Tasks: T04, T08, T10, T11, T12, T13, T14, T15, T18, T20, T21, T22, T24, T26, T29, T31, T32, T43, T47

- Not searched: dedicated MCP servers for pytest, ruff, mypy, or JSON Schema / pydantic validation.
- Fact: nothing in the searches above provides a test runner, linter or type checker that does better than the commands these tasks already specify (`make test`, `make lint`).
- Fact: the Claude Code plugin skill and the MCP Inspector (Category 4) cover manual testing of tool schemas.

## Category 12: Maintenance log reading and fingerprinting
Tasks: T32, T33

- Not searched: log-analysis or error-tracking MCP servers (for example Sentry-style tools).
- Fact: T32's log reader is plain file IO with a byte offset (AC2); no candidate here changes that.

## Categories with no candidate searched
- Docs and README (T51): the Claude Docs and Notion connectors are connected but were not evaluated for README work.
- Claude Code plugin packaging (T40, T42): the plugin format was not searched beyond what tasks.md states. The Claude Code plugin marketplace format is not covered by any source above.

## Already connected here (for reference)
Claude in Chrome, Atlassian Rovo, IDE (getDiagnostics, executeCode), Claude Docs, Notion, Slack, Gmail, Google Drive and Calendar, Crunchbase, pdf-viewer, Apollo. Not connected: GitHub, Docker, Playwright, SQLite, git, MCP Inspector.

## Open items for the decomposition agent
- Unverified: latest `mcp` and `claude-agent-sdk` versions (T03, T16 pin).
- Unverified: Claude Code elicitation behaviour (T02 is the verification).
- Unverified: Atlassian Rovo beta or GA status and its rate limits.
- Unverified: sooperset/mcp-atlassian licence (from listings only).
- Unverified: Docker Desktop licence for personal use (tasks.md says ₹0).
