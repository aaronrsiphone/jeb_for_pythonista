# JEB.md — MiniAgent self-editing rules

You are running inside the MiniAgent harness, and the project you have been
pointed at is the harness's own source. This file is standing context, not
a substitute for reading [`docs/self_editing.md`](docs/self_editing.md) (the
full guide) and [`docs/reference.md`](docs/reference.md) (the generated,
always-current list of every tool, command and module) when you need detail
— it exists so the rules that prevent real damage reach you even if you
never open either file.

## Golden rules

- Inspect before you change. `read_file` a file — including the region
  around your target — before you edit it. Never edit a file you have not
  read in this session.
- Prefer `edit_file` or `multi_edit` over `overwrite_file`. If a match
  fails, read what the error actually says (it names the closest match or
  every occurrence's line number) before retrying; use `occurrence=N` or
  `replace_all=true` instead of guessing.
- Never weaken or remove safety machinery: `workspace.py`'s path-confinement
  check (`resolve()`), the `.py` compile gate in `_atomic_write`,
  `runner.py`'s termination-call blocking and its `miniagent`/`miniagent.*`
  import-purge exclusion, or `permissions.py`/`checkpoints.py` storing their
  data outside the workspace. These keep the runner alive and this session
  recoverable — do not touch them unless that is the explicit task.
- Every mutating tool call is checkpointed automatically and `:undo` can
  restore what one turn changed. That is a safety net for mistakes, not
  license to skip reading a file first.
- Run the test suite after a change that could plausibly affect more than
  the file you touched (`run_python .../tests/run_all.py`), and add a test
  for new behavior instead of a scratch script you delete afterwards.
- If you add or change a tool, a command, or a module's docstring,
  regenerate the generated reference doc (`run_python .../gendocs.py`) —
  the checked-in copy is tested against it and the suite will fail until you
  do.
- Never generate process-termination calls: `exit()`, `sys.exit()`,
  `os._exit()`, `os.kill()`, `os.fork()`, `os.exec*()`, `raise SystemExit`.
  The runner blocks them; do not generate them anyway.
- Keep changes focused — one logical change at a time — so a failure stays
  recoverable and a diff stays reviewable.
- When finished, reply with a plain text summary — no dangling tool calls.

See [`docs/self_editing.md`](docs/self_editing.md) for the full guide
(including how to add a tool or a command) and
[`docs/architecture.md`](docs/architecture.md) for how the pieces fit
together.
