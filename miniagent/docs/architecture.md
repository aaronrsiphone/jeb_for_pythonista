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
            │                                 ┌──────────────┐
            │ ask_image ─────────────────────►│vision.py    │ vision QA
            │                                 └──────────────┘
            │ permission decisions            ┌──────────────┐
            └────────────────────────────────►│permissions.py│
                                              └──────────────┘
    agent.py also hands every appended message to a SessionLogger
    (sessions.py), which appends it to a per-workspace JSONL log;
    app.py's :resume loads such a log back into agent.messages.
```

## Construction (`app.py`)

`run(project_root)` is the single public entry point, re-exported from
`__init__.py`. It:

1. Resolves and validates `project_root`.
2. Determines the application state directory via `config.default_state_dir()`.
3. Loads (or first-run-creates) a `Config`.
4. Loads the API key for the selected provider from the keychain (prompting
   if missing). Keys are stored per provider; legacy hash-of-base-URL
   entries are migrated automatically. `_load_api_key(config, provider)`
   can also fetch another provider's key — the vision tool uses this to
   load the key of its own provider, which may differ from the selected one.
5. Constructs the object graph:
   - `Workspace(root)` — path confinement
   - `Permissions(state_dir, root)` — permission store
   - `Runner(workspace)` — in-process execution
   - `Vision(config, load_key)` — vision collaborator for `ask_image`
     (resolves its own provider/model from config.json's `vision_model`
     key; `load_key` loads that provider's key through the same keychain
     scheme)
   - `Tools(workspace, permissions, runner, vision=vision)` — tool dispatch
     + gating
   - `Provider(config, api_key)` — HTTP client
   - `SessionLogger(root, state_dir, provider, model)` — session recorder
   - `Agent(provider, tools, system_prompt, recorder=session_logger)` —
     conversation loop; every message it appends is recorded
5. Before constructing the `Agent`, it calls `load_jeb_md_context(root)` to
   discover any `JEB.md` files (global then local) and passes the result to
   `build_system_prompt()` as `extra_context`.
6. Enters `_console_loop`, which reads lines and either handles a `:command`
   or passes the text to `agent.turn()`.

Nothing happens at import time — all construction is inside `run()`.

## JEB.md discovery (`app.py`)

`load_jeb_md_context(project_root)` looks for `JEB.md` in three places:

1. **Global, Documents version** — `~/Documents/miniagent/JEB.md`,
   located via `Path.home()` inside `app.py` (`_global_jeb_md_paths`).
   This applies to every project on the install.
2. **Global, original** — `~/miniagent/JEB.md`, kept for backward
   compatibility. When both global files exist they are merged into a
   single section by `_merge_global_jeb_md_texts`: exact duplicates are
   kept once, near-duplicate units (similarity ≥
   `_CONFLICT_SIMILARITY`) are treated as conflicting instructions and
   resolved in favour of the Documents version, and everything else from
   the original is preserved.
3. **Local** — `<workspace>/JEB.md`, in the project root. This applies to
   the current project only.

Each file is read, trimmed, and truncated at 32,000 characters
(`_MAX_JEB_MD_CHARS`). The global content (merged or single-file) is
wrapped in a labelled header (`# Global JEB.md (~/Documents/miniagent
merged with ~/miniagent)` when both exist) and the local content in
`# Project JEB.md (workspace)`; the sections are joined with a `---`
divider, global first. The result is passed as `extra_context` to
`build_system_prompt()`, which appends it to the system prompt. Missing
files are silently skipped.

The `:context` console command (`_print_jeb_context`) prints which files
were found, any conflicts that were resolved in favour of the Documents
version, and the combined text, so the user can verify what the model
received. See [`jeb_md.md`](jeb_md.md) for the user-facing guide.

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

## Sessions (`sessions.py`)

Conversations are recorded so a later console can resume them.

**Storage.** One session is one JSON-lines file under
`<state_dir>/sessions/<workspace-key>/` — normally
`~/Documents/miniagent/sessions/`. The `<workspace-key>` is the workspace
path relative to `~/Documents` with path separators replaced by dashes
(`workspace_key()`: `~/Documents/projects/foo` → `projects-foo`;
workspaces outside Documents fall back to their full path). The file stem is
a timestamp-shaped session id such as `2025-06-07_21-14-03`. The first line
is a `meta` object (`version`, `started`, `workspace`, `provider`, `model`);
every conversation message is then appended as one
`{"type": "message", "message": {...}}` line, so files are only ever
appended to — never rewritten — and a crash loses at most the line being
written. Session files live outside any workspace, so the agent's own file
tools cannot read or modify them.

