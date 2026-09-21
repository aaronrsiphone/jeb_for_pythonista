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

| Command | Purpose |
|---------|---------|
| `:help` | Show available commands |
| `:reset` | Reset the conversation to the system prompt |
| `:config` | Print current configuration |
| `:config set KEY VALUE` | Set and save a config value |
| `:key` | Show whether an API key is stored |
| `:key set VALUE` | Store the API key in the keychain |
| `:model [NUMBER]` | List configured models as `<provider>/<model-name>` and select one for this session |
| `:effort [low\|medium\|high\|xhigh\|max]` | Set reasoning effort level |
| `:perms` | Show permission state |
| `:clear-perms` | Clear session and persistent permissions |
| `:files` | List top-level project files |
| `:workspace` | Print the project workspace path |
| `:context` | Show JEB.md files found and the combined context loaded |
| `:resume` | List recorded sessions (4 per page) and reinstate one to continue it |
| `:quit` | Stop MiniAgent |

Anything that is not a command is sent to the agent as a prompt.

## Tools the agent has

| Tool | Permission | Description |
|------|------------|-------------|
| `list_files` | none | List files and directories (recursive option) |
| `read_file` | none | Read a UTF-8 text file with line numbers and optional range |
| `search_files` | none | Search file contents for a literal or regex pattern; returns matches with path and line number |
| `create_file` | write | Create a new file; fails if it already exists |
| `edit_file` | edit | Replace exactly one unique text occurrence in a file |
| `overwrite_file` | overwrite | Replace the entire contents of an existing file |
| `clean_up` | none | Move files the agent created this session into the workspace `to_delete/` folder (recoverable; refuses anything else) |
| `run_python` | run_python | Execute a `.py` file in-process (not sandboxed; interactive input disabled — see [`docs/testing.md`](docs/testing.md)) |
| `ask_image` | ask_image | Ask the configured vision model a question about images: workspace files, http(s) URLs, photo-library images (`photo`, negative = from the end), and/or the clipboard image. Uses config.json's `vision_model` (`<provider>/<model-name>`); oversized images are shrunk/re-encoded automatically |

Read-only tools require no permission. Mutating and execution tools are
gated by an interactive prompt with per-once, per-session, and per-workspace
(persistent) choices. Any choice can be followed by a comment that is
passed to the agent — for example `n. Write the file to this path foo/bar`
denies the request but redirects the agent, and `y. But also can you check
xyz` approves it with an extra instruction attached to the tool result.
`clean_up` is also ungated: it only accepts files the
agent itself created with `create_file` in the current session, and it moves
them into `to_delete/` in the workspace instead of deleting, so nothing is
lost — empty that folder whenever you like. `ask_image` is gated even
though it reads rather than writes: it uploads image data — possibly
photos or clipboard images from outside the workspace — to the vision
provider's API, and the prompt preview shows exactly which images are about
to be sent where.

## Package layout

All paths below are relative to the package root (`miniagent/`).

| File | Responsibility |
|------|----------------|
| `__init__.py` | Package entry; re-exports `run` from `app.py` |
| `_jeb.py` | Example one-line launcher script |
| `app.py` | Construction, JEB.md discovery, and the interactive console loop |
| `agent.py` | Agent loop: model/tool orchestration and system prompt |
| `provider.py` | Provider-neutral OpenAI-compatible HTTP client |
| `vision.py` | Vision question-answering collaborator behind `ask_image` (multimodal payload building, image loading/shrinking, API key via injected loader) |
| `config.py` | Configuration load/save and first-run setup |
| `permissions.py` | Permission layer with interactive prompting |
| `sessions.py` | Append-only JSONL session logs and `:resume` loading |
| `tools.py` | Tool schemas, dispatch, and permission gating |
| `runner.py` | In-process Python execution with termination blocking and interactive-input hang prevention |
| `workspace.py` | Workspace confinement and low-level file operations |
| `tests/` | The in-package test suite — standalone `test_*.py` scripts plus `run_all.py` to run them all (kept, not deleted; see [`docs/testing.md`](docs/testing.md)) |
| `template_JEB.md` | Template for the global standing instructions (copy to `~/Documents/miniagent/JEB.md`) |
| `INSTALL.md` | Installation instructions |
| `README.md` | This file |
| `docs/` | Architecture, self-editing, testing, and JEB.md guides |

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — How the pieces fit
  together, data flow, and module responsibilities.
- [`docs/self_editing.md`](docs/self_editing.md) — Guide for a coding agent
  that wants to understand, modify, and extend this harness in place.
- [`docs/testing.md`](docs/testing.md) — How to test the harness (and
  prompt-driven code) without interactive prompts or hangs: scripted
  prompts, `builtins.input` guards, and the `InteractiveInputBlocked`
  fail-fast behavior of `run_python`.
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
- **Permission gating** — write, edit, overwrite, and run_python each
  require interactive authorization with once / session / always granularity.
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
  tells the model this.
