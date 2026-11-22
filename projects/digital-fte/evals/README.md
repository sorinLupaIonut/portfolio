# Evals

Trigger evals for the Worker's skills, kept here rather than inside the skill folders.

`load_skill` copies a skill's **entire** directory into the sandbox, so anything sitting
next to `SKILL.md` is shipped to the Worker at runtime and paid for on every load. Eval
sets and their results are build-time artifacts: they belong in the repo, not in the
container.

## `summarize-ticket/`

- `trigger-evals.json` — 20 cases, 10 that must fire the skill and 10 near-misses that
  must not. The negatives are the interesting half: "write a handoff note for the deploy",
  "tldr this slack thread", "summarize this vendor contract" all share the obvious
  keywords and must stay quiet.
- `trigger-results.json` — the raw run: 3 runs per case, 12 train + 8 held out, passing
  20/20 on the first iteration with no change to the description.

## Re-running them

The optimizer ships with `skill-creator`, but its scripts assume macOS/Linux and need
three patches to run on Windows: `select.select()` on a pipe (sockets only on Windows),
a bare `"claude"` in `Popen` (the launcher is `claude.CMD`), and `read_text()` without an
encoding (the default is the ANSI codepage, which mangles the em-dashes in a description).

A fourth change is not about Windows: `skill-creator` detects triggering by writing a
temporary file into `.claude/commands/` and watching for a call to it, and Claude Code
2.1.207 no longer surfaces that file as a skill. Point the detection at the real skill
instead, writing each candidate description into its `SKILL.md` for the duration of the
round.
