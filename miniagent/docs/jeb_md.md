# JEB.md — Project Instructions

MiniAgent supports a **JEB.md** file that acts like a `CLAUDE.md` or
`AGENT.md` file: it provides standing instructions that are automatically
discovered and injected into the system prompt at startup. This lets you give
the agent project-specific context, coding conventions, reminders, or
preferences that persist across every turn of every session.

## How it works

When MiniAgent starts (when `run()` is called), it looks for `JEB.md` files
in two locations:

| Order | Scope | Location | Purpose |
|-------|-------|----------|---------|
| 1 | Global | `~/miniagent/JEB.md` (in your home folder) | Instructions that apply to every project on this install. |
| 2 | Local | `<workspace>/JEB.md` (next to the launcher / in the project root) | Instructions specific to the current project. |

The contents are concatenated in that order — **global first, local second** —
and appended to the end of the system prompt, after the standard environment
constraints and coding behavior rules.

If neither file exists, nothing is added and the system prompt is unchanged.
If only one exists, only that one is used. There is no error or warning when
a file is absent — this is the normal case.

## Where to put each file

### Global `JEB.md`

Place this file in a `miniagent/` folder in your home directory:

```
~/miniagent/JEB.md
```

The global file is discovered at runtime via
`Path.home() / "miniagent" / "JEB.md"`. Keeping it in your home folder —
rather than inside the installed `site-packages/miniagent` package — means
it survives reinstalls and updates of the miniagent module.

Use this for install-wide preferences like:

- A default coding style or formatting convention.
- A reminder to always run a linter after edits.
- Instructions about how you like commits or file organization.

### Local `JEB.md`

Place this file in the project workspace root — the directory where your
`jeb.py` launcher lives and that you pass as `project_root`. For example:

```
MyProject/
    jeb.py
    JEB.md          ← project-local instructions
    src/
    ...
```

Use this for project-specific context like:

- A description of the project architecture.
- Build or test commands (the agent can run them via `run_python`).
- Conventions specific to this codebase.
- A list of files the agent should not touch.

## What the model sees

When one or both `JEB.md` files are present, the system prompt gets a section
appended that looks like:

```
Additional context from JEB.md files is provided below.
Global instructions (from ~/miniagent) appear first, followed by
project-local instructions (from the workspace). Treat these as standing
instructions that augment the rules above.

# Global JEB.md (~/miniagent)

<contents of ~/miniagent/JEB.md>

---

# Project JEB.md (workspace)

<contents of <workspace>/JEB.md>
```

The two sections are separated by a horizontal rule (`---`) and labelled so
the model can distinguish global from local instructions. The standard
constraints (no shell, workspace confinement, termination blocking, etc.)
always come **before** the JEB.md content and are never overridden by it.

## Checking what was loaded

Use the `:context` console command to see which `JEB.md` files were found and
the combined context that was added to the system prompt:

```
You> :context
```

This prints the global path (if found), the local path (if found), and the
full combined text. If neither file exists it prints:

```
No JEB.md files found (global or local).
```

## Size limits

Each `JEB.md` file is truncated at 32,000 characters. A truncation notice is
appended if the file is longer. This keeps the system prompt from growing
unboundedly. If you need more space, consider pointing the agent at other
documentation files (via `read_file`) from within your `JEB.md`.

## Editing JEB.md

Both files are plain UTF-8 Markdown. You can create and edit them with any
text editor, or — since the local file lives inside the workspace — the agent
itself can create and edit it using `create_file` / `edit_file` (subject to
the normal `write` / `edit` permissions).

Changes to `JEB.md` take effect on the **next startup** of MiniAgent. They
are not hot-reloaded into an already-running session. To pick up changes, use
`:quit` and relaunch, or (for the local file) use `:reset` after restarting.

## Example global `JEB.md`

```markdown
# Global instructions

- Always add a short docstring to new functions.
- Prefer `edit_file` over `overwrite_file` for existing files.
- After editing Python, offer to run the changed file for validation.
- Never delete test files without asking first.
```

## Example local `JEB.md`

```markdown
# MyProject notes

This is a Flask web app. The entry point is `app/__init__.py`.

## Conventions
- All API routes are under `app/api/`.
- Database models live in `app/models/`.
- Run tests with: `run_python tests/run_tests.py`

## Do not touch
- `app/legacy/` — deprecated code scheduled for removal.
- `config/prod.json` — production secrets, managed manually.
```
