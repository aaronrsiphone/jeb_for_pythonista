# Self-Editing Guide for a Coding Agent

This document is written for **you** — a coding agent that has been booted
inside MiniAgent and asked to modify or extend the MiniAgent harness itself.
It tells you how the codebase is organized, how to navigate it, and how to
make safe, focused changes to your own runtime.

All file paths mentioned here are relative to the package root
(`miniagent/`).

---

## 0. Before you start

You are running inside MiniAgent right now. That means:

- You can read any file in this workspace with `read_file`.
- You can list files with `list_files`.
- Writes, edits, overwrites, and `run_python` require permission. The user
  will be prompted. If they deny, adapt — do not retry blindly.
- Every mutating tool call is automatically checkpointed before it touches
  disk (see §2a, below): if an edit goes wrong, `:undo` can put the affected
  files back. This does not excuse carelessness — read before you edit — but
  it means a bad edit is recoverable, not a rebuild-from-scratch.
- A write to a `.py` file that would not even `compile()` is refused before
  it reaches disk — see §2b.
- `run_python` executes **in-process in the same Pythonista instance that is
  running you**. Treat it as powerful and fragile. Never run code that calls
  `exit()`, `sys.exit()`, `os._exit()`, `os.kill`, `os.fork`, or
  `os.exec*()` — the runner will block it, but you should not generate it
  anyway.
- There is no shell and no subprocess. Do not try to shell out.

Read the architecture overview first:
[`docs/architecture.md`](architecture.md). This guide assumes you understand
it. The generated tool/command/module tables in
[`docs/reference.md`](reference.md) are the fastest way to see exactly what
exists right now, without reading every file.

## 1. How to read this codebase

Start with the entry point and follow the call graph:

| Step | Read | What you learn |
|------|------|----------------|
| 1 | `__init__.py` | `run` is the public API; it comes from `app.py`. |
| 2 | `app.py` | How the object graph and `Context` are built (construction only — ~100 lines). |
| 3 | `console/loop.py` | The input loop: `:commands` via the registry, everything else via `ui.drive()`. |
| 4 | `agent.py` | The model/tool loop as a generator of `events.py` events, and the system prompt. |
| 5 | `events.py` | The event types that connect the engine to a front end. |
| 6 | `tools/dispatch.py` | The dispatch generator and the permission gate. |
| 7 | `tools/registry.py` | How a `@tool`-decorated function becomes a schema entry. |
| 8 | `workspace.py` | How file paths are confined, files are written, and checkpoints are taken. |
| 9 | `checkpoints.py` | The undo safety net — read this before you rely on it. |
| 10 | `runner.py` | How `run_python` works and what it blocks. |
| 11 | `permissions.py` | Policy only: `decide` / `parse_answer` / `apply_answer`. No I/O. |
| 12 | `ui/` | The renderers (`console`, `verbose`, `headless`) that consume the event stream. |
| 13 | `provider.py` | How HTTP requests are built and sent. |
| 14 | `config.py` | How settings are loaded, saved, and migrated. |
| 15 | `sessions.py` | How conversations are recorded to JSONL logs and resumed (`:resume`). |

Use `list_files` with `recursive=true` to see the full tree, and `read_file`
with `start_line` / `end_line` for large files.

## 2. The golden rules of editing this harness

You are editing the tool that is editing you. That is powerful but
dangerous. Follow these rules:

### Rule 1 — Inspect before you change

Always `read_file` the file you intend to edit, including the region around
your target. Never edit a file you have not read in this session.

### Rule 2 — Prefer `edit_file` / `multi_edit` over `overwrite_file`

`edit_file` replaces text and fails loudly if the match is ambiguous or
missing — but it is no longer all-or-nothing. If the match is not found, the
error shows the closest-matching region of the file with a diff of what
differs, so you can usually fix `old_text` in one retry instead of guessing.
If the match is not unique, the error names every occurrence's line number;
pass `occurrence=<1-based index>` to pick one, or `replace_all=true` if you
really mean every occurrence.

