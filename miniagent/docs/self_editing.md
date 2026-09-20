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
- `run_python` executes **in-process in the same Pythonista instance that is
  running you**. Treat it as powerful and fragile. Never run code that calls
  `exit()`, `sys.exit()`, `os._exit()`, `os.kill`, `os.fork`, or
  `os.exec*()` — the runner will block it, but you should not generate it
  anyway.
- There is no shell and no subprocess. Do not try to shell out.

Read the architecture overview first:
[`docs/architecture.md`](docs/architecture.md). This guide assumes you
understand it.

## 1. How to read this codebase

Start with the entry point and follow the call graph:

| Step | Read | What you learn |
|------|------|----------------|
| 1 | `__init__.py` | `run` is the public API; it comes from `app.py`. |
| 2 | `app.py` | How everything is constructed and wired; the console loop. |
| 3 | `agent.py` | The model/tool loop and the system prompt. |
| 4 | `tools.py` | Tool schemas, the dispatch path, and the permission gate. |
| 5 | `workspace.py` | How file paths are confined and files are written. |
| 6 | `runner.py` | How `run_python` works and what it blocks. |
| 7 | `permissions.py` | How authorization decisions are made and stored. |
| 8 | `provider.py` | How HTTP requests are built and sent. |
| 9 | `config.py` | How settings are loaded, saved, and migrated. |

Use `list_files` with `recursive=true` to see the full tree, and
`read_file` with `start_line` / `end_line` for large files.

## 2. The golden rules of editing this harness

You are editing the tool that is editing you. That is powerful but
dangerous. Follow these rules:

### Rule 1 — Inspect before you change

Always `read_file` the file you intend to edit, including the region around
your target. Never edit a file you have not read in this session.

### Rule 2 — Prefer `edit_file` over `overwrite_file`

`edit_file` replaces exactly one unique text occurrence. It is surgical and
fails loudly if the match is ambiguous or missing. Use it for almost
everything. Reserve `overwrite_file` for cases where an entire file genuinely
needs to be rewritten.

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

`runner.py` has a static preflight (`_BLOCKED_CALLS`, `_BLOCKED_IMPORTS`)
and runtime replacement (`_install_runtime_blocks`). `workspace.py` has the
`resolve()` confinement check. `permissions.py` stores its policy outside the
workspace on purpose. These exist to keep Pythonista alive and the user's
machine under their control. Do not remove or weaken them unless that is
the explicit task.

### Rule 6 — Do not create termination behavior

Never add code that calls `exit()`, `quit()`, `SystemExit`, `sys.exit()`,
`os._exit()`, `os.abort()`, `os.kill()`, `os.fork()`, or `os.exec*()`. The
runner will block it and the user will see an error. If a feature needs to
"stop", return from a function or raise a domain-specific exception that the
console loop catches.

### Rule 7 — Run changed code only when useful

If you have permission, you can `run_python` a changed file to validate it.
Do this when it is genuinely useful (e.g. checking a syntax error or a
logic path). Do not run code that imports `miniagent` and starts the console
loop — that would nest an agent inside the agent.

### Rule 8 — Recover from tool errors

If `edit_file` fails because `old_text` was not found or was ambiguous,
`read_file` the current contents again, adjust your `old_text` to match
exactly, and retry. Do not assume the file is what you think it is.

## 3. Common modification tasks

### Add a new tool

1. **Add the schema** to the `TOOL_SCHEMAS` list in `tools.py`. Give it a
   `name`, `description`, and `parameters`.
2. **Decide on permissions.** If the tool mutates state or executes code,
   add an entry to `CAPABILITY_MAP` in `tools.py` mapping the tool name to a
   capability. If it is read-only, leave it out.
3. **If you added a new capability**, add it to `CAPABILITIES` in
   `permissions.py`.
4. **Implement the action** in `Tools._execute()` in `tools.py`. Delegate
   the real work to `Workspace` (for file operations) or `Runner` (for
   execution) or a new collaborator.
5. **Add a permission preview** in `Tools._preview()` so the user sees what
   the tool is about to do before approving.
6. **Add tests or validation.** If you have permission, `run_python` a small
   script that imports the module and exercises the new path.

### Change the system prompt

Edit `build_system_prompt` in `agent.py`. The prompt is an f-string that
injects `{project_root}`. Keep the environment constraints intact unless the
task explicitly requires changing them. The function also accepts an
`extra_context` string — this is where JEB.md content is appended. If you
change how `extra_context` is formatted, keep the `_EXTRA_CONTEXT_HEADER`
explanation accurate so the model understands what the section is.

