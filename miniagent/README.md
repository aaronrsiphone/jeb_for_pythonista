# MiniAgent

A small, self-contained coding agent that runs entirely inside
[Pythonista](http://omz-software.com/pythonista/) on iOS. It talks to any
OpenAI-compatible chat completions endpoint, exposes a handful of file and
code-execution tools to the model, and confines every file operation to a
project workspace you choose.

MiniAgent has no third-party dependencies beyond `requests`, which Pythonista
ships with. The whole thing is a single importable Python package.

---

## What it is

MiniAgent is an **agent harness** — the loop, the tools, the permission
gating, and the console — that lets an LLM act on a directory of your code.
You drop a one-line launcher script into any project folder, run it, and
chat with the agent about that folder. The agent can list files, read them,
search their contents, create new ones, make focused edits, and execute
Python files in-process — each mutating or execution step gated by an
interactive permission prompt.

It is designed to be **bootstrapped by a coding agent itself**. An
LLM-powered agent can read the documentation in the [`docs/`](docs/) folder,
understand the architecture, and then use the file tools to modify or extend
MiniAgent in place. See [`docs/self_editing.md`](docs/self_editing.md) for
that guide.

## Project history

This package is organized as focused modules with clean import boundaries,
plus documentation so a coding agent can understand and self-edit the
harness.

## Quick start

### Install the package

Copy the `miniagent/` directory into Pythonista's user site-packages so it is
importable from any project. Full instructions are in
[`INSTALL.md`](INSTALL.md).

### Launch from any project

Drop this tiny launcher (call it `jeb.py` or whatever you like) into a
project directory and run it:

```python
from pathlib import Path
from miniagent import run
if __name__ == "__main__":
    run(project_root=Path(__file__).resolve().parent)
```

This starts an interactive console scoped to that directory.

### Configure

MiniAgent supports multiple providers in `config.json`. Each provider has
its own endpoint settings, API key, and a list of available models:

```json
{
  "provider": "mistral",
  "model": "zai-glm-5-3",
  "providers": {
    "mistral": {
      "base_url": "https://api.mistral.ai/v1",
      "chat_path": "chat/completions",
      "models": ["zai-glm-5-3", "mistral-medium-latest"],
      "auth_enabled": true,
      "auth_header": "Authorization",
      "auth_prefix": "Bearer ",
      "extra_headers": {},
      "extra_body": {},
      "timeout": 120
    },
    "openrouter": {
      "base_url": "https://openrouter.ai/api/v1",
      "models": ["openai/gpt-4o", "anthropic/claude-3.5-sonnet"]
    }
  }
}
```

`provider` and `model` select the active combination; unspecified
per-provider keys fall back to sensible defaults. On first run you are
prompted for a provider name, base URL, models, and auth settings. Each
provider's API key is stored in the Pythonista keychain, never on disk
inside your project. Change settings with `:config`, store the current
provider's key with `:key`, and switch models mid-session with `:model`
(selection is session-only and is not written back to `config.json`).

A separate top-level `vision_model` key selects the provider/model pair
the `ask_image` tool sends images to — format
`<provider>/<model-name>` (e.g. `"mistral/pixtral-12b-2409"`), or a bare
model name to use the selected provider's endpoint. It is optional; when
absent, `ask_image` reports that no vision model is configured. Change it
with `:config set vision_model <provider>/<model-name>`.

### Give the agent standing instructions

Create a `JEB.md` file to provide project-specific instructions that are
automatically injected into the system prompt. A **global** `JEB.md` goes in
`~/Documents/miniagent/` (applies to all projects); an original copy in
`~/miniagent/` is still read, and when both exist the two are merged with
conflicts resolved in favour of the Documents version. A **local** `JEB.md`
can go in the project workspace root (applies to that project only).
Global comes first, local comes second. See
[`docs/jeb_md.md`](docs/jeb_md.md) for details.

### Resume a past session

Every conversation is recorded as it happens to a JSON-lines file under
`~/Documents/miniagent/sessions/<workspace>/` (one file per session, append
only — see "Where state lives" below). Type `:resume` to browse previous
sessions for the current workspace and continue one: the list shows four
sessions per page with the first prompt of each; pick with 1-4, turn pages
with 0 (previous) and 5 (next), or press Enter to cancel. The chosen
session's message history is reinstated behind the *current* system prompt
(so you get the current JEB.md instructions, not stale ones), the
conversation picks up where it left off, and new messages are appended to
that session's log. `:reset` starts a fresh log file, so each conversation
segment stays separately resumable.

## Console commands

The full, generated list of every command — including aliases and a
one-line summary of each — is in [`docs/reference.md`](docs/reference.md),
produced straight from the command registry so it can never omit or
misdescribe one. Highlights: `:help` lists everything at the console;
`:resume` browses and continues a past session; `:undo` and `:checkpoints`
are the self-editing safety net (see below); `:verbose` switches between the
compact and expanded console renderers; anything not starting with `:` is
sent to the agent as a prompt.

## Tools the agent has

The full, generated list of every tool — its permission capability and its
exact description, read straight from the tool registry — is in
[`docs/reference.md`](docs/reference.md). In outline: three read-only tools
(`list_files`, `read_file`, `search_files`) need no permission; `create_file`,
`edit_file`, `multi_edit` and `overwrite_file` mutate the workspace;
`run_python` executes in-process; `ask_image` asks the configured vision
model about image(s). `clean_up` and `knowledge` are also ungated —
`clean_up` only moves files the agent created this session into the
workspace's `to_delete/` folder (recoverable; refuses anything else), and
`knowledge` only reads.

Mutating and execution tools are gated by an interactive prompt with
per-once, per-session, and per-workspace (persistent) choices. Any choice
can be followed by a comment that is passed to the agent — for example
`n. Write the file to this path foo/bar` denies the request but redirects
the agent, and `y. But also can you check xyz` approves it with an extra
instruction attached to the tool result. `ask_image` is gated even though it
reads rather than writes: it uploads image data — possibly photos or
clipboard images from outside the workspace — to the vision provider's API,
and the prompt preview shows exactly which images are about to be sent
where.

`edit_file` reports a near-miss with a diff against the closest-matching
region of the file instead of a bare "not found", and takes
`replace_all`/`occurrence` for a text occurrence that is not unique.
`multi_edit` applies several edits to one file atomically — either they all
apply, or the file is left completely untouched — for one permission prompt
instead of several.

## Package layout

All paths below are relative to the package root (`miniagent/`). This is a
summary for orientation; the generated, always-current module-by-module
table (each module's own one-line purpose, read straight from its
docstring) is in [`docs/reference.md`](docs/reference.md).

| File / directory | Responsibility |
|------|----------------|
| `__init__.py` | Package entry; re-exports `run` from `app.py` |
| `_jeb.py` | Template one-line launcher script (copy into a project as `jeb.py`) |
| `app.py` | Construction only: builds the object graph and `Context`, then enters the console loop (~100 lines) |
| `context.py` | The live per-session wiring (`ctx.provider`, `ctx.renderer`, ...) console commands mutate |
| `agent.py` | Agent loop: `Agent.turn()` is a generator yielding `events.py` events |
| `events.py` | Typed events exchanged between the agent loop and a front end |
| `tools/` | One file per tool, self-registered via `@tool` (`registry.py`); `dispatch.py` holds the dispatch generator and permission gate |
| `console/` | The console loop (`loop.py`), the command registry (`registry.py`), one file per command area under `commands/`, and the `:resume` pager (`resume.py`) |
| `ui/` | Renderers over the event stream: `console.py` (compact, default), `verbose.py` (expanded), `headless.py` (scripted, for tests) |
| `checkpoints.py` | Self-editing undo safety net: per-turn file snapshots outside the workspace, restored by `:undo` |
| `permissions.py` | Permission *policy* only (`decide`/`parse_answer`/`apply_answer`) — no I/O, no prompting |
| `jebmd.py` | `JEB.md` discovery and the global/local merge engine |
| `keys.py` | Keychain / API-key loading, storage and legacy migration |
| `provider.py` | Provider-neutral OpenAI-compatible HTTP client |
| `vision.py` | Vision question-answering collaborator behind `ask_image` |
| `config.py` | Configuration load/save and first-run setup |
| `sessions.py` | Append-only JSONL session logs and `:resume` loading |
| `runner.py` | In-process Python execution with termination blocking and interactive-input hang prevention |
| `workspace.py` | Workspace confinement, atomic writes with a `.py` compile gate, `edit_file`/`multi_edit` |
| `knowledge.py` | Collaborator behind the read-only `knowledge` tool |
| `gendocs.py` | Generates `docs/reference.md` from the tool/command registries and every module's docstring |
| `tests/` | The in-package test suite — standalone `test_*.py` scripts plus `run_all.py` to run them all (kept, not deleted; see [`docs/testing.md`](docs/testing.md)) |
| `template_JEB.md` | Template for the global standing instructions (copy to `~/Documents/miniagent/JEB.md`) |
| `JEB.md` | Standing self-editing rules injected into the system prompt when the workspace is this package itself |
| `INSTALL.md` | Installation instructions |
| `README.md` | This file |
| `docs/` | Architecture, self-editing, testing, JEB.md, and generated-reference guides |

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — How the pieces fit
  together: the event stream, the tool/command registries, the renderers,
  checkpoints, and every module's responsibility.
- [`docs/reference.md`](docs/reference.md) — **Generated.** The full tool
  table, command table, and module layout, produced by `gendocs.py` from the
  live code. Regenerate after a change with `run_python miniagent/gendocs.py`.
- [`docs/self_editing.md`](docs/self_editing.md) — Guide for a coding agent
  that wants to understand, modify, and extend this harness in place.
- [`docs/testing.md`](docs/testing.md) — How to test the harness without
  interactive prompts or hangs: driving the agent loop with the `Headless`
  renderer, testing `Permissions` directly, the few places `builtins.input`
  is still the right tool, and the `InteractiveInputBlocked` fail-fast
  behavior of `run_python`.
- [`docs/jeb_md.md`](docs/jeb_md.md) — How `JEB.md` files work: automatic
  discovery of global and project-local standing instructions.
- [`INSTALL.md`](INSTALL.md) — How to install the package in Pythonista.

## Where state lives

MiniAgent keeps its own state **outside** your projects:

```
~/Documents/miniagent/
    config.json        # provider settings (no API key)
    permissions.json   # per-project persistent permission decisions
    sessions/          # recorded conversations, one JSONL file per session
        <workspace-path-with-dashes>/
            2025-06-07_21-14-03.jsonl
    checkpoints/       # per-turn file snapshots for :undo, one dir per workspace
        <workspace-path-with-dashes>/
            <turn-id>/
                manifest.json
                0000.bin
                ...
```

The `<workspace-path-with-dashes>` directory is the workspace path relative
to `~/Documents` with slashes replaced by dashes, so sessions from different
projects never mix. Each session file gets a `meta` first line (start time,
workspace, provider, model) followed by one line per message — the file is
only ever appended to. API keys live in the Pythonista keychain, one entry
per provider (`MiniAgent:provider:<name>`), so each provider can use a
different key. Keys stored by older versions (keyed by a hash of the base
URL) are migrated to the per-provider scheme automatically the first time
the provider is used.

## Safety model

- **Workspace confinement** — every file path is resolved against the
  project root and rejected if it escapes (`../` traversal, absolute paths
  outside root, symlink escapes).
- **Permission gating** — write, edit, overwrite, run_python, and ask_image
  each require interactive authorization with once / session / always
  granularity. Permission *policy* (`permissions.py`) does no I/O; the
  actual prompting is a renderer's job (see
  [`docs/architecture.md`](docs/architecture.md)).
