# JEB.md — Project Instructions

MiniAgent supports a **JEB.md** file that acts like a `CLAUDE.md` or
`AGENT.md` file: it provides standing instructions that are automatically
discovered and injected into the system prompt at startup. This lets you give
the agent project-specific context, coding conventions, reminders, or
preferences that persist across every turn of every session.

## How it works

When MiniAgent starts (when `run()` is called), it looks for `JEB.md` files
in four locations:

| Order | Scope | Location | Purpose |
|-------|-------|----------|---------|
| 1 | Global (Documents version) | `~/Documents/miniagent/JEB.md` | Preferred global location — instructions that apply to every project on this install. |
| 2 | Global (original) | `~/miniagent/JEB.md` (in your home folder) | Legacy global location, kept for backward compatibility. |
| 3 | Package (self-editing only) | `<package>/JEB.md`, shipped with MiniAgent | The self-editing rules. Included **only** when the running `miniagent` package lives inside the workspace — i.e. the agent has been pointed at a directory that contains the harness running it. |
| 4 | Local | `<workspace>/JEB.md` (next to the launcher / in the project root) | Instructions specific to the current project. |

### The package entry, and why it is conditional

MiniAgent ships its own `JEB.md` carrying the rules for editing the harness
safely. Discovery has to go looking for it: the local lookup only checks
`<workspace>/JEB.md`, and in the usual self-editing setup — the workspace is
Pythonista's `site-packages` with `miniagent/` sitting *inside* it — that
path is a sibling of the package, not the shipped file. `jebmd.is_self_edit()`
detects that arrangement so the rules reach the system prompt automatically
instead of depending on the agent thinking to open `docs/self_editing.md`.

It is deliberately conditional. An unrelated project gets no self-editing
rules, because they would be noise that costs context on every turn. And when
the workspace *is* the package directory, the entry is skipped — the local
lookup already finds the same file, and including it twice would waste
context saying the same thing.

The contents are combined — **global first, then package, then local** — and appended
to the end of the system prompt, after the standard environment
constraints and coding behavior rules. When both global files exist they
are merged into a single section first (see
[Merging the two global files](#merging-the-two-global-files) below), with
conflicts resolved in favour of the Documents version.

If no file exists, nothing is added and the system prompt is unchanged.
Missing files are silently skipped — this is the normal case.

## Where to put each file

### Global `JEB.md`

Place this file in a `miniagent/` folder inside your Documents folder:

```
~/Documents/miniagent/JEB.md
```

The global file is discovered at runtime via
`Path.home() / "Documents" / "miniagent" / "JEB.md"`. On Pythonista/iOS this
is the user-visible Documents folder (available in the Files app), and —
like the legacy location — it lives outside the installed
`site-packages/miniagent` package, so it survives reinstalls and updates of
the miniagent module.

An older copy in `~/miniagent/JEB.md` is still read for backward
compatibility. When both files exist they are merged (see
[Merging the two global files](#merging-the-two-global-files)) rather than
concatenated, so the model never receives two conflicting sets of global
instructions.

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

## Merging the two global files

When both `~/Documents/miniagent/JEB.md` (the **Documents version**) and
`~/miniagent/JEB.md` (the **original**) exist, MiniAgent merges them into
one section instead of concatenating them, so you can keep an editable copy
in Documents without the model ever seeing two contradictory sets of global
instructions. The merge is deterministic:

1. **The Documents version wins conflicts.** Both files are split into
   sections by markdown heading and into *units* (a list item, including
   its continuation lines, or a block of text).
2. A unit present in both files (ignoring case and whitespace) is kept
   once.
3. A unit in the original that closely resembles a unit in the Documents
   version (text similarity of at least 85%) is treated as a *conflicting
   version of the same instruction*: the Documents version is kept and the
   original's version is dropped. The `:context` command reports every
   conflict resolved this way.
4. Everything else from the original is kept — sections unique to it are
   appended after the Documents sections, and units unique to it are
   appended within their own section.

If only one global file exists it is used as-is, so existing installs
that only have `~/miniagent/JEB.md` keep working unchanged.

## What the model sees

When one or more `JEB.md` files are present, the system prompt gets a section
appended that looks like:

```
Additional context from JEB.md files is provided below.
Global instructions come first: the Documents version
(~/Documents/miniagent/JEB.md) merged with any original copy
(~/miniagent/JEB.md), with conflicts resolved in favour of the
Documents version. Project-local instructions (from the workspace) follow.
Treat these as standing instructions that augment the rules above.

# Global JEB.md (~/Documents/miniagent merged with ~/miniagent)

<merged contents of the two global files>

---

# Project JEB.md (workspace)

<contents of <workspace>/JEB.md>
```

The sections are separated by a horizontal rule (`---`) and labelled so the
model can distinguish global from local instructions. The global section's
label reflects which files were found — for example
`# Global JEB.md (~/Documents/miniagent)` when only the Documents version
exists. The standard constraints (no shell, workspace confinement,
termination blocking, etc.) always come **before** the JEB.md content and
are never overridden by it.

## Checking what was loaded

Use the `:context` console command to see which `JEB.md` files were found and
the combined context that was added to the system prompt:

```
You> :context
```

This prints every candidate path with a `found`/`missing` marker (both
global locations plus the local one), any conflicts that were resolved in
favour of the Documents version, and the full combined text. If no file
exists it prints:

```
No JEB.md files found (global or local).
```

## Size limits

Each `JEB.md` file is truncated at 32,000 characters. A truncation notice is
appended if the file is longer. This keeps the system prompt from growing
unboundedly. If you need more space, consider pointing the agent at other
documentation files (via `read_file`) from within your `JEB.md`.

## Editing JEB.md

All of them are plain UTF-8 Markdown. You can create and edit them with any
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
