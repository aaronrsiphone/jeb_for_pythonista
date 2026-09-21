# Architecture

This document explains how MiniAgent is put together so that a coding agent
(or a human) can navigate the codebase confidently. All file paths are
relative to the package root (`miniagent/`).

This is prose about *why* the system is shaped the way it is; it does not
generate and can drift if the code moves without it being updated. The
tables of exactly what tools, commands and modules exist right now — which
*do* generate, from the code — live in [`reference.md`](reference.md); this
file does not repeat them.

## Big picture

The engine (`agent.py`, `tools/`) never prints or reads input directly. It
communicates with whatever is driving it through a stream of typed events
(`events.py`). A *renderer* (`ui/`) turns that stream into console output and
answers the one event that needs a reply — a permission question. This is
what makes the same agent loop usable from a compact phone console, a
verbose debugging console, and a silent test harness without three copies of
the loop.

```
 user types a line
        │
        ▼
 console/loop.py ── ":"? ──► console/registry.py ──► a console/commands/*.py handler
        │                                              (mutates ctx in place)
        │ no
        ▼
 ui.drive(agent, text, ctx.renderer)
        │
        │ pumps the generator, one event at a time
        ▼
 agent.py: Agent.turn()  ── generator ──►  events.py
        │  yields ReasoningChunk / AssistantText / ToolStarted /
        │  PermissionNeeded / ToolCompleted / TurnEnded / TurnFailed
        │
        │ each tool call:
        ▼
 tools/dispatch.py: Tools.dispatch()  ── generator, yield-from'd by Agent.turn() ──►
        │  required-arg gate → permissions.py: Permissions.decide()
        │  → (only if undecided) yields PermissionNeeded, driver send()s back
        │    a PermissionAnswer → Permissions.apply_answer()
        │  → tools/*.py implementation → workspace.py / runner.py / vision.py
        │
        ▼
 ctx.renderer.handle(event)  (ui/console.py, ui/verbose.py, or ui/headless.py)
        - informational events: printed (or recorded), return None
        - PermissionNeeded: prompts (or answers from a script), returns a
          PermissionAnswer that ui.drive() send()s back into the generator

 checkpoints.py sits underneath workspace.py: every mutating Workspace method
 snapshots the file's prior bytes (outside the workspace) before writing, so
 :undo can restore a whole turn's changes.

 agent.py also hands every appended message to a SessionLogger (sessions.py),
 which appends it to a per-workspace JSONL log; :resume (console/resume.py)
 loads such a log back into agent.messages.
```

## Construction (`app.py`, `context.py`)

`run(project_root)` is the single public entry point, re-exported from
`__init__.py`. `app.py` is deliberately small (under 100 lines) — it is only
the wiring that turns a project root into a running session; everything it
used to do inline now lives in its own module. `run()`:

1. Resolves and validates `project_root`.
2. Determines the application state directory via `config.default_state_dir()`
   and loads (or first-run-creates) a `Config`.
3. Loads the API key for the selected provider via `keys._ensure_api_key`
   (prompting if missing). Keys are stored per provider in the Pythonista
   keychain; legacy hash-of-base-URL entries are migrated automatically.
4. Constructs a `Checkpoints(root, state_dir)` — the undo safety net (see
   below) — and passes it into `Workspace(root, checkpoints=checkpoints)` so
   every mutating file operation is snapshotted automatically.
5. Constructs the rest of the object graph: `Permissions(state_dir, root)`,
   `Runner(workspace)`, `Vision(config, load_key)`, `Tools(workspace,
   permissions, runner, vision=vision)`, `Provider(config, api_key)`,
   `SessionLogger(root, state_dir, provider, model)`.
6. Calls `jebmd.load_jeb_md_context(root)` to discover any `JEB.md` files and
   passes the result to `agent.build_system_prompt()` as `extra_context`.
