"""Tests for session recording and resume (sessions.py + wiring).

Safe to run via run_python: no network, no console loop, and no writes
outside the system temp directory — the real ~/Documents/miniagent is never
touched because every SessionLogger is pointed at a throwaway state dir.
Agent turns are driven headlessly (miniagent.ui.drive + Headless): the agent
is a generator of events and prints nothing, so no stdout capture or input
stubbing is involved in recording a turn. The one remaining interactive path
exercised here is :resume's picker in app.py, which still reads its selection
with input() and is therefore driven through a scripted builtins.input queue.
Lives permanently in miniagent/tests/; run it directly or through run_all.py.
"""

import builtins
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent import app as ma_app  # noqa: E402
from miniagent.agent import Agent  # noqa: E402
from miniagent.events import ToolCompleted, ToolStarted  # noqa: E402
from miniagent.sessions import (  # noqa: E402
    SessionError,
    SessionLogger,
    repair_messages,
    workspace_key,
)
from miniagent.ui import drive  # noqa: E402
from miniagent.ui.headless import Headless  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


def read_lines(path):
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeProvider:
    """Canned chat responses; Agent uses Provider's static extract/split."""

    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, tools=None):
        return {"choices": [{"message": self.responses.pop(0)}]}


class FakeTools:
    """Dispatcher stub matching the generator contract of Tools.dispatch.

    It yields the lifecycle events and *returns* the JSON string the model
    sees, so the agent's ``result = yield from tools.dispatch(call)`` works
    against it unchanged.
    """

    schemas = []

    def dispatch(self, call):
        name = call.get("function", {}).get("name", "")
        outcome = {"ok": True, "value": 1}
        yield ToolStarted(name, {})
        yield ToolCompleted(name, "ok", "", outcome)
        return json.dumps(outcome)


class ScriptedInput:
    """Answers a queue of scripted input() responses; raises when exhausted.

    Only the :resume picker needs this: it is the last prompt in the code
    base that still reads from builtins.input.  Permission prompts are
    events now and are answered through the headless renderer instead.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(
                f"scripted input exhausted; prompt: {prompt!r}")
        return self.answers.pop(0)


class EOFInput:
    """input() stub that raises EOFError, for the picker's cancel path."""

    def __call__(self, prompt=""):
        raise EOFError


def fresh_logger(prefix):
    ws = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_ws_"))
    state = tempfile.mkdtemp(prefix=f"ma_{prefix}_state_")
    return ws, state, SessionLogger(ws, state_dir=state,
                                    provider="prov", model="mod")


def with_patched_input(fake, fn):
    """Run fn() with builtins.input replaced by *fake*; restore afterwards."""
    original = builtins.input
    builtins.input = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            fn()
    finally:
        builtins.input = original
    return buffer.getvalue()


tmp_roots = []  # cleaned in finally

