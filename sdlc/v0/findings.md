# V0 findings

## Host

Spike: `spikes/host/` (see its README). Every cell marked PENDING owner is filled after the owner steps; record what was observed, including "no".

| Question | Result | Claude Code version | Evidence | Implied decision for T36, T38, T41 |
|---|---|---|---|---|
| Elicitation: does a form appear, and can the host model answer it without the owner? | PENDING owner | PENDING owner | PENDING owner (screenshot path or description) | PENDING owner (e.g. unavailable: T38 uses the terminal fallback for every gate) |
| Notifications: are progress and resource-update messages shown? | PENDING owner | PENDING owner | PENDING owner | PENDING owner |
| `!` commands: does the PreToolUse hook see them? | PENDING owner | PENDING owner | PENDING owner (`hook.log` lines, incl. the model-issued Bash control) | PENDING owner |
| Version: which Claude Code version was tested? | PENDING owner (`claude --version`) | PENDING owner | PENDING owner | n/a |
