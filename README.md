# Jeb

Jeb is a coding agent that runs entirely inside
[Pythonista](http://omz-software.com/pythonista/) on iOS, and is meant to be
**built by bootstrapping an LLM against itself**. There is no separate build
step, no maintainer writing the harness by hand up front: you run a single
seed file, hand the model one instruction, and let it grow its own tool
loop, permission system, and console in place.

## What's in this repository

| Path | What it is |
|------|------------|
| [`jeb_bootstrap.py`](jeb_bootstrap.py) | The seed. A single ~2,150-line file with everything — provider client, agent loop, tools, permissions, runner, workspace, console — in one script. This is where a fresh bootstrap starts. |
| [`miniagent/`](miniagent/) | A worked example of what bootstrapping `jeb_bootstrap.py` can produce: the same functionality split into a clean multifile package, plus the documentation that makes it self-editable. |

`miniagent/` is not "the" answer — it's one outcome of running the process
below to completion once. Treat it as a reference, not a template to copy
verbatim.

## The first prompt

Point a coding agent at `jeb_bootstrap.py` and give it this as its first
task:

> Split `jeb_bootstrap.py` into a proper multifile module. Keep every
> behavior — the system prompt, the permission choices, the keychain scheme,
> the termination blocklist, the console commands, the first-run config
> migration — identical. Organize it into focused files with clean import
> boundaries, and write the documentation a future agent would need to
> understand and safely extend what you've built.

That single prompt is the whole bootstrap. Everything else — which module
names to use, how to split responsibilities, what to document — is left to
the agent doing the splitting. `miniagent/` is what one run of this produced;
`miniagent/docs/architecture.md` describes the shape it landed on, and
`miniagent/README.md` walks through using the result.

## Self-editing after the split

Once the split exists, the agent (and the project) keeps evolving the same
way: by editing its own harness in place, in the workspace it's running
from. The rules for doing that safely — read before you write, prefer
surgical edits, never remove the safety machinery, never touch the files
that grant it permissions — are written up for the agent itself, not for a
human maintainer, in [`miniagent/docs/self_editing.md`](miniagent/docs/self_editing.md).
Read that document before asking an agent to modify the harness further,
whether it's the one in `miniagent/` or a fresh split of your own.

Related documentation inside `miniagent/`:

- [`docs/architecture.md`](miniagent/docs/architecture.md) — how the pieces
  fit together after a split.
- [`docs/self_editing.md`](miniagent/docs/self_editing.md) — the guide a
  coding agent follows to modify its own harness.
- [`docs/jeb_md.md`](miniagent/docs/jeb_md.md) — how standing project
  instructions (`JEB.md`) are discovered and injected.
- [`INSTALL.md`](miniagent/INSTALL.md) — installing a split package in
  Pythonista.

## Why bootstrap instead of hand-write it

The point isn't that `jeb_bootstrap.py` is hard to split by hand — it's that
the harness should be able to build and rebuild itself using the same tool
loop it will later use for everything else. A harness that can't safely
restructure its own source under agent control hasn't proven the safety
model it's supposed to enforce on every other project. Bootstrapping it on
itself first is the test.