7. Builds the `Agent(provider, tools, system_prompt, recorder=session_logger)`
   and picks a renderer (`ui.get_renderer("console")` — the default).
8. Bundles everything into a `Context` (`context.py`) — config, provider,
   agent, permissions, workspace, root, session_logger, renderer,
   renderer_name, checkpoints — and calls `console.console_loop(ctx)`.

Nothing happens at import time — all construction is inside `run()`.

`Context` exists because `Provider` snapshots config at construction: before
it existed, switching provider/model meant `_apply_selection` had to
*return* a new `Provider` that every command handler threaded back through a
local variable in the console loop. A command handler now does
`ctx.provider = new_provider` (or `ctx.renderer = new_renderer` for
`:verbose`) directly.

## The console (`console/`)

`console/loop.py`'s `console_loop(ctx)` reads a line at a time:

- A line starting with `:` is split into a command word and the rest of the
  text, looked up in `console/registry.py` (which also resolves aliases like
  `:q` for `:quit`), and the handler is called as `spec(ctx, arg)`. Handlers
  mutate `ctx` in place; returning the `QUIT` sentinel ends the loop. An
  unrecognised `:word` is now reported directly (`Unknown command: :word
  (:help lists commands)`) rather than being sent to the model as a prompt.
- Anything else is a prompt for the agent. One `Checkpoints.begin_turn()` is
  opened per prompt (so `:undo` undoes everything one prompt caused as a
  unit), and the turn is run with `ui.drive(ctx.agent, line, ctx.renderer)`.
- A `KeyboardInterrupt` (the Pythonista stop button) during a turn prints
  `[interrupted]` and repairs the conversation tail the same way
  `Agent.turn()` does defensively (see below), so the next prompt is not
  rejected by the provider.

Commands are a registry, the same shape as the tool registry described
below: each command is one `@command("name", aliases=(...))`-decorated
function in a file under `console/commands/`, taking `(ctx, arg)`. Adding a
command used to mean editing the console loop's if/elif chain, a hand-written
`_print_help`, and the startup banner's command list by hand — three places
that had already drifted. `:help` (`console/registry.py:render_help`) and the
startup banner's command list (`render_command_list`) are now both generated
by walking the registry, so neither can go stale or omit a command. See
[`reference.md`](reference.md) for the full, generated list of commands.

`console/resume.py` holds the `:resume` pager as a plain function taking
`(agent, session_logger)` rather than `ctx`, so it can be driven directly by
`test_sessions.py` with scripted `input()` answers; the registered `:resume`
command is a thin wrapper over it.

## The event stream (`events.py`)

Before this module existed, `agent.py` and `permissions.py` called `print()`
and `input()` directly, from as deep as six frames below the console loop.
That made a non-TTY front end impossible and forced tests to monkeypatch
`builtins.input`. Now `Agent.turn()` and `Tools.dispatch()` are **generators**
that `yield` typed `Event` subclasses and, for the one event that needs an
answer, receive a reply via `send()`.

Every turn yields exactly one **terminal event** — `TurnEnded` (a final
reply, or the step limit was hit) or `TurnFailed` (the provider call failed)
— so a driver can always tell when a turn is over. The informational events
are `ReasoningChunk`, `AssistantText`, `ToolStarted`, `ToolCompleted` and
`StepLimitReached`; the one interactive event is `PermissionNeeded`, which a
driver answers by `send()`-ing a `PermissionAnswer` back into the generator.
Sending anything else (including `None`, what a renderer sends when it
cannot ask) is treated as a deny-once.

`miniagent.ui.drive(agent, user_text, renderer)` is the one place that knows
how to pump this protocol: it repeatedly does `event = gen.send(to_send)`,
hands `event` to `renderer.handle(event)`, and sends back whatever that
returns. It also closes the generator on `KeyboardInterrupt` so an
interrupted turn does not leave a suspended frame alive. Every front end —
the console loop, `test_agent.py`, a future GUI — drives a turn through this
one function.

