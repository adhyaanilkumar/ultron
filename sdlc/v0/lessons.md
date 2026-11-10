# Lessons from building V0 with ultron's own pipeline

We build V0 by playing ultron's pipeline with Claude Code subagents: the main session plays the engine, and
subagents play the ultron agents at their spec settings. Each lesson below is something ultron itself must handle.
Lessons that change approved V0 behaviour go to the owner first. The rest feed the next cycle's `intent.md` (Maintain).

| # | Date | What happened | Lesson for ultron | Where it lands |
|---|---|---|---|---|
| L1 | 2026-11-10 | The decomposition agent (Opus 5.5 high) ran for over an hour and wrote nothing to disk. The host couldn't see what it was doing, so a stall looked the same as slow work. | Agents that produce one big artifact must **write it incrementally**, so progress shows on disk. The engine's prompt for the spec and decomposition roles should say so. | V0: the decomposition and spec agent prompts (§11.3), in the task that builds them |
| L2 | 2026-11-10 | The "no new activity for 10 minutes" stop (§9.1) counts tool activity. A long single generation has no tool calls, so it looks like a stall even when it's working. | The progress signal should also count **streamed output** (tokens or thinking summaries), not only tool calls. Add a **wall-clock cap per role** (for example 3× the expected time) as a second trigger. | V0: §9.1 automatic stops, `ultron.toml` thresholds |
| L3 | 2026-11-10 | A message sent to the running agent queues until its next tool call, so it never arrived. | A running agent **can't be steered mid-generation**. That supports §9.1's rule that `ultron_stop` only ends or pauses work. Retry with the stop reason as feedback, and don't try to nudge. | Confirms §9.1. No change |
| L4 | 2026-11-10 | The owner had to ask why it was slow, and the host had no answer. | This is the case `ultron_monitor` and elapsed time per agent exist for (§9.1). The monitor should show **expected and elapsed time** for each role, so "slow" is visible without asking. | V0: `ultron_monitor` output |
