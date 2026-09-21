# Testing the Harness Without Interactive Prompts

This guide is for a coding agent (or a human) validating changes to
MiniAgent itself — the agent loop, the permission layer, tools dispatch, the
runner, or console commands. It exists because of a real accident:

> A permissions test constructed a fresh `Permissions` instance pointed at
> the **wrong (empty) state directory**, so a previously stored
> always-allow decision was not found. The code fell through to a default
> prompt that called `input()`. Under `run_python`, stdout is captured into
> a buffer, so the prompt was invisible — and `input()` blocked forever on
> the real stdin. Pythonista had to be force-quit, because there is no
> in-process way to interrupt a blocked read.

That accident is why `run_python` disables interactive input outright (see
below), and it is also why the engine no longer prompts from deep inside a
call stack at all: `Agent.turn()` and `Tools.dispatch()` are generators that
yield typed events (`events.py`), and the one event that needs an answer —
`PermissionNeeded` — is answered by whatever is driving the turn, not by a
buried `input()` call. `Permissions` (`permissions.py`) is policy only now:
it does no I/O and never prompts. The `Headless` renderer (`ui/headless.py`)
answers `PermissionNeeded` from a scripted queue and records every event, so
most of what used to require monkeypatching `builtins.input` is now a matter
of driving a turn and asserting on the events it produced.

## Why interactive input can never work under run_python

`Runner.run()` (in `runner.py`) executes the script **in-process** and
captures stdout/stderr into buffers that are handed back only when the run
ends. Consequences:

- Anything the script prints is invisible until the run finishes.
- Nobody can type into the captured session, so `input()` waits forever.
- There is no subprocess and Python threads cannot be killed, so nothing in
  the harness can interrupt a blocked read. **Prevention is the only
  strategy** — which is why the block below exists.

## What the runner does about it

For the duration of every `run_python` run:

- `builtins.input` becomes a callable that raises `InteractiveInputBlocked`.
- `sys.stdin` becomes an object whose read-like methods (`read`,
  `readline`, `readlines`, iteration, `buffer.read`, ...) raise
  `InteractiveInputBlocked` immediately.

The error message explains the problem and points to this guide. A script
that trips it fails in milliseconds with a clear traceback in the run
result's `stderr` — instead of hanging Pythonista. The original `input` and
`sys.stdin` are restored afterwards, so the interactive console is
unaffected.

A script **may rebind `builtins.input` itself** — that is exactly how a test
that still needs a scripted `input()` answer works (§3, below); the block
only covers the *default* interactive path. Rebinding is safe because the
runner restores `input` and `print` after every run.

`InteractiveInputBlocked` is a `RuntimeError`, not an `EOFError` — so code
that treats `EOFError` as "no answer, use a safe default" (the `:resume`
pager does; see below) fails loudly instead of silently doing the wrong
thing if it unexpectedly reaches a blocked read.

## 1. Driving the agent loop: `Headless` + `ui.drive()`

This is now the primary technique for anything that used to need
`builtins.input` monkeypatching: the agent loop, tool dispatch, permission
gating.

```python
from miniagent.agent import Agent
from miniagent.ui import drive
from miniagent.ui.headless import Headless

agent = Agent(provider, tools, "system prompt")
seen = Headless(["y"])          # one scripted answer per PermissionNeeded
result = drive(agent, "do the thing", seen)

assert result == seen.text      # the turn's final reply
```

- `Headless(answers)` takes a list consumed **in order**, one entry per
  `PermissionNeeded` event: either a raw typed answer (`"y"`,
  `"n. use another path"`, parsed exactly the way the console renderer
  parses a keystroke) or an already-built `PermissionAnswer` if you want to
  skip parsing.
- `seen.events` is every event the turn produced, in order — assert on
  types (`seen.events_of(ToolCompleted)`) or the exact sequence
  (`[type(e).__name__ for e in seen.events]`).
- An **exhausted queue is a deny-once**, the same safe default a blank
  console answer has always been — this makes "what happens when nobody
  answers?" a thing a test can assert on, not a thing that hangs.
- `ui.drive()` is the same pump the console loop uses (`console/loop.py`
  calls it too), so a test exercises the real turn protocol, not a
  simplified stand-in for it.

See `miniagent/tests/test_agent.py` for the full pattern, including how to
double the provider and the tools dispatcher, and `test_tools.py` /
`test_registry.py` for driving `Tools.dispatch()` (also a generator; see the
`dispatch_result()` helper in `test_registry.py` for pumping it directly
without `Agent` or a renderer at all) end to end.