## The agent loop (`agent.py`)

`Agent.turn(user_text)` is a generator:

1. Defensively repairs `self.messages` with `sessions.repair_messages()`
   first (see below), then appends the user message.
2. Calls `provider.chat(messages, tools=self.tools.schemas)` in a loop (up
   to `max_steps`, default 50). A `ProviderError` here appends a synthetic
   assistant message (`"[provider error: ...]"`) and yields `TurnFailed` —
   the conversation and session log stay balanced even on failure.
3. Normalises the response's reasoning and content via
   `Provider.split_content()` (some providers return typed thinking/text
   blocks instead of a plain string) and yields `ReasoningChunk` /
   `AssistantText` for whatever is present. The original content shape is
   kept in history for round-tripping.
4. If the model made no tool calls, yields `TurnEnded(text=..., usage=...)`
   and returns — that is the turn's reply.
5. Otherwise, for each tool call it does `result = yield from
   tools.dispatch(call)`: the dispatch generator's own `ToolStarted` /
   `PermissionNeeded` / `ToolCompleted` events pass straight through to
   whatever is driving `Agent.turn()`, and the JSON result becomes a
   `tool`-role history message. The loop then goes back to the provider with
   the tool results.
6. If `max_steps` is exhausted without a final reply, yields
   `StepLimitReached` followed by a `TurnEnded` stop message.

Usage accounting: every response's `usage` dict is summed into
`self.session_usage` (lifetime) and a per-turn `turn_usage` (carried on
`TurnEnded.usage`); non-numeric fields are ignored.

The system prompt is built by `build_system_prompt(project_root,
extra_context="")`, which injects the absolute project root, the
environment constraints (no shell, no subprocess, workspace confinement,
never generate termination calls) and coding-behavior guidance. When
`extra_context` (JEB.md content discovered by `jebmd.py`) is non-empty, it is
appended after a header explaining the global/local precedence. See
[`jeb_md.md`](jeb_md.md).

## Repairing an interrupted turn (`sessions.repair_messages`)

An interrupted turn (the Pythonista stop button raises `KeyboardInterrupt`
mid-tool-loop) can leave `agent.messages` ending in an assistant `tool_calls`
message with no matching `tool` results — most providers reject the next
request with a 400 if that reaches them. `repair_messages()` strips a
dangling `tool_calls` message (and any orphaned `tool` result with no
matching call) from the tail. It is applied twice: once in the
`KeyboardInterrupt` handler in `console/loop.py`, and again defensively at
the top of every `Agent.turn()` call, so a corrupt tail is healed even if a
caller never went through the console loop.

## Tools (`tools/`)

`tools/` is a package, not a module: **one file is one tool**. Each tool
function is decorated with `@tool(capability=..., params={...})` from
`tools/registry.py`, which derives its JSON schema from the function's own
signature and docstring rather than a hand-maintained schema dict:

```python
@tool(capability=perm.EDIT, params={"path": "...", "old_text": "..."})
def edit_file(ctx, path: str, old_text: str, new_text: str,
              replace_all: bool = False, occurrence=None) -> dict:
    """Replace text in an existing file. ..."""
    ...

@edit_file.preview
def _(ctx, path, old_text, new_text, **kw):
    return "EDIT FILE", ctx.workspace.preview_edit(path, old_text, new_text, **kw)
```

- The description is the docstring, dedented and collapsed to one line.
- Required-ness comes straight from which parameters have no default —
  "forgot to mark an argument required" cannot happen, because there is
  nothing separate to forget.
- `overrides` lets a parameter's schema fragment be adjusted (an enum, a
  default) for the rare case the annotation cannot express it.
- `@some_tool.preview` registers the function that renders the permission
  prompt for that tool; a gated tool without one falls back to a generic
  label.

