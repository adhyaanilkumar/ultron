# Host spike (T02, throwaway)

Owner steps, about 5 minutes. Run from the repo root after merging T02 (paths below assume `C:\git\ultron`; adjust if different).

1. Register the server: `claude mcp add spike -- uv run python C:\git\ultron\spikes\host\server.py`
2. Paste the hook block from `spikes/host/settings.snippet.json` into `.claude/settings.local.json` (merge into any existing `hooks`). Restart Claude Code, then run `claude --version` and note it.
3. Ask Claude: `call the spike_probe tool`. Note: did a form appear (screenshot path or one line), did progress and resource-update messages show, and could the model answer the form without you? Save a screenshot inside the repo (for example `sdlc/v0/evidence/`) and record its path.
4. Type `! echo spike-bang-test`, then ask Claude to run `echo control-call` in Bash. Open `spikes/host/hook.log` and note which of the two lines are there.
5. Fill the PENDING cells (none may remain; every Evidence cell names an existing screenshot path or quotes `hook.log` lines) in `sdlc/v0/findings.md` (Host section), then `claude mcp remove spike` and remove the hook block.