For several related edits to the *same* file, prefer `multi_edit`: pass a
list of `{old_text, new_text}` edits and they are applied atomically — either
every edit succeeds and the file is written once, or the first failure
aborts the whole call with the file byte-for-byte untouched, and the error
names which numbered edit failed and why. This is both safer (no
half-applied file) and cheaper on a metered connection (one permission
prompt instead of one per edit).

Reserve `overwrite_file` for cases where an entire file genuinely needs to
be rewritten.

### Rule 3 — Keep changes focused

One logical change at a time. If a task touches three modules, make three
sets of edits, not one giant overwrite. This makes failures recoverable and
diffs reviewable.

### Rule 4 — Do not edit the system prompt carelessly

The system prompt lives in `agent.py` (`build_system_prompt`). It tells the
model — possibly your own future self — the environment constraints. If you
weaken it, you degrade every future agent run. Only modify it deliberately
and with a clear reason.

### Rule 5 — Do not remove safety machinery

None of the following are optional cleanup targets:

- `runner.py`'s static preflight (`_BLOCKED_CALLS`, `_BLOCKED_IMPORTS`),
  runtime replacement (`_install_runtime_blocks`), interactive-input
  blocking, and the `miniagent`/`miniagent.*` exclusion in
  `_purge_stale_imports` (removing that exclusion would let a validation
  script hot-reload the very harness running you, mid-session).
- `workspace.py`'s `resolve()` confinement check and the `.py` compile gate
  in `_atomic_write`.
- `permissions.py` storing its policy outside the workspace on purpose.
- `checkpoints.py` snapshotting outside the workspace on purpose, so you
  cannot tamper with your own undo history.

These exist to keep Pythonista alive and the user's machine under their
control. Do not remove or weaken any of them unless that is the explicit
task.

### Rule 6 — Do not create termination behavior

Never add code that calls `exit()`, `quit()`, `SystemExit`, `sys.exit()`,
`os._exit()`, `os.abort()`, `os.kill()`, `os.fork()`, or `os.exec*()`. The
runner will block it and the user will see an error. If a feature needs to
"stop", return from a function or raise a domain-specific exception that the
console loop catches.

### Rule 7 — Run the test suite after a change, and add to it

If you have permission, run `run_python miniagent/tests/run_all.py` after a
change that could plausibly break something — not just the file you touched.
When your change adds behavior, add a test for it in `miniagent/tests/`
rather than a scratch script you delete afterwards, so the fix keeps its
regression test. See [`testing.md`](testing.md) for how to test
permission-gated and prompt-driven code without touching `builtins.input`.

Do not run code that imports `miniagent` and starts the console loop — that
would nest an agent inside the agent. Validation scripts must never read
interactive input: under `run_python`, `input()` and `sys.stdin` fail fast
with `InteractiveInputBlocked`. When testing prompt-driven code, use the
`Headless` renderer instead — see [`testing.md`](testing.md).

### Rule 8 — Recover from tool errors

If `edit_file` or `multi_edit` fails, read what the error actually says — it
names the closest match, the conflicting line numbers, or which numbered
edit failed — before retrying. Do not assume the file is what you think it
is; `read_file` it again if the error is surprising.

### Rule 9 — Use `:undo` deliberately, not as a substitute for care

Every mutating tool call is checkpointed automatically (§2a), and `:undo`
can restore the files one prompt's turn touched. This is a safety net for
mistakes, not a reason to be careless: `:undo` restores files on disk, it
does not un-send a request that already went to the model, and it only goes
back through the retained checkpoint history (`DEFAULT_RETENTION = 20`
turns per workspace).

## 2a. The checkpoint safety net (`checkpoints.py`)

Before this existed, an agent that botched an edit to the file it was
editing had no rollback: `to_delete/` only covers scratch files the agent
itself moved there, not a bad `edit_file` on a file still in use. Now:

- Every mutating `Workspace` method (`create_file`, `edit_file`,
  `multi_edit`, `overwrite_file`, `clean_up`) snapshots the target's prior
  bytes *before* writing, into `<state_dir>/checkpoints/<workspace>/<turn>/`
  — outside the workspace, so you cannot read or tamper with your own undo
  history through the file tools.