- **Checkpoints** — every mutating file operation snapshots the file's
  prior bytes outside the workspace before writing, so `:undo` can restore
  everything one turn changed (and `:checkpoints` lists what is retained).
  This is the self-editing safety net: an agent editing the harness from
  inside the harness has a rollback.
- **A compile gate on Python writes** — any write to a `.py` path is
  `compile()`d before the atomic rename that would make it live; a syntax
  error refuses the write (nothing touches disk) and returns the exact
  line/problem to the model instead of leaving a harness that will not
  start.
- **Termination blocking** — the runner does a static AST preflight and
  runtime builtin replacement to stop `exit()`, `sys.exit()`, `os._exit()`,
  `os.kill`, `os.fork`, `os.exec*`, and `raise SystemExit` from killing
  Pythonista. This is defense-in-depth, not a sandbox.
- **Hang prevention** — `run_python` captures the script's output and
  disables interactive input for the duration of the run: `input()` and
  `sys.stdin` reads raise `InteractiveInputBlocked` immediately instead of
  blocking forever on an invisible prompt. The originals are restored
  afterwards, so the interactive console is unaffected. See
  [`docs/testing.md`](docs/testing.md).
- **In-process execution** — `run_python` executes inside the same
  Pythonista process. It is explicitly **not** sandboxed. The system prompt
  tells the model this. `run_python` also never evicts `miniagent`'s own
  modules from `sys.modules` after a run, even when the workspace root is
  the package's own install location (the self-editing case) — otherwise a
  validation script that imports `miniagent.tools` would hot-reload the
  *running* harness mid-session.
