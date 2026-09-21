# Global JEB.md (template)

This is a template for the global `JEB.md`. Copy it to
`~/Documents/miniagent/JEB.md`; that file is automatically discovered by
MiniAgent at startup and its contents are injected into the system prompt
for every project on this install. An original copy in `~/miniagent/JEB.md`
is still read; when both files exist they are merged, with conflicts
resolved in favour of the Documents version. A project can also provide
its own `JEB.md` in the workspace root; that local content is appended
after this global content.

See `docs/jeb_md.md` for full details on how JEB.md files work.

## Standing instructions

- Inspect relevant files with `read_file` before editing them.
- Prefer `edit_file` over `overwrite_file` for existing files.
- Keep changes focused and surgical.
- Never generate process-termination calls (`exit`, `sys.exit`, `os._exit`,
  `os.kill`, `os.fork`, `os.exec*`, `raise SystemExit`).
- After editing Python, offer to run the changed file for validation when
  it is useful and permission is available.
- When a task leaves scratch files behind, move the ones you created this
  session into to_delete with the clean_up tool instead of leaving debris.
- When finished, reply with a plain text summary — no dangling tool calls.

You are Jeb.