### Change JEB.md discovery

The JEB.md lookup lives in `app.py` (`load_jeb_md_context`,
`_read_jeb_md`, `_global_jeb_md_path`). It checks the global path
(`~/miniagent/JEB.md`) first and the workspace-local path second. The
`:context` command (`_print_jeb_context`) shows the user what was loaded.
See [`jeb_md.md`](jeb_md.md) for the user-facing description.

### Change how a tool result is formatted

Tool results are JSON strings built by `_result()` at the bottom of
`tools.py`. The truncation limit is `_RESULT_LIMIT`. The agent loop in
`agent.py` inspects the JSON for `ok`, `denied`, `blocked`, and `error` keys
to print a status line. If you change the result shape, make sure the status
detection in `agent.py` still makes sense.

### Change the permission prompt

The prompt text and the choice mapping live in `Permissions._ask()` in
`permissions.py`. The six choices (`y/s/a/n/d/x`) map to internal tokens
defined at the top of the file. If you add or remove a choice, update
`_apply()` and `_ask()` together.

### Change the console commands

Console commands are handled in `_console_loop()` in `app.py`. They are
plain `if`/`startswith` checks. Add yours there. Keep the help text in
`_print_help()` in sync.

### Change the HTTP client behavior

`provider.py` builds the request in `_headers()` and `_body()`. The
`extra_headers` and `extra_body` config keys let you merge provider-specific
flags without changing code. If you need a new top-level behavior (e.g.
streaming, retries), add it here and keep it behind a config flag if
possible.

### Change where state is stored

`config.default_state_dir()` in `config.py` controls the state directory.
`Permissions` and `Config` both receive `state_dir` as a constructor
argument from `app.py`. API keys are stored per provider — see
`_provider_service()` in `app.py`; `_keychain_service()` is the legacy
hash-of-base-URL scheme, kept only so old entries can be migrated. If you
move state, keep it outside any user project so the agent cannot edit its
own permissions.

## 4. What lives where — quick reference

| You want to change... | Edit this file |
|-----------------------|----------------|
| The public entry point / wiring | `app.py` |
| The conversation loop | `agent.py` |
| The system prompt | `agent.py` (`build_system_prompt`) |
| JEB.md discovery and context loading | `app.py` (`load_jeb_md_context`) |
| Which tools exist and their schemas | `tools.py` (`TOOL_SCHEMAS`) |
| Which tools need permission | `tools.py` (`CAPABILITY_MAP`) |
| What a tool actually does | `tools.py` (`_execute`) and possibly `workspace.py` or `runner.py` |
| File confinement / atomic writes | `workspace.py` |
| Python execution and termination blocking | `runner.py` |
| Permission prompt text and choices | `permissions.py` |
| What capabilities exist | `permissions.py` (`CAPABILITIES`) |
| HTTP request shape | `provider.py` |
| Provider settings (defaults, save/load) | `config.py` |
| First-run setup and legacy migration | `config.py` |
| Where state/config/permissions are stored | `config.py` (`default_state_dir`) and `app.py` (keychain service) |
| Console commands | `app.py` (`_console_loop`, `_print_help`) |
| The example launcher | `_jeb.py` |

## 5. A safe self-test you can run

If you want to confirm the package imports cleanly after your changes, you
can `run_python` a tiny script like:

```python
# check_imports.py
import miniagent
from miniagent import agent, app, config, permissions, provider, runner, tools, workspace
print("all modules import ok")
print("version:", miniagent.__version__)
```

This does not start the agent loop and does not touch the network. It only
exercises the import graph. Clean it up or leave it — your call.

## 6. What not to do

- **Do not** edit `permissions.json` or `config.json` through the file tools.
  They live outside the workspace by design. If you can reach them, something
  is misconfigured — tell the user.
- **Do not** add `import subprocess` or `import os` termination calls
  anywhere.
- **Do not** remove the workspace confinement check in
  `workspace.py:resolve()`.
- **Do not** start a nested `run()` call inside the running agent. You will
  hijack the console loop.

## 7. After you finish

When you are done with your changes:

1. Summarize what you changed and why, referencing the relative file paths.
2. Note anything the user should verify manually (e.g. re-running MiniAgent,
   checking a config value with `:config`).
3. If you created scratch files, mention them so the user can decide whether
   to keep them.
4. Stop. Reply with a normal text message — no dangling tool calls.