- One checkpoint group is opened per user prompt in the console loop, so
  `:undo` undoes everything one prompt's turn caused, as a unit.
- The user runs `:undo` (with a confirmation prompt) to restore the most
  recent group; `:checkpoints` lists retained groups. You cannot invoke
  `:undo` yourself — it is a user-facing command — but you should mention it
  when a change did not go as planned, so the user knows the option exists.

## 2b. The compile gate (`workspace.py:_atomic_write`)

Any write to a path ending in `.py` is `compile()`d before the atomic
rename that would make it live. A syntax error refuses the write entirely
(nothing touches disk, not even a temp file) and comes back as a tool error
naming the line and the problem — act on it immediately rather than
re-reading the whole file to hunt for the mistake.

## 3. Common modification tasks

### Add a new tool

Adding a tool is **one new file, plus one new import line** — not the
four-region edit this used to require. `TOOL_SCHEMAS`, `CAPABILITY_MAP` and
the required-argument gate are all *derived* from the registry, so there is
nothing else to keep in sync:

1. Create `tools/your_tool.py`. Write the implementation as a plain function
   whose first parameter is the tool context (conventionally named `ctx`)
   and whose remaining parameters — with type annotations and defaults —
   are exactly the model-facing arguments:

   ```python
   """``your_tool``: one-line description of what it does."""

   from __future__ import annotations

   from . import registry as _perm  # only if gated; see permissions.py
   from .registry import tool


   @tool(params={"path": "Workspace-relative path."})
   def your_tool(ctx, path: str) -> dict:
       """What the model is told this tool does. Becomes the schema description."""
       return ctx.workspace.your_operation(path)
   ```

   Pass `capability=perm.WRITE` (or `EDIT`/`OVERWRITE`/`RUN_PYTHON`/
   `ASK_IMAGE`, or a new constant you add to `CAPABILITIES` in
   `permissions.py`) to `@tool(...)` if the tool mutates state, executes
   code, or leaves the workspace (uploads, network). Leave it out for a
   read-only tool.

2. If it is gated, register a preview so the user sees what they are about
   to approve:

   ```python
   @your_tool.preview
   def _(ctx, path, **kw):
       return "YOUR TOOL", f"your_tool: {path}"
   ```

3. Add one import line to `tools/__init__.py`, in the position you want the
   model to see it in the tool list:

   ```python
   from .your_tool import your_tool  # noqa: F401
   ```

4. Delegate the real work to `Workspace`/`Runner`/`Vision` or a new
   collaborator — do not put file I/O directly in the tool function.
5. Run `run_python miniagent/tests/run_all.py`; `test_registry.py` will
   catch a schema that does not reach an implementation, and
   `test_reference.py` will tell you to regenerate `docs/reference.md`
   (`run_python miniagent/gendocs.py`) once your tool is registered.
6. Add a test in `miniagent/tests/` if the tool has any real logic.

### Add a new console command

Same shape, smaller: one decorated function in a file under
`console/commands/` (an existing one if it fits an existing theme, or a new
file — `console/commands/__init__.py` just needs one more import line for a
new file):

```python
@command("mycommand", aliases=("mc",))
def mycommand_command(ctx, arg):
    """One-line summary. This becomes the :help entry and reference.md's
    summary column, so write it the way it should be read there.
    """
    ...
```

`:help` and the startup banner's command list are both generated from the
registry (`console/registry.py:render_help` / `render_command_list`), so
there is nothing else to update. Regenerate `docs/reference.md` afterwards.

### Change the system prompt

Edit `build_system_prompt` in `agent.py`. Keep the environment constraints
intact unless the task explicitly requires changing them. The function also
accepts an `extra_context` string — JEB.md content is appended there. If you
change how it is formatted, keep `_EXTRA_CONTEXT_HEADER` accurate.

### Change JEB.md discovery