`tools/__init__.py` imports every tool module once, in the order the model
should see them, which is what registers them; `TOOL_SCHEMAS`,
`CAPABILITY_MAP` and `REQUIRED_ARGS` are *derived* from the registry
(`registry.build_schemas()` / `build_capability_map()` / `build_required_args()`)
rather than hand-maintained lists. Adding a tool is one new file plus one new
import line — see [`self_editing.md`](self_editing.md). The current tool
list, with each one's capability and description read straight from the
registry, is in [`reference.md`](reference.md).

`tools/dispatch.py`'s `Tools.dispatch(tool_call)` is the generator every tool
call goes through:

1. Parses the JSON arguments (a parse failure short-circuits to an error
   result).
2. Yields `ToolStarted(name, args)`.
3. **Required-argument gate**: any argument the registry marks required but
   the call omits (or sends as `null`) is rejected with a clear tool error
   before anything else runs — a call missing `path` gets
   `"read_file requires the argument(s) 'path'"` instead of a bare
   `KeyError` reaching the model as "Unexpected error in ...".
4. **Permission gate**: if the tool has a capability, asks
   `Permissions.decide(capability)`. `None` means "ask" — the preview is
   built only now (not before, so it is never wasted work when policy
   already decides) and a `PermissionNeeded(capability, title, details)` is
   yielded; whatever the driver `send()`s back goes to
   `Permissions.apply_answer()`, which returns `(allowed, comment)`. A denial
   yields `ToolCompleted(..., "denied", ...)` and returns without executing.
5. Calls the tool's implementation (`spec.func(self, **kwargs)`), catching
   `WorkspaceError`/`RunnerError`/`KnowledgeError`/`VisionError` and any other
   exception into a `{"ok": false, "error": ...}` result — nothing raises
   into the model.
6. Yields `ToolCompleted(name, status, comment, payload)` (`status` is one of
   `ok`/`denied`/`blocked`/`error`) and returns the JSON string (truncated at
   40,000 characters), with any permission-prompt comment attached under
   `"user_comment"`.

Read-only tools (`list_files`, `read_file`, `search_files`, `knowledge`) have
no capability and skip the gate entirely; `clean_up` is also ungated because
it only moves files the agent itself created this session
(`Tools._session_created`) into the recoverable `to_delete/` folder.
`ask_image` is gated even though it only reads, because it uploads image
data — possibly photos or clipboard images from outside the workspace — to
the vision provider's API.

## Permissions (`permissions.py`)

`Permissions` is **policy only** now — it does no I/O and never prompts.
Five capabilities: `write`, `edit`, `overwrite`, `run_python`, `ask_image`.
Decisions are keyed by the canonical project path, so distinct projects have
distinct policies, and stored at `permissions.json` in the state directory
— outside any project, so the agent cannot edit its own permission policy
through the file tools.

Three methods carry the whole flow, matching the split described above:

- `decide(capability)` — persistent → session → `None` ("must ask"). An
  unknown capability is refused outright (`False`) rather than turned into a
  question, so a typo in a tool's capability map can never become something
  a user might approve.
- `parse_answer(raw)` (static) — turns a typed answer (`"y"`,
  `"n. use another path"`) into a `PermissionAnswer(decision, comment)`.
  This is the *only* place that knows what the six letters
  (`y`/`s`/`a`/`n`/`d`/`x`) mean, so two renderers can never disagree about
  what `"d"` does. Returns `None` for gibberish (`"yes"`), so a front end
  that can re-prompt knows to.
- `apply_answer(capability, answer)` — records a session/persistent decision
  as appropriate and returns `(allowed, comment)`. A missing or malformed
  answer (including `None` — what a renderer sends when it cannot ask) is a
  safe deny-once, matching a blank answer's historical meaning.

## Front ends (`ui/`)

A renderer is one method: `handle(event)`, called once per event in order,
returning `None` for everything except `PermissionNeeded`, for which it
returns a `PermissionAnswer`. No base class, no registration.