**Recording.** `Agent` accepts an optional `recorder` (a `SessionLogger`).
Every message it appends to `self.messages` goes through `Agent._append()`,
which records it. The system prompt is never recorded — it is rebuilt fresh
on every start. The logger creates its file lazily on the first recorded
message, so a session where nothing was said leaves nothing behind.
`Agent.reset()` calls `recorder.rotate()`, so a post-`:reset` conversation
segment gets its own file instead of corrupting the previous segment's
history. `record()` never raises: the first write failure prints one
warning (`Session logging disabled: ...`) and disables logging for the
process.

**Resuming.** The `:resume` console command (`_resume_command` in
`app.py`) lists sessions for the workspace four per page (most recent
activity first, with a one-line preview of each session's first user
prompt); 1-4 pick, 0/5 turn pages, Enter cancels. On selection,
`SessionLogger.load()` reads the file and passes it through
`repair_messages()`, which drops system messages and any assistant
`tool_calls` message whose results are incomplete (a session cut off
mid-turn) along with orphan tool results — so what remains can be sent
straight to the provider. `SessionLogger.attach()` then points the logger
at that file, `Agent.restore()` reinstates the history behind the *current*
system prompt, and the conversation continues with new messages appended
to the same session file.

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

## Vision (`vision.py`)

The collaborator behind the `ask_image` tool. `Vision(config, load_key)` is
constructed by `app.run()` — `load_key` is backed by the keychain scheme in
`app.py` (`_load_api_key(config, provider)`), so the module itself contains
no keychain logic and imports nothing from the rest of the package (no
import cycles).

- **Target resolution.** config.json's top-level `vision_model` key
  (`<provider>/<model-name>`; a bare model name means the selected
  provider). The named provider's own `base_url`/`chat_path`, auth
  settings, `extra_body` and `timeout` build the request, and
  `load_key(provider)` supplies that provider's API key — so the vision
  model can live on a different provider than the chat model.
- **Image sources.** File paths (EXIF-rotated, shrunk and re-encoded as
  JPEG when the base64 data URL exceeds ~2 MiB, long side capped at
  1280), http(s) URLs (passed to the API as-is), photo-library images
  (`photos.get_image`, negative index = from the end), and the clipboard
  image. Nothing is written and nothing prompts.
- **Request format.** A `text` block followed by one `image_url` block
  per image — `data:` URLs or plain http(s) URLs — inside a standard
  chat-completions body.
- **Results.** The `ask()` dict carries `answer`, `provider`, `model`,
  per-image labels and token `usage`; failures raise `VisionError`, which
  `Tools.dispatch` turns into a clean error result for the model.

Workspace confinement for file paths happens in `tools.py` (`_ask_image`
resolves them through `Workspace.resolve` first); photo and clipboard
sources are device-level, which is why the tool is permission-gated.

## Configuration (`config.py`)

A single JSON file (`config.json`) in the state directory holds settings for
one or more providers:

