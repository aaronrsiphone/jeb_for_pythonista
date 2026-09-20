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

| Step | What it was |
|------|-------------|
| 1 | [`jeb_bootstrap.py`](../jeb_bootstrap.py) — a single ~2,150-line file containing everything: provider, agent loop, tools, permissions, runner, workspace, and the console. Lives at the repository root. |
| 2 | This multifile package — the same functionality split into focused modules with clean import boundaries. |
| 3 | This documentation — a guide so a coding agent can understand and self-edit the harness. |

The original single-file bootstrap (`jeb_bootstrap.py`, at the repository
root) is kept for reference and migration. It is not imported by this
package. See the [repository README](../README.md) for how a fresh
bootstrapping run is meant to produce something like this package.

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

### Give the agent standing instructions

Create a `JEB.md` file to provide project-specific instructions that are
automatically injected into the system prompt. A **global** `JEB.md` goes in
`~/miniagent/` (applies to all projects); a **local** `JEB.md` can go in the
project workspace root (applies to that project only).
Global comes first, local comes second. See
[`docs/jeb_md.md`](docs/jeb_md.md) for details.

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
| `clean_up` | none | Move files the agent created this session into the workspace `.to_delete/` folder (recoverable; refuses anything else) |
| `run_python` | run_python | Execute a `.py` file in-process (not sandboxed) |

Read-only tools require no permission. Mutating and execution tools are
gated by an interactive prompt with per-once, per-session, and per-workspace
(persistent) choices. `clean_up` is also ungated: it only accepts files the
agent itself created with `create_file` in the current session, and it moves
them into `.to_delete/` in the workspace instead of deleting, so nothing is
lost — empty that folder whenever you like.

## Package layout

All paths below are relative to the package root (`miniagent/`).

| File | Responsibility |
|------|----------------|
| `__init__.py` | Package entry; re-exports `run` from `app.py` |
| `_jeb.py` | Example one-line launcher script |
| `app.py` | Construction, JEB.md discovery, and the interactive console loop |
| `agent.py` | Agent loop: model/tool orchestration and system prompt |
| `provider.py` | Provider-neutral OpenAI-compatible HTTP client |
| `config.py` | Configuration load/save and first-run setup |
| `permissions.py` | Permission layer with interactive prompting |
| `tools.py` | Tool schemas, dispatch, and permission gating |
| `runner.py` | In-process Python execution with termination blocking |
| `workspace.py` | Workspace confinement and low-level file operations |
| `template_JEB.md` | Template for the global standing instructions (copy to `~/miniagent/JEB.md`) |
| `INSTALL.md` | Installation instructions |
| `README.md` | This file |
| `docs/` | Architecture and self-editing guides |

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — How the pieces fit
  together, data flow, and module responsibilities.
- [`docs/self_editing.md`](docs/self_editing.md) — Guide for a coding agent
  that wants to understand, modify, and extend this harness in place.
- [`docs/jeb_md.md`](docs/jeb_md.md) — How `JEB.md` files work: automatic
  discovery of global and project-local standing instructions.
- [`INSTALL.md`](INSTALL.md) — How to install the package in Pythonista.

## Where state lives

MiniAgent keeps its own state **outside** your projects:

```
~/Documents/miniagent/
    config.json        # provider settings (no API key)
    permissions.json   # per-project persistent permission decisions
```

API keys live in the Pythonista keychain, one entry per provider
(`MiniAgent:provider:<name>`), so each provider can use a different key.
Keys stored by older versions (keyed by a hash of the base URL) are
migrated to the per-provider scheme automatically the first time the
provider is used.

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
- **In-process execution** — `run_python` executes inside the same
  Pythonista process. It is explicitly **not** sandboxed. The system prompt
  tells the model this.