- **`ui/console.py` (`Console`)** — the default, tuned for a roughly
  40-column phone screen. Collapses each tool call to one line
  (`→ tool_name  status`) printed on `ToolCompleted` rather than two lines
  each for request and result; collapses reasoning over 200 characters to a
  character count (`· thinking (412 chars)`); shows the full permission
  legend (the six choices plus the comment syntax) once per renderer
  instance, then a compact one-line prompt with `?` to reprint the legend.
- **`ui/verbose.py` (`Verbose`)** — subclasses `Console` and overrides only
  the per-event methods to restore the old, fully expanded transcript:
  `Reasoning:` / `Assistant:` headers, `Tool request:` (with arguments) /
  `Tool result:` pairs, and the full permission prompt shown every time
  (never just once). Inherits the permission *parsing* unchanged, so the two
  renderers can never drift on what an answer means. Switched to with
  `:verbose on` / `:verbose off` (`console/commands/config_cmds.py`).
- **`ui/headless.py` (`Headless`)** — prints nothing; answers
  `PermissionNeeded` from a scripted queue of typed answers (or pre-built
  `PermissionAnswer`s), consumed in order, and records every event it sees
  (`.events`) for a test to assert against. An empty queue — or an
  unrecognised scripted answer — is a deny-once, the same safe default a
  blank console answer has always been. This is what makes `test_agent.py`,
  `test_tools.py` and friends possible without touching `builtins.input` —
  see [`testing.md`](testing.md).

`ui.get_renderer(name)` looks a renderer up by name (`"console"` is
`DEFAULT_RENDERER`); `ui.drive()` is described under "The event stream"
above.

## Checkpoints (`checkpoints.py`) — the self-editing safety net

An agent editing `agent.py` *from inside* `agent.py` has no rollback without
this. `Checkpoints` snapshots a file's bytes the first time it is touched
within an open "turn" group and can restore the whole group later.
Snapshots live **outside the project workspace**, under
`<state_dir>/checkpoints/<workspace-key>/<turn-id>/` — the same reasoning
that already keeps `permissions.json` and session logs out of reach: the
agent's own file tools cannot read or tamper with its own undo history.

- `Workspace` calls `self.checkpoints.snapshot(path)` (a no-op if no
  `Checkpoints` is attached) immediately before every mutation —
  `create_file`, `edit_file`, `multi_edit`, `overwrite_file`, `clean_up` —
  recording whether the path existed before and, if so, a copy of its prior
  bytes. A second touch of the same path within the same group is a no-op:
  undoing means restoring to *before the turn's first touch*.
- `console/loop.py` opens one turn group per user prompt
  (`checkpoints.begin_turn()`), so `:undo` undoes everything one prompt
  caused, as a unit.
- `:undo` (`console/commands/session_cmds.py`) restores the most recent
  group after confirmation — deleting a path that did not exist before
  (undoing a `create_file`) or rewriting one back to its prior bytes — and
  consumes it, so a second `:undo` goes one turn further back. `:checkpoints`
  lists retained groups (newest first, up to `DEFAULT_RETENTION = 20` per
  workspace) with their turn id, timestamp and touched files.
- Recording never raises: any failure disables further recording (one
  printed warning) rather than breaking the write it was meant to protect.

## Workspace (`workspace.py`)

`Workspace(root, checkpoints=None)` canonicalises the root once.
`resolve(path)` anchors any relative path at the root, resolves symlinks,
and rejects anything that falls outside the root — the single chokepoint for
confinement.

Mutating operations (`create_file`, `edit_file`, `multi_edit`,
`overwrite_file`, `clean_up`) all snapshot through `self._snapshot(full)`
before writing, and all write through `_atomic_write`, which:

- **Compile-gates every `.py` write.** The new content is `compile()`d
  before the atomic rename; a `SyntaxError` refuses the write (nothing on
  disk is touched — not even a temp file) and returns a tool error naming
  the line and problem, so the model can fix it immediately instead of
  discovering a harness that will not start on next launch.
