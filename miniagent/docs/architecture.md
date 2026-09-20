# Architecture

This document explains how MiniAgent is put together so that a coding agent
(or a human) can navigate the codebase confidently. All file paths are
relative to the package root (`miniagent/`).

## Big picture

```
                         ┌──────────┐
   user types prompt ──► │  app.py  │  console loop, construction
                         └────┬─────┘
                              │ builds
            ┌─────────────────┼───────────────────────────┐
            ▼                 ▼                            ▼
       ┌─────────┐      ┌──────────┐                ┌──────────┐
       │ agent.py │      │provider.py│                │config.py │
       │ the loop │      │ HTTP chat │                │ settings │
       └────┬─────┘      └──────────┘                └──────────┘
            │
            │ each step: provider.chat(messages, tools) → tool_calls
            │
            ▼
       ┌─────────┐  schemas + dispatch      ┌──────────────┐
       │ tools.py│ ─────────────────────────►│workspace.py  │ file ops
       └────┬────┘   permission gate         └──────────────┘
            │                                 ┌──────────────┐
            │ run_python ────────────────────►│runner.py    │ exec
            │                                 └──────────────┘
            │ permission decisions            ┌──────────────┐
            └────────────────────────────────►│permissions.py│
                                              └──────────────┘
```

## Construction (`app.py`)

`run(project_root)` is the single public entry point, re-exported from
`__init__.py`. It:

1. Resolves and validates `project_root`.
2. Determines the application state directory via `config.default_state_dir()`.
3. Loads (or first-run-creates) a `Config`.
4. Loads the API key for the selected provider from the keychain (prompting
   if missing). Keys are stored per provider; legacy hash-of-base-URL
   entries are migrated automatically.
5. Constructs the object graph:
   - `Workspace(root)` — path confinement
   - `Permissions(state_dir, root)` — permission store
   - `Runner(workspace)` — in-process execution
   - `Tools(workspace, permissions, runner)` — tool dispatch + gating
   - `Provider(config, api_key)` — HTTP client
   - `Agent(provider, tools, system_prompt)` — conversation loop
5. Before constructing the `Agent`, it calls `load_jeb_md_context(root)` to
   discover any `JEB.md` files (global then local) and passes the result to
   `build_system_prompt()` as `extra_context`.
6. Enters `_console_loop`, which reads lines and either handles a `:command`
   or passes the text to `agent.turn()`.

Nothing happens at import time — all construction is inside `run()`.

## JEB.md discovery (`app.py`)

`load_jeb_md_context(project_root)` looks for `JEB.md` in two places:

1. **Global** — `~/miniagent/JEB.md`, located via `Path.home()` inside
   `app.py`. This applies to every project on the install.
2. **Local** — `<workspace>/JEB.md`, in the project root. This applies to the
   current project only.

Each file is read, trimmed, and truncated at 32,000 characters
(`_MAX_JEB_MD_CHARS`). The two sections are wrapped in labelled headers
(`# Global JEB.md (~/miniagent)` / `# Project JEB.md (workspace)`) and
joined with a `---` divider, global first. The result is passed as
`extra_context` to `build_system_prompt()`, which appends it to the system
prompt. Missing files are silently skipped.

The `:context` console command (`_print_jeb_context`) prints which files were
found and the combined text, so the user can verify what the model received.
See [`jeb_md.md`](jeb_md.md) for the user-facing guide.

## The agent loop (`agent.py`)

`Agent.turn(user_text)`:

1. Appends the user message.
2. Calls `provider.chat(messages, tools=schemas)` in a loop (up to
   `max_steps`, default 50).
3. Extracts the assistant message and normalises it via
   `Provider.split_content()`. Reasoning may arrive as a dedicated
   `reasoning_content`/`reasoning` field or as typed thinking blocks inside
   a block-list `content`; either way it is printed under `Reasoning:`.
   Text content is printed under `Assistant:`. The original message shape
   (string or block list) is preserved in the history for round-tripping.
4. If there are no `tool_calls`, the turn is done — the extracted text is returned.
5. For each `tool_call`, it calls `tools.dispatch(call)`, appends the result
   as a `tool`-role message, and loops back to the provider.

The system prompt is built by `build_system_prompt(project_root, extra_context="")`,
which injects the absolute project root and the environment constraints. When
`extra_context` is non-empty (JEB.md content discovered by `app.py`), it is
appended to the prompt after a short header explaining that global instructions
come first and local instructions second. See [`jeb_md.md`](jeb_md.md).

## The provider (`provider.py`)

A thin `requests`-based client for any OpenAI-compatible
`/chat/completions` endpoint. It:

- Builds the URL from `base_url` + `chat_path`.
- Adds auth headers only when `auth_enabled` and a key is present.
- Sends `model`, `messages`, `tools`, optional `tool_choice`, optional
  `reasoning_effort`, and any `extra_body` merge.
- Extracts `choices[0].message` via the static `extract_message()`.
- Splits assistant content into text and thinking parts via the static
  `split_content()`. This normalises the typed content blocks some
  providers return instead of a plain string — e.g.
  `{"type": "thinking", "thinking": [...], "closed": true}` — so the agent
  displays reasoning as text instead of dumping raw JSON.

`set_effort(level)` controls the `reasoning_effort` field sent to providers
that understand it. It is not persisted.

## Configuration (`config.py`)

A single JSON file (`config.json`) in the state directory holds settings for
one or more providers:

```json
{
  "provider": "mistral",
  "model": "zai-glm-5-3",
  "providers": {
    "mistral": {
      "base_url": "https://api.mistral.ai/v1",
      "chat_path": "chat/completions",
      "models": ["zai-glm-5-3"],
      "auth_enabled": true,
      "...": "..."
    }
  }
}
```