## 2. Testing `Permissions` directly

`Permissions` needs no renderer and no scripted queue at all — it is pure
policy. Test it directly:

```python
from miniagent.permissions import Permissions

perms = Permissions(state_dir, project_root)
assert perms.decide("write") is None          # nobody has decided yet

answer = Permissions.parse_answer("y")          # what did the user type?
allowed, comment = perms.apply_answer("write", answer)  # record it
assert perms.decide("write") is True            # ...and now it's decided
```

Point every instance at a **fresh temporary state dir** per test (the
original accident above was an unintentional mismatch between two
instances pointed at different directories). When a test deliberately
checks persistence, reuse the *same* dir on purpose, across two `Permissions`
instances, and assert the second one sees what the first one stored. See
`miniagent/tests/test_permissions.py`.

## 3. Where `builtins.input` is still the right tool

A few things genuinely read from the terminal directly rather than going
through the event stream, because they are not part of a *turn* — they are
the console's own bookkeeping UI. For these, stubbing `builtins.input` (or
`input` as imported into the module under test) is still correct:

- **The `:resume` pager** (`console/resume.py`) — paging through recorded
  sessions and picking one. `test_sessions.py` drives
  `resume_command(agent, session_logger)` directly with a scripted
  `input()` (via a small fake or by rebinding `builtins.input`), covering
  paging, cancel, EOF, and retry-on-bad-input.
- **Commands that ask a plain yes/no or a number directly**, not through a
  permission prompt: `:clear-perms`'s confirmation, `:undo`'s confirmation,
  and `:model` with no argument (its "pick a number" prompt).
  `test_commands.py` stubs `builtins.input` for exactly these handlers and
  nothing else.

The pattern is the same as before this phase — construct a fake or a
context manager that feeds fixed answers and restores the real `input`
afterward:

```python
import builtins

class ScriptedInput:
    """Feeds a fixed queue of answers to input(); raises when exhausted."""

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt=""):
        if not self.answers:
            raise AssertionError(f"scripted answers exhausted; unexpected prompt: {prompt!r}")
        return self.answers.pop(0)

old_input = builtins.input
builtins.input = ScriptedInput(["y"])
try:
    ...  # exercise the code that prompts
finally:
    builtins.input = old_input
```

Do **not** reach for this for anything that goes through
`Tools.dispatch()`'s permission gate or the agent loop — those are
`PermissionNeeded` events now, and `Headless` (§1) is both simpler and
exercises the real protocol.

## 4. Reading `run_python` results

`Runner.run(path)` returns `{"ok", "stdout", "stderr"}`. A blocked
interactive-input probe shows up as `ok=false` with the
`InteractiveInputBlocked` traceback in `stderr` — assert on that text when
you *want* the block to fire (confirming the safety net still works), and
treat it as a bug in the script under test when you do not.

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
- Each test is a standalone script: it never blocks on a real prompt (use
  §1/§2/§3 above as the situation calls for), works only in throwaway
  directories under the system temp folder, and re-imports the package fresh
  (deleting `miniagent`/`miniagent.*` from `sys.modules` first) so the
  current source is exercised, not whatever was imported at session start.
- A test prints its own PASS/FAIL lines and **raises `AssertionError` at the
  end if anything failed**, so a failure is always visible in the run result
  — including when it runs inside the suite.
- Run one file directly with `run_python miniagent/tests/test_<name>.py`, or
  the whole suite with `run_python miniagent/tests/run_all.py` (which runs
  every `test_*.py` in the folder and raises if any failed). `test_vision.py`
  is expected to fail with `No module named 'PIL'` on an install without
  Pillow — that is a missing optional dependency, not a regression.
- If you changed a tool, a command, or a module's docstring, also run
  `run_python miniagent/gendocs.py` and re-run the suite:
  `test_reference.py` fails the suite if `docs/reference.md` is out of date,
  and tells you the exact command to fix it.
- One-off *probe* scripts (diagnostics, not tests) may still be created in
  the workspace root and moved to `to_delete/` with `clean_up` when their
  job is done — but if the probe verified a fix, consider turning it into a
  test in `miniagent/tests/` first.
- Never start the interactive console (`miniagent.run()` / `console_loop`)
  from a test — it reads stdin, which is blocked under `run_python` by
  design.
- Avoid native input dialogs (`console.secure_input`, `console.alert`) in
  tests too: they do not hang invisibly, but they still demand a human.