- Writes via `tempfile.mkstemp` + `os.replace`, so a crash mid-write never
  leaves a half-written file.

`edit_file(path, old_text, new_text, replace_all=False, occurrence=None)`:
by default `old_text` must match exactly once (unchanged historical
behaviour). Two escape hatches for a non-unique match: `replace_all=True`
replaces every occurrence, `occurrence=N` (1-based) replaces only the Nth —
mutually exclusive. Failure messages are designed to be acted on without a
second failed attempt:
- **no match** — reports the closest-matching region of the file (by
  character-level similarity over nearby line-window sizes) with a diff
  against `old_text`, since most real failures are a whitespace or
  line-ending mismatch;
- **non-unique match, no `replace_all`/`occurrence` given** — names every
  matching line number so the model can pick one instead of guessing again.

`multi_edit(path, edits)` applies a list of `{old_text, new_text}` edits to
one file: each edit must uniquely match the file's *running* content at the
point it is applied (default `edit_file` semantics, no `replace_all` inside
a `multi_edit`); either every edit succeeds and the file is written once, or
the first failure aborts the whole call with the file byte-for-byte
untouched (validation happens entirely in memory before anything is
written). One permission prompt covers the whole change instead of one per
edit.

Other operations: `list_files` (flat or recursive with `max_depth`),
`read_file` (line-numbered text with an optional range), `search_files`
(literal or regex content search, capped at `max_results`, skipping
non-UTF-8/oversized/confinement-violating files), `clean_up` (moves a file
into `to_delete/` under a collision-free name; refuses paths already inside
it), and `preview_edit` / `preview_multi_edit` / `preview_overwrite` (diffs
for the permission prompt, no mutation, no snapshot).

## Runner (`runner.py`)

`Runner.run(path, args)` executes a `.py` file in-process — **not
sandboxed**, because Pythonista has no usable subprocess boundary. Layers of
defense, unchanged in shape from before this phase:

1. **Static preflight** (`static_scan` / `_preflight`) — an AST walk
   blocking calls to `exit`, `quit`, `sys.exit`, `os._exit`, `os.abort`,
   `os.kill`, `os.fork`, `os.exec*`, `raise SystemExit`/`raise
   BaseException`, and the matching `from ... import ...` forms.
2. **Runtime replacement** (`_install_runtime_blocks`) — temporarily patches
   the same builtins with callables that raise `RuntimeError`; restored in
   `finally`.
3. **Hang prevention** — `builtins.input` and `sys.stdin` are replaced for
   the duration of the run with objects that raise `InteractiveInputBlocked`
   immediately, because run output is captured into buffers (a real prompt
   would be invisible) and a blocking read on the real stdin has no
   in-process way to be interrupted. A script may still rebind
   `builtins.input` itself to inject scripted answers — see
   [`testing.md`](testing.md).
4. **Self-import protection (§1.5 of `rearchitecture.md`).**
   `_purge_stale_imports` removes modules imported during the run from
   `sys.modules` (so re-running a changed file picks up the edit), *except*
   it never evicts `miniagent` or any `miniagent.*` module — even when the
   workspace root is the package's own install location (the self-editing
   case). Without this exception, a validation script that imports
   `miniagent.tools` would hot-reload the *running* harness's own modules
   from half-edited source mid-session.

Execution captures stdout/stderr (truncated at 20k chars), sets `sys.argv`,
changes cwd to the workspace root, and restores the console-critical
builtins `input`/`print` and `sys.stdin` afterward, so a script that rebinds
them cannot poison the interactive console loop once the run ends.

## Sessions (`sessions.py`)