```json
{
  "provider": "mistral",
  "model": "zai-glm-5-3",
  "vision_model": "mistral/pixtral-12b-2409",
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

The optional top-level `vision_model` key (`<provider>/<model-name>`, or a
bare model name for the selected provider) selects the provider/model pair
the `ask_image` tool sends images to — see [Vision](#vision-visionpy).
Unknown top-level keys are preserved on load/save.

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
/ `miniagent_config.py` from the project root or prompts interactively for
provider name, base URL, models, chat path, and auth.

`default_state_dir()` picks `~/Documents/miniagent`, then `~/.miniagent`,
then a temp dir — never hard-coding an iOS container UUID.

## Permissions (`permissions.py`)

Five capabilities: `write`, `edit`, `overwrite`, `run_python`, `ask_image`.
Decisions are keyed by the canonical project path so distinct projects have
distinct policies.

`ask_image` is gated although it only reads: it uploads image data —
workspace files, photo-library images, or the clipboard image — to the
vision provider's API, so each call is user-visible before anything leaves
the device.

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

Any choice may be followed by a free-text comment for the agent, e.g.
`y. But also can you check xyz` or `n. Write the file to this path foo/bar`.
`_parse_choice()` splits the answer into the choice letter and the comment
(the letter must stand alone or be followed by a separator — whitespace or
`.`, `,`, `:`, `;`, `-`, `)` — so inputs like `yes` still re-prompt).
`authorize_with_comment()` returns `(allowed, comment)`; the legacy
`authorize()` wraps it and returns just the bool. Comments are relayed to
the model in the tool result (see below).

## Tools (`tools.py`)

`TOOL_SCHEMAS` is the list passed to the model. `Tools.dispatch(call)`:

1. Parses JSON arguments.
2. Looks up the capability in `CAPABILITY_MAP`. Read-only tools (`list_files`,
   `read_file`, `search_files`) are absent from the map and skip the gate.
   So does `clean_up`: it only moves files the agent created with
   `create_file` in the current session (tracked in memory by the `Tools`
   instance), and it moves them into `to_delete/` rather than deleting, so
   no permission prompt is needed. `ask_image` is in the map even though it
   is read-only: it uploads image data (files, photos, clipboard) to the
   vision provider's API, so the user sees and approves each upload; its
   preview shows the question, the image sources, and the target
   provider/model.
3. If a capability is required, builds a human-readable preview via
   `_preview()` and calls `permissions.authorize_with_comment()`, which
   returns both the decision and any comment the user appended to their
   prompt answer. A denial returns `{"ok": false, "denied": true}` (plus a
   `"user_comment"` field when the user gave a redirect or comment) to the
   model without executing. On approval, an approved action that carried a
   comment gets the comment attached to its result the same way, so an
   addendum like `y. But also check xyz` reaches the model as
   `{"ok": true, ..., "user_comment": "But also check xyz"}`.
4. Calls the implementation in `_execute()`, which delegates to `Workspace`,
   `Runner`, or the `Vision` collaborator (injected by `app.py`).
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
- `clean_up` — moves a file into the workspace-level `to_delete/` trash
  folder under a collision-free name (`name-1.ext`, ...); refuses paths
  inside that folder.
- `preview_edit` / `preview_overwrite` — produce unified diffs for the
  permission prompt, without mutating.

Atomic writes use `tempfile.mkstemp` + `os.replace` so a crash never leaves
a half-written file.

## Runner (`runner.py`)

`Runner.run(path, args)` executes a `.py` file in-process. Because
Pythonista has no usable subprocess boundary for this, it is **not
sandboxed**. Three layers of defense:

1. **Static preflight** (`static_scan` / `_preflight`): an AST walk that
   blocks calls to `exit`, `quit`, `sys.exit`, `os._exit`, `os.abort`,
   `os.kill`, `os.fork`, `os.exec*`, and `raise SystemExit` /
   `raise BaseException`. It also blocks `from sys import exit`, `from
   builtins import exit/quit`, and `from os import
   _exit/abort/kill/fork/exec*`. `static_scan` is a pure function
   returning human-readable findings; `_preflight` raises on the first
   finding, and the `run_python` permission preview in `tools.py` shows the
   same findings to the user before approval.

2. **Runtime replacement** (`_install_runtime_blocks`): temporarily patches
   `builtins.exit`, `builtins.quit`, `sys.exit`, `os._exit`, `os.abort`, and
   `os.kill` with callables that raise `RuntimeError`. A `SystemExit`
   exception is caught and reported. Everything is restored in `finally`.

3. **Hang prevention** (interactive-input blocking): run output is captured
   into buffers, so a real prompt would be invisible — and a blocking read
   on the real stdin would hang Pythonista with no in-process way to
   interrupt it. For the duration of a run, `builtins.input` and
   `sys.stdin` are replaced with objects that raise
   `InteractiveInputBlocked` immediately, so a script that trips it fails
   fast with a clear traceback instead of hanging the app. A script may
   still rebind `builtins.input` itself to inject scripted answers; only
   the default interactive path is blocked. See
   [`testing.md`](testing.md) for the testing patterns around this.

Execution captures stdout/stderr into buffers (truncated at 20k chars),
sets `sys.argv`, changes cwd to the workspace root, and purges workspace
modules from `sys.modules` afterward so re-runs pick up file changes. The
console-critical builtins `input` and `print` — and `sys.stdin` — are also
saved and restored around each run, so a script that rebinds them cannot
poison the interactive console loop after the run ends.