Lives entirely in `jebmd.py` now (`load_jeb_md_context`, `_read_jeb_md`,
`_global_jeb_md_paths`, `_merge_global_jeb_md_texts`) — not `app.py`. The
`:context` command (`console/commands/workspace_cmds.py`) shows what was
loaded. See [`jeb_md.md`](jeb_md.md) for the user-facing description and the
merge algorithm.

### Change how a tool result is formatted

Tool results are JSON strings built by `_result()` in `tools/dispatch.py`.
The truncation limit is `_RESULT_LIMIT`. `Tools.dispatch` derives `status`
(`ok`/`denied`/`blocked`/`error`) from the outcome dict via `_status_of()`
and attaches a permission-prompt comment under `"user_comment"` via
`_with_comment()`. If you change the result shape, check both of those.

### Change the permission prompt or its choices

The letter semantics (`y`/`s`/`a`/`n`/`d`/`x`) and comment parsing live in
`permissions.py` (`_CHOICE_MAP`, `_parse_choice`, `Permissions.parse_answer`,
`Permissions._apply`) — change all of them together if you add or remove a
choice. The actual *prompting* — what gets printed, when the legend is shown
— lives in the renderer instead (`ui/console.py:on_permission` /
`_show_prompt`), not in `permissions.py`, since `Permissions` does no I/O.

### Change the console loop or a renderer's output

The loop itself is `console/loop.py`. Console *output* for a given event —
what a tool call, a reasoning chunk, or a permission prompt looks like —
lives in `ui/console.py` (compact) or `ui/verbose.py` (expanded); both
implement the same `handle(event)` seam described in
[`architecture.md`](architecture.md). Add a new renderer by writing a class
with a `handle(event)` method and registering it in `ui/__init__.py`'s
`RENDERERS` dict.

### Change the HTTP client behavior

`provider.py` builds the request in `_headers()` and `_body()`. The
`extra_headers` and `extra_body` config keys let you merge provider-specific
flags without changing code. If you need a new top-level behavior (e.g.
streaming, retries), add it here and keep it behind a config flag if
possible.

### Change where state is stored

`config.default_state_dir()` in `config.py` controls the state directory.
`Permissions`, `Config` and `Checkpoints` all receive `state_dir` as a
constructor argument from `app.py`. API keys are stored per provider — see
`keys.py` (`_provider_service`, `_keychain_service` for the legacy
hash-of-base-URL scheme kept only for migration). If you move state, keep it
outside any user project so the agent cannot edit its own permissions or
undo history.

## 4. What lives where — quick reference

