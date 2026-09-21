"""Tests for the agent's event-stream contract (agent.py).

Safe to run via run_python: no network, no file writes, no console loop.  A
fake provider replays canned responses, a fake tools dispatcher stands in for
the real one, and turns are driven through the headless renderer — so this
file asserts on typed events rather than on printed text.

What it pins down:

* the exact event sequence for a multi-step turn (tool call, then reply);
* the protocol's central promise — **exactly one** terminal event per turn —
  across a normal reply, step-limit exhaustion and a provider failure;
* a ProviderError becomes TurnFailed and still leaves a balanced history
  whose last message is the synthetic assistant one (§1.6);
* StepLimitReached is informational and is followed by TurnEnded;
* TurnEnded.usage sums tokens across every model call in the turn, while
  session_usage accumulates across turns (§1.7);
* the agent prints **nothing** — the whole point of the refactor;
* the §1.4 repair of an interrupted turn: a dangling assistant tool_calls
  message is healed away and the system prompt survives.

Lives permanently in miniagent/tests/; run it directly or through
run_all.py.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these new modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent.agent import Agent  # noqa: E402
from miniagent.events import (  # noqa: E402
    AssistantText,
    Event,
    PermissionAnswer,
    PermissionNeeded,
    ReasoningChunk,
    StepLimitReached,
    ToolCompleted,
    ToolStarted,
    TurnEnded,
    TurnFailed,
)
from miniagent.provider import ProviderError  # noqa: E402
from miniagent.sessions import repair_messages  # noqa: E402
from miniagent.ui import drive  # noqa: E402
from miniagent.ui.headless import Headless  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


# --- doubles ---------------------------------------------------------------


class FakeProvider:
    """Replays canned assistant messages, one per chat() call.

    An entry that is an exception instance is raised instead of returned, so
    a scenario can script a provider failure at a chosen step.  A ``usage``
    dict may be attached per response through *usages*.
    """

    def __init__(self, messages, usages=None, repeat_last=False):
        self._messages = list(messages)
        self._usages = list(usages or [])
        self.repeat_last = repeat_last
        self.calls = 0
        self.seen = []  # the message list handed to each call

    def chat(self, messages, tools=None, tool_choice=None):
        self.seen.append([dict(m) for m in messages])
        index = self.calls
        if index >= len(self._messages):
            if not self.repeat_last:
                raise AssertionError(
                    f"FakeProvider ran out of responses at call {index + 1}")
            index = len(self._messages) - 1
        self.calls += 1
        message = self._messages[index]
        if isinstance(message, Exception):
            raise message
        response = {"choices": [{"message": message}]}
        if index < len(self._usages) and self._usages[index] is not None:
            response["usage"] = self._usages[index]
        return response


class FakeTools:
    """Dispatcher double implementing the generator contract of Tools.

    Yields the tool-call lifecycle events and *returns* the JSON string the
    model receives, exactly as ``Tools.dispatch`` does, so the agent's
    ``result = yield from tools.dispatch(call)`` is exercised for real.  When
    *gate* is set, the named tool also raises a ``PermissionNeeded`` first
    and records the answer it is sent, which is how the agent's pass-through
    of the interactive event gets covered here.
    """

    schemas = []

    def __init__(self, gate=None):
        self.calls = []
        self.answers = []
        self.gate = gate

    def dispatch(self, call):
        self.calls.append(call)
        name = call.get("function", {}).get("name", "")
        yield ToolStarted(name, {})
        if self.gate is not None and name == self.gate:
            answer = yield PermissionNeeded(self.gate, "GATED", "details")
            self.answers.append(answer)
            if not isinstance(answer, PermissionAnswer):
                outcome = {"ok": False, "denied": True}
                yield ToolCompleted(name, "denied", "", outcome)
                return json.dumps(outcome)
        outcome = {"ok": True, "tool": name}
        yield ToolCompleted(name, "ok", "", outcome)
        return json.dumps(outcome)


def tool_call(call_id, name="list_files", arguments="{}"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def calls_then_reply(*ids, reply="all done"):
    """Canned responses: one tool-calling step per id, then a text reply."""
    return [{"role": "assistant", "content": None,
             "tool_calls": [tool_call(i)]} for i in ids] + [
        {"role": "assistant", "content": reply}]


def types_of(headless):
    return [type(event).__name__ for event in headless.events]


def terminals(headless):
    return [e for e in headless.events if isinstance(e, (TurnEnded, TurnFailed))]


def run(agent, text="go", answers=None):
    """Drive one turn headlessly and return the renderer that recorded it."""
    seen = Headless(answers)
    seen.result = drive(agent, text, seen)
    return seen


# --- 1. the exact sequence of a multi-step turn ---------------------------

provider = FakeProvider(calls_then_reply("c1", "c2", reply="finished"))
tools = FakeTools()
agent = Agent(provider, tools, "sysprompt")
seen = run(agent)

check("multi-step event sequence", types_of(seen),
      ["ToolStarted", "ToolCompleted",
       "ToolStarted", "ToolCompleted",
       "AssistantText", "TurnEnded"])
check("one dispatch per tool call", len(tools.calls), 2)
check("provider called once per step", provider.calls, 3)
check("drive returns the final reply", seen.result, "finished")
check("TurnEnded carries the final text",
      seen.events_of(TurnEnded)[0].text, "finished")
check("the reply arrives as AssistantText before TurnEnded",
      [e.text for e in seen.events_of(AssistantText)], ["finished"])
check("history roles for a two-tool turn",
      [m["role"] for m in agent.messages],
      ["system", "user", "assistant", "tool", "assistant", "tool",
       "assistant"])
check("tool results are recorded against their call ids",
      [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"],
      ["c1", "c2"])
check("tool result content is the dispatcher's return value",
      json.loads(agent.messages[3]["content"]),
      {"ok": True, "tool": "list_files"})
check("every yielded object is an Event",
      all(isinstance(e, Event) for e in seen.events), True)

# A step that reasons and talks before calling a tool emits both events
# ahead of the tool pair.
provider = FakeProvider([
    {"role": "assistant", "content": "working on it",
     "reasoning_content": "let me look", "tool_calls": [tool_call("c1")]},
    {"role": "assistant", "content": "done"},
])
agent = Agent(provider, FakeTools(), "sysprompt")
seen = run(agent)
check("reasoning and text precede the tool events", types_of(seen),
      ["ReasoningChunk", "AssistantText", "ToolStarted", "ToolCompleted",
       "AssistantText", "TurnEnded"])
check("reasoning text passes through unmodified",
      seen.events_of(ReasoningChunk)[0].text, "let me look")

# PermissionNeeded raised inside dispatch reaches the driver, and the answer
# the driver sends is delivered back into the suspended tool call.
gated = FakeTools(gate="list_files")
agent = Agent(FakeProvider(calls_then_reply("c1")), gated, "sysprompt")
seen = run(agent, answers=["y"])
check("permission event reaches the driver", types_of(seen),
      ["ToolStarted", "PermissionNeeded", "ToolCompleted", "AssistantText",
       "TurnEnded"])
check("the answer is delivered back into dispatch",
      [a.decision for a in gated.answers], ["once_allow"])
check("scripted answer consumed", seen.answers_left, 0)

# With no scripted answer the driver sends None, which dispatch treats as a
# deny — the safe outcome when nobody is watching.
gated = FakeTools(gate="list_files")
agent = Agent(FakeProvider(calls_then_reply("c1")), gated, "sysprompt")
seen = run(agent)
check("an unanswered prompt denies", gated.answers, [None])
check("denied tool completes as denied",
      seen.events_of(ToolCompleted)[0].status, "denied")
check("a denied tool still ends the turn normally",
      len(terminals(seen)), 1)


# --- 2. exactly one terminal event, in every scenario ---------------------

scenarios = {}

# normal reply, no tools
agent = Agent(FakeProvider([{"role": "assistant", "content": "hello"}]),
              FakeTools(), "sysprompt")
scenarios["plain reply"] = run(agent)

# step-limit exhaustion: the provider never stops calling tools
agent = Agent(FakeProvider([{"role": "assistant", "content": None,
                             "tool_calls": [tool_call("loop")]}],
                           repeat_last=True),
              FakeTools(), "sysprompt", max_steps=3)
scenarios["step limit"] = run(agent)
step_limit_seen = scenarios["step limit"]

# provider failure on the very first call
agent = Agent(FakeProvider([ProviderError("HTTP 500 from nowhere")]),
              FakeTools(), "sysprompt")
scenarios["provider failure"] = run(agent)
failure_seen = scenarios["provider failure"]

# provider failure partway through a tool loop
mid_provider = FakeProvider(
    [{"role": "assistant", "content": None, "tool_calls": [tool_call("c1")]},
     ProviderError("Request failed: timeout")])
mid_agent = Agent(mid_provider, FakeTools(), "sysprompt")
scenarios["failure mid-loop"] = run(mid_agent)

for label, recorded in scenarios.items():
    check(f"{label}: exactly one terminal event", len(terminals(recorded)), 1)
    check(f"{label}: the terminal event is last",
          isinstance(recorded.events[-1], (TurnEnded, TurnFailed)), True)
    check(f"{label}: drive returns the terminal text",
          recorded.result, recorded.text)


# --- 3. a provider error fails the turn and leaves a balanced history -----

check("provider failure yields TurnFailed",
      types_of(failure_seen), ["TurnFailed"])
check("no TurnEnded alongside TurnFailed",
      failure_seen.events_of(TurnEnded), [])
failed = failure_seen.events_of(TurnFailed)[0]
check("TurnFailed carries the provider error",
      failed.error, "HTTP 500 from nowhere")
check("TurnFailed carries the synthetic reply text",
      failed.text, "[provider error: HTTP 500 from nowhere]")

history = [m for m in mid_agent.messages if m["role"] != "system"]
check("mid-loop failure: last message is the synthetic assistant one",
      history[-1],
      {"role": "assistant",
       "content": "[provider error: Request failed: timeout]"})
check("mid-loop failure: history roles are balanced",
      [m["role"] for m in history],
      ["user", "assistant", "tool", "assistant"])
# repair_messages is what the next turn would run over this history; a
# balanced history must survive it untouched.
check("mid-loop failure: history needs no repair",
      repair_messages(history), history)
check("mid-loop failure: system prompt intact",
      mid_agent.messages[0], {"role": "system", "content": "sysprompt"})

# The synthetic reply is the conversation's last assistant text, so the
# session log and any resume see a complete turn.
check("synthetic reply is visible as the last assistant text",
      mid_agent.last_assistant_text(),
      "[provider error: Request failed: timeout]")


# --- 4. the step limit is informational, TurnEnded still follows ----------

check("step-limit event sequence", types_of(step_limit_seen),
      ["ToolStarted", "ToolCompleted"] * 3 + ["StepLimitReached", "TurnEnded"])
check("StepLimitReached names the limit",
      step_limit_seen.events_of(StepLimitReached)[0].limit, 3)
check("StepLimitReached is immediately followed by TurnEnded",
      isinstance(step_limit_seen.events[-1], TurnEnded), True)
check("step limit ends with the stop marker",
      step_limit_seen.text, "[maximum agent steps reached; stopping]")


# --- 5. usage accounting --------------------------------------------------

usage_provider = FakeProvider(
    calls_then_reply("c1", "c2", reply="done"),
    usages=[
        {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37},
    ],
)
usage_agent = Agent(usage_provider, FakeTools(), "sysprompt")
seen = run(usage_agent)
check("TurnEnded.usage sums every model call in the turn",
      seen.events_of(TurnEnded)[0].usage,
      {"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75})
check("last_usage is the final response's own usage",
      usage_agent.last_usage,
      {"prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37})
check("session_usage matches after one turn",
      usage_agent.session_usage,
      {"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75})

usage_agent.provider = FakeProvider(
    [{"role": "assistant", "content": "again"}],
    usages=[{"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}],
)
seen = run(usage_agent, "second turn")
check("a second turn's usage is only its own",
      seen.events_of(TurnEnded)[0].usage,
      {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
check("session_usage accumulates across turns",
      usage_agent.session_usage,
      {"prompt_tokens": 61, "completion_tokens": 17, "total_tokens": 78})

# A provider that reports no usage must not break the accounting, and
# non-numeric fields are not summed.
quiet_agent = Agent(
    FakeProvider([{"role": "assistant", "content": "quiet"}],
                 usages=[{"model": "m", "cached": True, "total_tokens": 4}]),
    FakeTools(), "sysprompt")
seen = run(quiet_agent)
check("non-numeric usage fields are ignored",
      seen.events_of(TurnEnded)[0].usage, {"total_tokens": 4})
none_agent = Agent(FakeProvider([{"role": "assistant", "content": "x"}]),
                   FakeTools(), "sysprompt")
seen = run(none_agent)
check("a turn with no reported usage ends with an empty usage dict",
      seen.events_of(TurnEnded)[0].usage, {})


# --- 6. the agent prints nothing -------------------------------------------

# The core guarantee of the event-stream refactor: everything the engine used
# to print is an event now, so a turn driven by a silent renderer must leave
# stdout completely untouched.
quiet_provider = FakeProvider([
    {"role": "assistant", "content": "narration",
     "reasoning_content": "thinking out loud",
     "tool_calls": [tool_call("c1")]},
    {"role": "assistant", "content": "spoken reply"},
])
quiet_tools = FakeTools(gate="list_files")
quiet = Agent(quiet_provider, quiet_tools, "sysprompt", max_steps=5)
buffer = io.StringIO()
with redirect_stdout(buffer):
    quiet_text = drive(quiet, "say something", Headless(["y"]))
check("a whole turn prints nothing", buffer.getvalue(), "")
check("...and still produced its reply", quiet_text, "spoken reply")

# Same for the failure path and the step-limit path, which used to print
# their own banners from inside the loop.
buffer = io.StringIO()
with redirect_stdout(buffer):
    drive(Agent(FakeProvider([ProviderError("boom")]), FakeTools(),
                "sysprompt"), "fail", Headless())
check("a failed turn prints nothing", buffer.getvalue(), "")

buffer = io.StringIO()
with redirect_stdout(buffer):
    drive(Agent(FakeProvider([{"role": "assistant", "content": None,
                               "tool_calls": [tool_call("loop")]}],
                             repeat_last=True),
                FakeTools(), "sysprompt", max_steps=2),
          "spin", Headless())
check("an exhausted turn prints nothing", buffer.getvalue(), "")


# --- 7. §1.4 — an interrupted turn is repaired before the next one ---------

# What a KeyboardInterrupt mid-tool-loop leaves behind: an assistant message
# announcing tool calls whose results never arrived.  Most providers reject
# that with a 400, so the agent heals the tail before adding to it.
dangling = {"role": "assistant", "content": None,
            "tool_calls": [tool_call("orphan"), tool_call("orphan2")]}
interrupted = Agent(FakeProvider([{"role": "assistant", "content": "recovered"}]),
                    FakeTools(), "sysprompt")
interrupted.messages = [
    {"role": "system", "content": "sysprompt"},
    {"role": "user", "content": "earlier question"},
    {"role": "assistant", "content": "earlier answer"},
    {"role": "user", "content": "interrupted question"},
    dangling,
]
seen = run(interrupted, "next question")

check("interrupted turn completes normally", seen.result, "recovered")
check("the dangling tool_calls message was healed away",
      any(m.get("tool_calls") for m in interrupted.messages), False)
check("history after repair",
      [(m["role"], m.get("content")) for m in interrupted.messages],
      [("system", "sysprompt"),
       ("user", "earlier question"),
       ("assistant", "earlier answer"),
       ("user", "interrupted question"),
       ("user", "next question"),
       ("assistant", "recovered")])
check("the system prompt survived the repair",
      interrupted.messages[0], {"role": "system", "content": "sysprompt"})
check("the repaired history is what the provider actually saw",
      [m["role"] for m in interrupted.provider.seen[0]],
      ["system", "user", "assistant", "user", "user"])

# An orphaned tool result (the mirror-image corruption) is dropped too, and a
# history that is already balanced is left exactly as it was.
orphaned = Agent(FakeProvider([{"role": "assistant", "content": "ok"}]),
                 FakeTools(), "sysprompt")
orphaned.messages = [
    {"role": "system", "content": "sysprompt"},
    {"role": "tool", "tool_call_id": "gone", "name": "f", "content": "{}"},
    {"role": "user", "content": "still here"},
]
run(orphaned, "carry on")
check("an orphan tool result is dropped",
      any(m["role"] == "tool" for m in orphaned.messages), False)

intact = Agent(FakeProvider([{"role": "assistant", "content": "ok"}]),
               FakeTools(), "sysprompt")
before = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": None, "tool_calls": [tool_call("c1")]},
    {"role": "tool", "tool_call_id": "c1", "name": "list_files",
     "content": "{}"},
    {"role": "assistant", "content": "a"},
]
intact.messages = [{"role": "system", "content": "sysprompt"}] + [
    dict(m) for m in before]
run(intact, "more")
check("a balanced history is left untouched",
      intact.messages[1:5], before)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} agent check(s) failed")
print("All checks passed.")
