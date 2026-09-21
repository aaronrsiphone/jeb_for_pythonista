<!--
    GENERATED FILE — DO NOT EDIT BY HAND.

    This file is produced by miniagent/gendocs.py from the tool registry,
    the command registry, and every module's own docstring.  To regenerate
    it after changing a tool, a command, or a module docstring, run:

        run_python miniagent/gendocs.py

    miniagent/tests/test_reference.py fails if this file is out of date;
    that is the check that actually enforces "regenerate it", not this
    comment.  See docs/rearchitecture.md §5.7 for why this file exists.
-->

# MiniAgent reference

Generated tables for the tool registry, the command registry, and the
package's module layout. Prose about *why* the system is built this way
lives in [`architecture.md`](architecture.md); this file only lists *what
currently exists*, read straight from the code.

## Tools (11)

| Tool | Capability | Description |
|---|---|---|
| `list_files` | none | List files and directories inside the project workspace. |
| `read_file` | none | Read a UTF-8 text file with line numbers. |
| `search_files` | none | Search the contents of files inside the project workspace for a pattern, returning matching lines with file path and line number. Case-insensitive literal search by default; set regex=true for regular expressions. Skips non-UTF-8 and very large files. |
| `create_file` | write | Create a new UTF-8 text file. Fails if it already exists. |
| `edit_file` | edit | Replace text in an existing file. By default old_text must occur exactly once; a non-unique match reports every occurrence's line number instead of just failing, and no match reports the closest-matching region of the file with a diff of what differs. Set replace_all=true to replace every occurrence, or occurrence=<1-based index> to pick one specific occurrence (mutually exclusive with replace_all). |
| `multi_edit` | edit | Apply a list of {old_text, new_text} edits to one existing file atomically: either every edit applies (each old_text must be a unique match in the file's content at the point it is applied, in order) or the file is left completely untouched and the error names which edit failed and why. One permission prompt covers the whole change instead of one per edit. |
| `overwrite_file` | overwrite | Replace all contents of an existing file. |
| `clean_up` | none | Move scratch files you created with create_file during this session into the workspace to_delete folder (a recoverable trash area - nothing is permanently deleted). Use this at the end of a task to tidy away temporary files you no longer need. Only files created in the current session are accepted; anything else is refused and listed under 'skipped' with a reason. |
| `run_python` | run_python | Execute a .py file in-process inside the project workspace. NOT sandboxed. Requires the run_python permission. The script's stdout/stderr are captured and returned; interactive input (input() or sys.stdin) is disabled and fails fast with an error instead of prompting. Never write validation scripts that read stdin; script any answers the code under test would prompt for. |
| `ask_image` | ask_image | Ask the configured vision model a question about one or more images. Image sources, in the order sent: 'images' (workspace-relative file paths and/or http(s) image URLs), 'photo' (iOS photo-library image index; negative counts from the end, so -1 is the most recent photo), and/or 'clipboard' (the image currently on the clipboard). The image data is uploaded to the vision provider's API (config.json 'vision_model', format '<provider>/<model-name>'). Oversized images are automatically shrunk and re-encoded as JPEG. Returns the model's answer plus token usage. |
| `knowledge` | none | Read-only access to this install's Pythonista reference material; no permission required. Actions: 'list' (index the files under the optional, user-populated miniagent/knowledge/ directory, if present), 'read' (line-numbered contents of one such file), 'search' (literal or regex across those files), 'docs' (offline search of Pythonista's bundled official documentation: 'query' returns ranked symbol matches, 'page' returns a doc page's readable text, no arguments lists the Pythonista module doc pages). 'list'/'read'/'search' report a clear error when the knowledge directory is absent; 'docs' is independent of it and always works. No action writes files or executes code. Knowledge paths are relative to the knowledge root and cannot escape it. |

## Commands (16)

| Command | Aliases | Summary |
|---|---|---|
| `:help` | — | Show commands. |
| `:quit` | `:q`, `:exit` | Stop MiniAgent normally. |
| `:reset` | — | Reset conversation context. |
| `:resume` | — | List recorded sessions for this workspace, 4 per page, and reinstate the chosen session's message history so the conversation picks up where it left off. |
| `:undo` | — | Restore every file the last turn touched to its state from before that turn (a create_file is undone by deleting the file it created). |
| `:checkpoints` | — | List the retained undo groups (newest first): turn id, timestamp and the files each one touched. |
| `:config` | — | Print provider configuration. |
| `:key` | — | Show whether an API key is stored. |
| `:model` | `:models` | List every configured model as <provider>/<model-name>, numbered, and select one to use for the current session (selection is not persisted). |
| `:effort` | — | Set reasoning effort level sent as reasoning_effort. |
| `:verbose` | — | Switch the front end between the compact console renderer (the default: collapsed reasoning, one line per tool call, the permission legend shown once) and the verbose one (full reasoning, separate tool request/result lines, the legend on every prompt). |
| `:perms` | — | Show permission state. |
| `:clear-perms` | — | Clear session and persistent permissions. |
| `:files` | — | List top-level project files. |
| `:workspace` | — | Print the project workspace path. |
| `:context` | — | Show JEB.md files discovered (global + package + local) and the combined context that was added to the system prompt. |

## Module layout (45 files)

| Module | Purpose |
|---|---|
| `__init__.py` | MiniAgent — a small coding agent for Pythonista. |
| `agent.py` | Agent loop: model/tool orchestration. |
| `app.py` | High-level construction — the console front end's entry point. |
| `checkpoints.py` | Undo safety net for self-editing (§5.2 of docs/rearchitecture.md). |
| `config.py` | MiniAgent application configuration. |
| `console/__init__.py` | The interactive console: the command registry and the input loop. |
| `console/commands/__init__.py` | Every console command, registered with ``@command`` (§5.5), by area. |
| `console/commands/config_cmds.py` | Provider/model/key configuration and the front-end renderer switch. |
| `console/commands/help_cmds.py` | ``:help`` and ``:quit`` (plus its ``:q``/``:exit`` aliases). |
| `console/commands/perms_cmds.py` | ``:perms`` and ``:clear-perms``. |
| `console/commands/session_cmds.py` | Conversation and self-editing safety-net commands. |
| `console/commands/workspace_cmds.py` | ``:files``, ``:workspace`` and ``:context`` (the JEB.md inspector). |
| `console/loop.py` | The interactive console loop. |
| `console/registry.py` | The ``@command`` decorator: registration for console (``:...``) commands. |
| `console/resume.py` | The ``:resume`` pager: pick a recorded session and reinstate its history. |
| `context.py` | The live wiring object handed to every console command (§5.6). |
| `events.py` | Typed events exchanged between the agent loop and a front end. |
| `gendocs.py` | Generate ``docs/reference.md`` from the live tool/command registries and every module's own docstring (§5.7 of ``docs/rearchitecture.md``). |
| `jebmd.py` | JEB.md discovery and the global/local merge engine. |
| `keys.py` | Keychain / API-key loading, storage and legacy migration. |
| `knowledge.py` | This install's Pythonista reference material, for the knowledge tool. |
| `permissions.py` | Permission policy for MiniAgent tools. |
| `provider.py` | Provider-neutral, OpenAI-compatible Chat Completions client. |
| `runner.py` | In-process Python execution for the project workspace. |
| `sessions.py` | Session recording and resumption. |
| `tools/__init__.py` | Tool schemas, dispatch and permission gating. |
| `tools/ask_image.py` | ``ask_image``: ask the configured vision model about image(s) (gated, ASK_IMAGE). |
| `tools/clean_up.py` | ``clean_up``: move this session's own scratch files into to_delete. |
| `tools/create_file.py` | ``create_file``: create a new text file (gated, WRITE). |
| `tools/dispatch.py` | ``Tools``: the dispatch generator and permission gating. |
| `tools/edit_file.py` | ``edit_file``: replace one unique text occurrence (gated, EDIT). |
| `tools/knowledge.py` | ``knowledge``: read-only access to this install's Pythonista reference material. |
| `tools/list_files.py` | ``list_files``: list a directory inside the project workspace. |
| `tools/multi_edit.py` | ``multi_edit``: apply several edits to one file atomically (gated, EDIT). |
| `tools/overwrite_file.py` | ``overwrite_file``: replace a whole file's contents (gated, OVERWRITE). |
| `tools/read_file.py` | ``read_file``: read a text file with line numbers. |
| `tools/registry.py` | The ``@tool`` decorator: registration and schema generation. |
| `tools/run_python.py` | ``run_python``: execute a workspace .py file in-process (gated, RUN_PYTHON). |
| `tools/search_files.py` | ``search_files``: search file contents inside the project workspace. |
| `ui/__init__.py` | Front ends for the agent's event stream. |
| `ui/console.py` | Compact terminal renderer — the default, tuned for a phone screen. |
| `ui/headless.py` | Silent renderer with scripted answers — for tests and automation. |
| `ui/verbose.py` | Expanded renderer — the debugging / desktop view. |
| `vision.py` | Vision question-answering for the ``ask_image`` tool. |
| `workspace.py` | Workspace confinement and low-level file operations. |
