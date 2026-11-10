# Lessons from building V0 with ultron's own pipeline

We build V0 by playing ultron's pipeline with Claude Code subagents: the main session plays the engine, and
subagents play the ultron agents at their spec settings. Each lesson below is something ultron itself must handle.
Lessons that change approved V0 behaviour go to the owner first. The rest feed the next cycle's `intent.md` (Maintain).

| # | Date | What happened | Lesson for ultron | Where it lands |
|---|---|---|---|---|
| L1 | 2026-11-10 | T35's mascot passed 80 tests, but the owner saw it was asymmetric. Cause: a transparent cell prints a space while the previous cell's background colour is still set, so the space shows as a coloured block. The testing agent decoded each cell as if colours reset between cells. A real terminal doesn't do that. | (1) The testing agent must judge output the way the real consumer does: for terminal output, decode ANSI with state carried over, or take a screenshot. (2) **Visual acceptance criteria need a visual check.** The decomposition agent should name the tool a criterion needs (a screenshot through Claude in Chrome or computer use, or a terminal emulator library). tasks.md has no field for that today. | Next cycle: add a `verify_with` field per criterion (FR5). V0: T35's retry and the T48 demo |
| L2 | 2026-11-10 | The owner found T35's defect at the `result` gate, after both agents passed it. | The human result gate catches what agent tests miss, especially look and feel. Keep `result` human by default (§8). An `unattended` preset should warn that visual criteria go unchecked. | Confirms §8. Next cycle: warn on `unattended` when tasks have visual criteria |