This table names *files*, for when you already know the area you are
changing. For the generated, always-current list of every tool, command and
module (with each module's own one-line purpose), use
[`docs/reference.md`](reference.md) instead — it cannot go stale because it
is produced from the code.

| You want to change... | Edit this file |
|-----------------------|----------------|
| The public entry point / object-graph wiring | `app.py` |
| The live per-session wiring commands see (`ctx.provider`, `ctx.renderer`, ...) | `context.py` |
| The console input loop | `console/loop.py` |
| The command registry / `:help` / banner generation | `console/registry.py` |
| An individual console command | `console/commands/*.py` |
| The `:resume` pager | `console/resume.py` |
| The conversation loop (event stream) | `agent.py` |
| The system prompt | `agent.py` (`build_system_prompt`) |
| The event types themselves | `events.py` |
| JEB.md discovery and the global/local merge engine | `jebmd.py` |
| The tool registry / schema generation | `tools/registry.py` |
| The dispatch generator and the permission gate | `tools/dispatch.py` |
| An individual tool's behavior | `tools/<tool_name>.py` |
| Image questions (the `ask_image` tool) | `vision.py` (the collaborator), `tools/ask_image.py` (the tool + workspace confinement) |
| File confinement, atomic writes, the compile gate, `edit_file`/`multi_edit` semantics | `workspace.py` |
| The self-editing undo safety net | `checkpoints.py` |
| Python execution and termination blocking | `runner.py` |
| Permission policy (`decide`/`parse_answer`/`apply_answer`) | `permissions.py` |
| What capabilities exist | `permissions.py` (`CAPABILITIES`) |
| What a permission prompt looks like on screen | `ui/console.py` / `ui/verbose.py` |
| Renderer selection / the turn-driving loop | `ui/__init__.py` |
| A new front end (renderer) | a new file in `ui/`, registered in `ui/__init__.py`'s `RENDERERS` |
| HTTP request shape | `provider.py` |
| Provider settings (defaults, save/load) | `config.py` |
| First-run setup and legacy migration | `config.py` |
| API key storage / keychain migration | `keys.py` |
| Where state/config/permissions/checkpoints are stored | `config.py` (`default_state_dir`) |
| Session recording and `:resume` | `sessions.py` (`SessionLogger`, `repair_messages`), wired through `agent.py` (`recorder`, `restore`) and `console/resume.py` |
| The test suite (`miniagent/tests/`) | the `test_*.py` files plus `run_all.py`; conventions in [`testing.md`](testing.md) |
| The generated reference doc | `gendocs.py` (the generator), `docs/reference.md` (its output — never hand-edit) |
| The example launcher | `_jeb.py` |

## 5. A safe self-test you can run

If you want to confirm the package imports cleanly after your changes, you
can `run_python` a tiny script like:

```python
# check_imports.py
import miniagent
from miniagent import agent, app, config, permissions, provider, runner, workspace
from miniagent import tools, ui, console, events, checkpoints, jebmd, keys, context
print("all modules import ok")
print("version:", miniagent.__version__)
```

This does not start the agent loop and does not touch the network. It only
exercises the import graph.

For real validation, use the permanent test suite in `miniagent/tests/`: run
one file with `run_python miniagent/tests/test_<name>.py`, or everything
with `run_python miniagent/tests/run_all.py`. When your change needs a new
test, add it there (or extend an existing file) rather than creating a
scratch test to delete afterwards — see the conventions in
[`testing.md`](testing.md). For anything that touches the permission layer,
the agent loop, or other event/prompt-driven code, read [`testing.md`](testing.md)
first: it explains how to drive a turn with the `Headless` renderer so a
test asserts on typed events instead of scraping printed text, and
documents the `InteractiveInputBlocked` fail-fast behavior the runner
guarantees under `run_python`.

If you changed a tool, a command, or any module's docstring, regenerate
`docs/reference.md` too:

```
run_python miniagent/gendocs.py
```

`test_reference.py` (part of `run_all.py`) fails the suite if you forget —
regenerating is the fix it tells you to run.

## 6. What not to do

- **Do not** edit `permissions.json`, `config.json`, or anything under
  `checkpoints/` or `sessions/` through the file tools. They live outside
  the workspace by design. If you can reach them, something is
  misconfigured — tell the user.
- **Do not** add `import subprocess` or `os` termination calls anywhere.
- **Do not** remove the workspace confinement check in
  `workspace.py:resolve()`, the compile gate in `_atomic_write`, or the
  `miniagent`/`miniagent.*` exclusion in `runner.py`'s
  `_purge_stale_imports`.
- **Do not** start a nested `run()` call inside the running agent. You will
  hijack the console loop.
- **Do not** write tests that call `input()` or read `sys.stdin` — under
  `run_python` they fail fast with `InteractiveInputBlocked` by design.
  Drive prompt-driven code through `Headless` instead
  ([`testing.md`](testing.md)).
- **Do not** hand-edit `docs/reference.md`. It is generated; edit the source
  it reads from (a tool, a command, a module docstring) and run
  `run_python miniagent/gendocs.py`.

## 7. After you finish

When you are done with your changes:

1. Summarize what you changed and why, referencing the relative file paths.
2. Note anything the user should verify manually (e.g. re-running MiniAgent,
   checking a config value with `:config`), and mention `:undo` if a change
   did not land the way you intended.
3. If you created scratch files, mention them so the user can decide whether
   to keep them (or move them to `to_delete/` with `clean_up` yourself).
4. Stop. Reply with a normal text message — no dangling tool calls.