try:
    # --- workspace_key ------------------------------------------------------

    base = Path(tempfile.mkdtemp(prefix="ma_key_base_"))
    tmp_roots.append(base)
    (base / "projects" / "foo").mkdir(parents=True)
    (base / "solo").mkdir()
    outside = Path(tempfile.mkdtemp(prefix="ma_key_out_"))
    tmp_roots.append(outside)

    check("key for nested workspace",
          workspace_key(base / "projects" / "foo", base_dir=base),
          "projects-foo")
    check("key for direct child",
          workspace_key(base / "solo", base_dir=base), "solo")
    check("key outside base falls back to full path",
          workspace_key(outside, base_dir=base),
          str(outside.resolve()).replace("/", "-").strip("-"))
    check("key for the base directory itself",
          workspace_key(base, base_dir=base), "root")

    # --- lazy start + record round-trip -------------------------------------

    ws, state, logger = fresh_logger("rec")
    tmp_roots += [ws, Path(state)]

    check("no active session before first message", logger.active_id, None)
    check("no sessions when directory missing", logger.list_sessions(), [])

    user = {"role": "user", "content": "first prompt line\nsecond line"}
    logger.record(user)
    sid = logger.active_id
    check("session id assigned lazily", sid is not None, True)
    path = logger.session_path(sid)
    lines = read_lines(path)
    check("meta line comes first", lines[0].get("type"), "meta")
    check("meta version", lines[0].get("version"), 1)
    check("meta workspace recorded", lines[0].get("workspace"), str(ws.resolve()))
    check("meta provider and model",
          (lines[0].get("provider"), lines[0].get("model")), ("prov", "mod"))
    check("meta started present", bool(lines[0].get("started")), True)
    check("user message recorded verbatim",
          lines[1], {"type": "message", "message": user})

    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"}}],
    }
    tool = {"role": "tool", "tool_call_id": "c1", "name": "read_file",
            "content": '{"ok": true}'}
    final = {"role": "assistant", "content": "all done"}
    for msg in (assistant, tool, final):
        logger.record(msg)

    logger.record({"role": "system", "content": "SECRET system prompt"})
    check("system message not recorded", len(read_lines(path)), 5)

    sessions = logger.list_sessions()
    check("one session listed", len(sessions), 1)
    info = sessions[0]
    check("listed id matches", info.id, sid)
    check("message count", info.message_count, 4)
    check("preview shows first user prompt", info.preview, "first prompt line")
    check("load round-trips the conversation",
          logger.load(sid), [user, assistant, tool, final])

    # --- preview truncation --------------------------------------------------

    logger.rotate()
    long_prompt = "word " * 30
    logger.record({"role": "user", "content": long_prompt})
    info = logger.list_sessions()[0]
    check("long preview truncated",
          (len(info.preview) <= 72, info.preview.endswith("...")), (True, True))

    # --- rotate ---------------------------------------------------------------

    logger.rotate()
    check("rotate clears the active id", logger.active_id, None)
    logger.record({"role": "user", "content": "second session prompt"})
    sid2 = logger.active_id
    check("rotate starts a new file", sid2 != sid, True)
    check("three sessions listed", len(logger.list_sessions()), 3)

    # --- attach (resume continuation) ------------------------------------------

    continuation = SessionLogger(ws, state_dir=state)
    continuation.attach(sid)
    check("attach sets the active id", continuation.active_id, sid)
    continuation.record({"role": "user", "content": "continue here"})
    check("appended to the original file",
          read_lines(logger.session_path(sid))[-1]["message"]["content"],
          "continue here")
    counts = {s.id: s.message_count for s in continuation.list_sessions()}
    check("attached session count grew", counts[sid], 5)

    # --- invalid ids ------------------------------------------------------------

    for bad in ("../evil", "a/b", "no-such-session", "."):
        try:
            continuation.load(bad)
            check(f"load({bad!r}) raises", "no error", "SessionError")
        except SessionError:
            check(f"load({bad!r}) raises", "SessionError", "SessionError")
    try:
        continuation.attach("missing-too")
        check("attach unknown id raises", "no error", "SessionError")
    except SessionError:
        check("attach unknown id raises", "SessionError", "SessionError")

    # --- meta-only files are not resumable ---------------------------------------

    meta_only = logger.sessions_dir / "meta-only.jsonl"
    meta_only.write_text(
        json.dumps({"type": "meta", "version": 1}) + "\n", encoding="utf-8")
    ids = [s.id for s in logger.list_sessions()]
    check("meta-only file not listed", "meta-only" in ids, False)

    # --- corrupt line is skipped, not fatal ----------------------------------------

    corrupt_target = logger.sessions_dir / "corrupt.jsonl"
    corrupt_target.write_text(
        json.dumps({"type": "meta", "version": 1}) + "\n"
        + '{"type": "message", "message": {"role": "user", "content": "ok"}}\n'
        + '{"broken json line\n',
        encoding="utf-8")
    corrupt_logger = SessionLogger(ws, state_dir=state)
    ids = [s.id for s in corrupt_logger.list_sessions()]
    check("corrupt file still listed", "corrupt" in ids, True)
    check("corrupt line skipped on load",
          corrupt_logger.load("corrupt"),
          [{"role": "user", "content": "ok"}])

    # --- repair_messages ---------------------------------------------------------------

    def msg(role, **kw):
        out = {"role": role}
        out.update(kw)
        return out

    u1 = msg("user", content="u1")
    u2 = msg("user", content="u2")
    a_text = msg("assistant", content="done")
    t_c1 = msg("tool", tool_call_id="c1", name="f", content="r1")
    tc_c1 = [{"id": "c1", "type": "function",
              "function": {"name": "f", "arguments": "{}"}}]
    a_c1 = msg("assistant", content=None, tool_calls=[dict(tc_c1[0])])
    a_c1c2 = msg("assistant", content=None, tool_calls=[
        dict(tc_c1[0]),
        {"id": "c2", "type": "function",
         "function": {"name": "g", "arguments": "{}"}},
    ])

    check("complete sequence kept",
          repair_messages([u1, a_c1, t_c1, a_text]), [u1, a_c1, t_c1, a_text])
    check("trailing dangling assistant dropped",
          repair_messages([u1, a_c1]), [u1])
    check("partial tool results dropped mid-file",
          repair_messages([u1, a_c1c2, t_c1, u2, a_text]), [u1, u2, a_text])
    check("orphan tool result dropped",
          repair_messages([t_c1, u1]), [u1])
    check("system message dropped",
          repair_messages([msg("system", content="s"), u1]), [u1])

    # --- unwritable state dir disables logging instead of raising -----------------------

    blocker = Path(tempfile.mkdtemp(prefix="ma_sess_block_")) / "blocker"
    blocker.write_text("x", encoding="utf-8")
    tmp_roots.append(blocker.parent)
    broken = SessionLogger(ws, state_dir=str(blocker))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        broken.record({"role": "user", "content": "should not raise"})
    check("record survives unwritable state dir", broken.active_id, None)
    check("failure warning printed once",
          "Session logging disabled" in output.getvalue(), True)

    # --- Agent records its turns --------------------------------------------------------

    ws2, state2, agent_logger = fresh_logger("agent")
    tmp_roots += [ws2, Path(state2)]
    provider = FakeProvider([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call1", "type": "function",
                         "function": {"name": "list_files",
                                      "arguments": "{}"}}]},
        {"role": "assistant", "content": "I listed the files."},
    ])
    agent = Agent(provider, FakeTools(), "sysprompt", recorder=agent_logger)
    # Turns are driven headlessly: the agent is a generator of events now, so
    # drive() pumps it and the renderer records what it emitted.  Nothing is
    # printed, so no stdout redirection is needed here.
    turn_events = Headless()
    turn_text = drive(agent, "list the files please", turn_events)
    check("turn returned the final reply", turn_text, "I listed the files.")
    check("turn emitted the tool lifecycle",
          [type(e).__name__ for e in turn_events.events],
          ["ToolStarted", "ToolCompleted", "AssistantText", "TurnEnded"])

    check("agent history roles",
          [m["role"] for m in agent.messages],
          ["system", "user", "assistant", "tool", "assistant"])
    agent_sid = agent_logger.active_id
    recorded = read_lines(agent_logger.session_path(agent_sid))
    check("agent turn recorded as lines",
          [o["type"] for o in recorded],
          ["meta", "message", "message", "message", "message"])
    check("system prompt never recorded",
          any(o.get("type") == "message"
              and o.get("message", {}).get("role") == "system"
              for o in recorded), False)

    agent.reset()
    check("reset rotates the recorder", agent_logger.active_id, None)
    agent.provider = FakeProvider([{"role": "assistant", "content": "fresh"}])
    check("post-reset turn returns its reply",
          drive(agent, "new conversation", Headless()), "fresh")
    check("post-reset turn starts a new file",
          agent_logger.active_id != agent_sid, True)
    check("both segments listed", len(agent_logger.list_sessions()), 2)

    # --- restore ---------------------------------------------------------------

    agent.restore([{"role": "system", "content": "OLD PROMPT"},
                   {"role": "user", "content": "restored"}])
    check("restore keeps the current system prompt",
          agent.messages[0], {"role": "system", "content": "sysprompt"})
    check("restore reinstates the history",
          [m["role"] for m in agent.messages], ["system", "user"])

    # --- :resume picker (app wiring), driven with scripted input -----------------

    ws3, state3, rec = fresh_logger("page")
    tmp_roots += [ws3, Path(state3)]
    ids = []
    for i in range(6):
        rec.record({"role": "user", "content": f"prompt {i}"})
        ids.append(rec.active_id)
        rec.rotate()
    for i, session_id in enumerate(ids):  # deterministic, increasing mtimes
        os.utime(rec.session_path(session_id), (1000 + i, 1000 + i))
    expected_order = list(reversed(ids))  # most recent activity first

    pager_logger = SessionLogger(ws3, state_dir=state3)
    picker_agent = Agent(FakeProvider([]), FakeTools(), "sysprompt",
                         recorder=pager_logger)
    scripted = ScriptedInput(["5", "0", "2"])
    out = with_patched_input(
        scripted, lambda: ma_app._resume_command(picker_agent, pager_logger))

    check("picker consumed exactly three answers", len(scripted.prompts), 3)
    check("both pages were shown",
          ("page 1 of 2" in out, "page 2 of 2" in out), (True, True))
    picked = expected_order[1]
    check("second entry of page one resumed", pager_logger.active_id, picked)
    check("restored history behind fresh system prompt",
          picker_agent.messages,
          [{"role": "system", "content": "sysprompt"},
           {"role": "user", "content": "prompt 4"}])
    before = len(read_lines(pager_logger.session_path(picked)))
    pager_logger.record({"role": "user", "content": "post-resume"})
    check("new messages append to the resumed file",
          len(read_lines(pager_logger.session_path(picked))), before + 1)

    # blank answer cancels
    cancel_logger = SessionLogger(ws3, state_dir=state3)
    cancel_agent = Agent(FakeProvider([]), FakeTools(), "sysprompt")
    scripted_cancel = ScriptedInput([""])
    with_patched_input(
        scripted_cancel,
        lambda: ma_app._resume_command(cancel_agent, cancel_logger))
    check("blank input cancels",
          cancel_agent.messages,
          [{"role": "system", "content": "sysprompt"}])

    # EOF also cancels
    eof_agent = Agent(FakeProvider([]), FakeTools(), "sysprompt")
    with_patched_input(EOFInput(),
                       lambda: ma_app._resume_command(eof_agent, pager_logger))
    check("EOF cancels the picker", len(eof_agent.messages), 1)

    # invalid inputs re-prompt until a valid choice arrives
    retry_logger = SessionLogger(ws3, state_dir=state3)
    retry_agent = Agent(FakeProvider([]), FakeTools(), "sysprompt",
                        recorder=retry_logger)
    scripted_retry = ScriptedInput(["x", "9", "0", "3"])
    out = with_patched_input(
        scripted_retry,
        lambda: ma_app._resume_command(retry_agent, retry_logger))
    check("invalid entries re-prompt", len(scripted_retry.prompts), 4)
    check("picked third entry after retries",
          retry_logger.active_id, expected_order[2])

    # empty workspace has nothing to resume
    ws4, state4, empty_logger = fresh_logger("empty")
    tmp_roots += [ws4, Path(state4)]
    empty_agent = Agent(FakeProvider([]), FakeTools(), "sysprompt")
    out = with_patched_input(
        ScriptedInput([]),
        lambda: ma_app._resume_command(empty_agent, empty_logger))
    check("no-sessions message shown",
          "No recorded sessions" in out, True)

finally:
    for root in tmp_roots:
        shutil.rmtree(root, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} session self-test check(s) failed")
print("All checks passed.")