Unchanged in shape from earlier phases. One session is one append-only
JSON-lines file under `<state_dir>/sessions/<workspace-key>/`; a `meta` first
line, then one `{"type": "message", ...}` line per appended message.
`Agent._append()` records every message through an attached `SessionLogger`
(the system prompt itself is never recorded); `Agent.reset()` calls
`recorder.rotate()` so a post-`:reset` segment gets its own file.
`repair_messages()` (also used defensively in `Agent.turn()`, see above) is
what makes a session file safe to resume even if it was cut off mid-turn.

`:resume` (`console/resume.py`) lists sessions four per page, most recent
first; `SessionLogger.load()` + `repair_messages()` prepares the history,
`SessionLogger.attach()` points the logger at that file, and
`Agent.restore()` reinstates the history behind the *current* system prompt
— so a resumed session gets the current JEB.md instructions, not stale ones.

## The provider (`provider.py`)

A thin `requests`-based client for any OpenAI-compatible `/chat/completions`
endpoint: builds the URL from `base_url` + `chat_path`, adds auth headers
only when enabled and a key is present, sends `model`/`messages`/`tools`/
optional `tool_choice`/`reasoning_effort`, and merges `extra_body`.
`extract_message()` pulls `choices[0].message`; `split_content()` normalises
either a plain string or a list of typed content blocks (some providers
return `{"type": "thinking", ...}` blocks instead of a `reasoning_content`
field) into `(text, thinking)`. `set_effort(level)` controls
`reasoning_effort`; it is not persisted.

## Vision (`vision.py`)

The collaborator behind the `ask_image` tool, constructed by `run()` with a
`load_key` callback backed by `keys._load_api_key` — the module itself has
no keychain logic and imports nothing else from the package. Resolves its
target provider/model from config.json's top-level `vision_model` key,
accepts workspace file paths (EXIF-rotated, shrunk/re-encoded above ~2 MiB),
http(s) URLs, photo-library images and the clipboard image, and returns an
`answer` plus per-image labels and token `usage`. Workspace confinement for
file-path sources happens in `tools/ask_image.py` before `Vision` ever sees
the path.

## Configuration (`config.py`)

A single JSON file (`config.json`) in the state directory holds one or more
named providers plus the current `provider`/`model` selection and an
optional top-level `vision_model`. `Config.__getattr__` exposes the selected
provider's settings as plain attributes (`config.base_url`, ...), so
`Provider` is built from a `Config` without knowing about the multi-provider
layout. `select()` changes the selection in memory (`:model`); `set()`
validates, mutates and persists (`:config set`). `default_state_dir()` picks
`~/Documents/miniagent`, then `~/.miniagent`, then a temp dir. No credentials
are stored here — see [Keys](#keys-keyspy).

## JEB.md discovery (`jebmd.py`)

Split out of `app.py` into its own module — the largest self-contained piece
of logic `app.py` used to carry, and splitting it out is what made it
testable in isolation (`test_jebmd.py`). `load_jeb_md_context(project_root)`
discovers up to three files (global Documents version, global legacy
version, project-local) and returns the combined text passed to
`build_system_prompt()` as `extra_context`. When both global files exist, a
small markdown merge engine (`_merge_global_jeb_md_texts`) combines them
into one section, resolving conflicts (near-duplicate instructions) in
favour of the Documents version. The `:context` console command
(`console/commands/workspace_cmds.py`) shows what was found and any
conflicts resolved. Full details, including the merge algorithm and worked
examples, are in [`jeb_md.md`](jeb_md.md).

## Keys (`keys.py`)

Keychain access and API-key storage/migration, with no dependency on the
console loop or the agent — split out of `app.py` for the same reason as
`jebmd.py`. Keys are stored per provider (`MiniAgent:provider:<name>`);
`_load_api_key(config, provider)` can load any configured provider's key,
not just the selected one, which is how `Vision` loads its own provider's
key independently of the main chat provider. A legacy hash-of-base-URL entry
is migrated to the per-provider scheme automatically the first time that
provider is used.
