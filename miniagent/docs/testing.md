# Testing the Harness Without Interactive Prompts

This guide is for a coding agent (or a human) validating changes to
MiniAgent itself — the permission layer, tools dispatch, the runner, or any
code whose real behavior is an interactive prompt. It exists because of a
real accident:

> A permissions test constructed a fresh `Permissions` instance pointed at
> the **wrong (empty) state directory**, so a previously stored
> always-allow decision was not found. The code fell through to the default
> prompt, which calls `input()`. Under `run_python`, stdout is captured into
> a buffer, so the prompt was invisible — and `input()` blocked forever on
> the real stdin. Pythonista had to be force-quit, because there is no
> in-process way to interrupt a blocked read.

## Why interactive input can never work under run_python

`Runner.run()` (in `runner.py`) executes the script **in-process** and
captures stdout/stderr into buffers that are handed to the agent only when
the run ends. Consequences:

- Anything the script prints — including a permission prompt — is invisible
  until the run finishes.
- Nobody can type into the captured session, so `input()` waits forever.
- There is no subprocess and Python threads cannot be killed, so nothing in
  the harness can interrupt a blocked read. **Prevention is the only
  strategy** — which is why the block below exists.

## What the harness does about it

The runner replaces the interactive path for the duration of every run:

- `builtins.input` becomes a callable that raises `InteractiveInputBlocked`.
- `sys.stdin` becomes an object whose read-like methods (`read`,
  `readline`, `readlines`, iteration, `buffer.read`, ...) raise
  `InteractiveInputBlocked` immediately.

The error message explains the problem and points to this guide. A script
that trips it fails in milliseconds with a clear traceback in the run
result's `stderr` — instead of hanging Pythonista. The original `input` and
`sys.stdin` are restored afterwards, so the interactive console is
unaffected.

A script **may rebind `builtins.input` itself** — that is exactly how
scripted answers work — the block only covers the default interactive path.
Rebinding is safe because the runner restores `input` and `print` after
every run.

Important nuance for `permissions.py`: `_default_prompt` treats `EOFError`
as "blank answer → deny once". `InteractiveInputBlocked` is deliberately a
`RuntimeError`, **not** an `EOFError`, so a test that unexpectedly reaches
the default prompt fails loudly instead of silently recording a fake
"deny".

## The five rules of harness tests

1. **Never let code under test reach a default prompt.** Construct
   `Permissions` with an injected `prompt=...` callable, or pre-arrange a
   session/persistent decision so no prompt is needed.
2. **Make scripted answers fail loudly when exhausted.** If the code
   consumes more answers than you scripted, raise — do not loop forever
   and do not invent defaults.
3. **Point every instance at the state directory you mean.** Use a fresh
   temporary state dir per test. When a test deliberately checks
   persistence, pass the *same* dir on purpose — the original accident was
   an unintentional mismatch between two instances.
4. **Guard `builtins.input` as belt-and-braces.** Stub it at the top of the
   test with a callable that raises. (The runner already blocks it; the
   stub covers code that rebinds it back.)
5. **Report results as printed PASS/FAIL lines** and raise at the end if
   anything failed, so the run's `stdout`/`ok` shows exactly what passed.
   Treat a traceback in `stderr` as a test bug to fix, not a harness bug.

## Patterns

### Scripted prompt queue (rule 1 + 2)

```python
class ScriptedPrompt:
    """Answers a queue of scripted responses; raises when exhausted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, message):
        self.seen.append(message)
        if not self.answers:
            raise AssertionError(
                f"scripted answers exhausted; unexpected prompt: {message!r}"
            )
        return self.answers.pop(0)
```

### Guard stub for builtins.input (rule 4)

```python
import builtins

def _no_input(message):
    raise AssertionError(f"unexpected interactive prompt: {message!r}")
builtins.input = _no_input
```

### Fresh state dir per test (rule 3)

```python
import tempfile
from miniagent.permissions import Permissions

state = tempfile.mkdtemp(prefix="perm_test_")
perms = Permissions(state, str(project_root), prompt=ScriptedPrompt(["y"]))
# ... and for the persistence case, deliberately reuse `state`:
perms_again = Permissions(state, str(project_root))  # no prompt expected
```

### Driving Tools.dispatch end to end

```python
import json, tempfile
from miniagent.permissions import Permissions
from miniagent.runner import Runner
from miniagent.tools import Tools
from miniagent.workspace import Workspace

ws = Workspace(project_root)
perms = Permissions(tempfile.mkdtemp(prefix="pt_"), str(project_root),
                    prompt=ScriptedPrompt(["n. use another path"]))
tools = Tools(ws, perms, Runner(ws))
result = json.loads(tools.dispatch({
    "id": "t1",
    "function": {"name": "create_file",
                 "arguments": json.dumps({"path": "_probe.txt",
                                           "content": "x"})},
}))
assert result["ok"] is False and result["denied"] is True
assert result["user_comment"] == "use another path"
```

### Reading run results

`Runner.run(path)` returns `{"ok", "stdout", "stderr"}`. A blocked probe
shows up as `ok=false` with the `InteractiveInputBlocked` traceback in
`stderr` — assert on that text when you *want* the block to fire, and treat
it as a bug when you do not.

## What the harness cannot recover from

Prevention covers stdin, but in-process execution has no general watchdog:
a script stuck in a pure-Python infinite loop, or blocked in a native call
elsewhere, still hangs Pythonista until force-quit. Keep validation
scripts small, non-interactive, and free of long waits. If a run seems to
hang, force-quit Pythonista, restart, and re-run the script after fixing
it — the harness cannot save you from that class of hang, only from the
interactive-input one.

## Conventions for tests

- **Permanent tests live in `miniagent/tests/` as `test_*.py`.** Do not
  create scratch test files in the package root and delete them afterwards —
  add to the suite instead, so every fix keeps its regression test. The
  folder ships with the package (see `INSTALL.md`).
- Each test is a standalone script: it never prompts (use the patterns
  above), works only in throwaway directories under the system temp folder,
  and re-imports the package fresh so the current source is exercised.
- A test prints its own PASS/FAIL lines and **raises `AssertionError` at the
  end if anything failed** (rule 5 above), so a failure is always visible in
  the run result — including when it runs inside the suite.
- Run one file directly with `run_python miniagent/tests/test_<name>.py`, or
  the whole suite with `run_python miniagent/tests/run_all.py` (which runs
  every `test_*.py` in the folder and raises if any failed).
- One-off *probe* scripts (diagnostics, not tests) may still be created in
  the workspace root and moved to `to_delete/` with `clean_up` when their
  job is done — but if the probe verified a fix, consider turning it into a
  test in `miniagent/tests/` first.
- Never start the interactive console (`miniagent.run()` /
  `app.py`'s loop) from a test — it reads stdin, which is blocked under
  `run_python` by design.
- Avoid native input dialogs (`console.secure_input`, `console.alert`) in
  tests too: they do not hang invisibly, but they still demand a human.