On load, `Config` normalises the file: provider entries are merged with
`PROVIDER_DEFAULTS`, the selection is validated, and legacy flat configs
(`base_url` / `model` at the top level) are migrated automatically — the one
provider is named after its base-URL hostname and its model becomes the
single entry in that provider's `models` list. `__getattr__` exposes the
selected provider's settings as attributes (`config.base_url`,
`config.auth_enabled`, ...), so `Provider` is built from a `Config` without
knowing about the multi-provider layout.

`select()` changes the provider/model selection in memory (used by
`:model`); `set()` validates, mutates and persists (used by `:config set`).
Setting `model` auto-appends it to the provider's `models` list; setting
`provider` switches to that provider's remembered model. No credentials are
stored here.

`first_run_setup()` either silently migrates a legacy `miniagent_config.json`
/ `miniagent_config.py` from the project root (the old `jeb_bootstrap.py`
scheme) or prompts interactively for provider name, base URL, models, chat
path, and auth.

`default_state_dir()` picks `~/Documents/miniagent`, then `~/.miniagent`,
then a temp dir — never hard-coding an iOS container UUID.

## Permissions (`permissions.py`)

Four capabilities: `write`, `edit`, `overwrite`, `run_python`. Decisions are
keyed by the canonical project path so distinct projects have distinct
policies.

The store lives at `permissions.json` in the state directory — outside any
project, so the agent cannot edit its own permissions through the file
tools.

`authorize(capability, title, details)` checks persistent → session →
interactive prompt, in that order. The prompt offers:

| Key | Meaning |
|-----|---------|
| `y` | allow once |
| `s` | allow for this session |
| `a` | always allow for this workspace (persisted) |
| `n` | deny once |
| `d` | deny for this session |
| `x` | always deny for this workspace (persisted) |

## Tools (`tools.py`)

`TOOL_SCHEMAS` is the list passed to the model. `Tools.dispatch(call)`:

1. Parses JSON arguments.
2. Looks up the capability in `CAPABILITY_MAP`. Read-only tools (`list_files`,
   `read_file`, `search_files`) are absent from the map and skip the gate.
   So does `clean_up`: it only moves files the agent created with
   `create_file` in the current session (tracked in memory by the `Tools`
   instance), and it moves them into `.to_delete/` rather than deleting, so
   no permission prompt is needed.
3. If a capability is required, builds a human-readable preview via
   `_preview()` and calls `permissions.authorize()`. A denial returns
   `{"ok": false, "denied": true}` to the model without executing.
4. Calls the implementation in `_execute()`, which delegates to `Workspace`
   or `Runner`.
5. Returns a JSON string (truncated at 40k chars).

Errors are caught and returned as JSON — never as exceptions to the model.

## Workspace (`workspace.py`)

`Workspace(root)` canonicalises the root once. `resolve(path)` anchors any
relative path at the root, resolves symlinks, and rejects anything that
falls outside the root. This is the single chokepoint for confinement.

Operations:
- `list_files` — flat or recursive with `max_depth`.
- `read_file` — returns line-numbered text with an optional line range.
- `search_files` — literal or regex content search beneath a directory;
  returns matching lines with path and line number, capped at
  `max_results` (default 200). Skips non-UTF-8, oversized (> 2 MiB), and
  confinement-violating paths, counting them in `files_skipped`.
- `create_file` — fails if the path already exists; writes atomically.
- `edit_file` — replaces exactly one unique occurrence; fails on zero or
  multiple matches.
- `overwrite_file` — replaces entire file contents.
- `clean_up` — moves a file into the workspace-level `.to_delete/` trash
  folder under a collision-free name (`name-1.ext`, ...); refuses paths
  inside that folder.
- `preview_edit` / `preview_overwrite` — produce unified diffs for the
  permission prompt, without mutating.

Atomic writes use `tempfile.mkstemp` + `os.replace` so a crash never leaves
a half-written file.

## Runner (`runner.py`)

`Runner.run(path, args)` executes a `.py` file in-process. Because
Pythonista has no usable subprocess boundary for this, it is **not
sandboxed**. Two layers of defense:

1. **Static preflight** (`_preflight`): an AST walk that blocks calls to
   `exit`, `quit`, `sys.exit`, `os._exit`, `os.abort`, `os.kill`,
   `os.fork`, `os.exec*`, and `raise SystemExit` / `raise BaseException`.
   It also blocks `from sys import exit`, `from builtins import exit/quit`,
   and `from os import _exit/abort/kill/fork/exec*`.

2. **Runtime replacement** (`_install_runtime_blocks`): temporarily patches
   `builtins.exit`, `builtins.quit`, `sys.exit`, `os._exit`, `os.abort`, and
   `os.kill` with callables that raise `RuntimeError`. A `SystemExit`
   exception is caught and reported. Everything is restored in `finally`.

Execution captures stdout/stderr into buffers (truncated at 20k chars),
sets `sys.argv`, changes cwd to the workspace root, and purges workspace
modules from `sys.modules` afterward so re-runs pick up file changes. The
console-critical builtins `input` and `print` are also saved and restored
around each run, so a script that rebinds them cannot poison the interactive
console loop after the run ends.

## The legacy single file (`jeb_bootstrap.py`)

The original `jeb_bootstrap.py` (kept at the repository root for reference)
contained all of the above in one ~2,150-line file. The multifile package is
a faithful refactor: the system prompt, the permission prompt choices, the
keychain service scheme, the termination block list, the console commands,
and the first-run migration logic all match the original. `jeb_bootstrap.py`
is not imported by the package and exists only as history.
