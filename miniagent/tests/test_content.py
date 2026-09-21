"""Tests for Provider.split_content and the agent's content normalisation.

Safe to run via run_python: no network, no file writes, no console loop.
It re-imports the miniagent package fresh so it exercises the CURRENT source
files, then simulates agent turns with a fake provider. Lives permanently in
miniagent/tests/; run it directly or through run_all.py.

The turn scenarios below assert on the *events* the agent yields (through the
headless renderer), not on printed text: the point of each scenario is that a
particular content shape is normalised into the right reasoning/text split,
and ``ReasoningChunk.text`` states that directly instead of testing a
renderer's formatting by proxy.  The agent yields the full reasoning text —
collapsing long reasoning to one line is the console renderer's job — so
these compare against the complete string.
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
    ReasoningChunk,
    ToolCompleted,
    ToolStarted,
    TurnEnded,
)
from miniagent.provider import Provider  # noqa: E402
from miniagent.ui import drive  # noqa: E402
from miniagent.ui.headless import Headless  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


# --- split_content unit checks ------------------------------------------

check("string passthrough", Provider.split_content("hello"), ("hello", ""))
check("None", Provider.split_content(None), ("", ""))
check("empty list", Provider.split_content([]), ("", ""))
check("bare string part", Provider.split_content(["a", "b"]), ("a\nb", ""))

# The exact shape from the bug report: content is a list whose only block
# is a thinking block with nested text parts.
bug_shape = [
    {
        "type": "thinking",
        "thinking": [
            {
                "type": "text",
                "text": 'The user sent ":context". I should inspect the project.',
            }
        ],
        "closed": True,
    }
]
check(
    "thinking-only blocks (bug shape)",
    Provider.split_content(bug_shape),
    ("", 'The user sent ":context". I should inspect the project.'),
)

mixed_shape = bug_shape + [{"type": "text", "text": "Here is the overview."}]
check(
    "thinking + text blocks",
    Provider.split_content(mixed_shape),
    ("Here is the overview.",
     'The user sent ":context". I should inspect the project.'),
)

check(
    "anthropic-style string payload",
    Provider.split_content([{"type": "thinking", "thinking": "pondering"}]),
    ("", "pondering"),
)

check(
    "reasoning alias block",
    Provider.split_content([{"type": "reasoning", "reasoning": "hmm"}]),
    ("", "hmm"),
)

check(
    "unknown block types ignored",
    Provider.split_content(
        [
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "t"},
        ]
    ),
    ("t", ""),
)

check(
    "reasoning field as list of text parts",
    Provider.split_content([{"type": "text", "text": "chain"}]),
    ("chain", ""),
)


# --- agent turn simulation ----------------------------------------------

class FakeProvider:
    """Returns canned messages in order, shaped like provider.chat output."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice=None):
        message = self._messages[min(self.calls, len(self._messages) - 1)]
        self.calls += 1
        return {"choices": [{"message": message}]}


class FakeTools:
    """Minimal dispatcher matching the generator contract of Tools.dispatch.

    ``dispatch`` yields the tool lifecycle events and *returns* the JSON
    string for the model, so the agent's ``yield from`` works exactly as it
    does against the real dispatcher.
    """

    schemas = []
    dispatched = []

    def dispatch(self, call):
        self.dispatched.append(call)
        name = call.get("function", {}).get("name", "")
        yield ToolStarted(name, {})
        outcome = {"ok": True}
        yield ToolCompleted(name, "ok", "", outcome)
        return json.dumps(outcome)


# The full reasoning text the two block shapes above normalise to; the agent
# yields it unmodified.
THOUGHT = 'The user sent ":context". I should inspect the project.'


def run_turn(message):
    """Drive one turn headlessly; return (agent, headless renderer, result)."""
    provider = FakeProvider(message if isinstance(message, list) else [message])
    agent = Agent(provider, FakeTools(), "system prompt")
    headless = Headless()
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = drive(agent, "hi", headless)
    # The engine is silent by construction now; if anything printed, the
    # scenario below would be asserting on the wrong layer.
    check("engine printed nothing", buf.getvalue(), "")
    return agent, headless, result


def event_types(headless):
    return [type(e).__name__ for e in headless.events]


# Scenario 1: the bug — content is a list with only a thinking block.
agent, seen, result = run_turn({"role": "assistant", "content": bug_shape})
check("S1 event sequence", event_types(seen),
      ["ReasoningChunk", "TurnEnded"])
check("S1 reasoning is the normalised thinking text",
      seen.events_of(ReasoningChunk)[0].text, THOUGHT)
check("S1 no assistant text event (no text part)",
      seen.events_of(AssistantText), [])
check("S1 turn ends with placeholder",
      seen.events_of(TurnEnded)[0].text, "(no response)")
check("S1 drive returns placeholder", result, "(no response)")
check("S1 history keeps raw content",
      agent.messages[-1]["content"], bug_shape)
check("S1 last_assistant_text is str", agent.last_assistant_text(), "")

# Scenario 2: thinking block + text block in the same content list.
agent, seen, result = run_turn({"role": "assistant", "content": mixed_shape})
check("S2 event sequence", event_types(seen),
      ["ReasoningChunk", "AssistantText", "TurnEnded"])
check("S2 reasoning split out of the block list",
      seen.events_of(ReasoningChunk)[0].text, THOUGHT)
check("S2 assistant text split out of the block list",
      seen.events_of(AssistantText)[0].text, "Here is the overview.")
check("S2 turn ends with the text part",
      seen.events_of(TurnEnded)[0].text, "Here is the overview.")
check("S2 drive returns the text part", result, "Here is the overview.")
check("S2 last_assistant_text", agent.last_assistant_text(),
      "Here is the overview.")

# Scenario 3: classic DeepSeek-style reasoning_content field still works.
agent, seen, result = run_turn(
    {"role": "assistant", "content": "Answer!",
     "reasoning_content": "chain of thought"}
)
check("S3 event sequence", event_types(seen),
      ["ReasoningChunk", "AssistantText", "TurnEnded"])
check("S3 reasoning_content becomes a ReasoningChunk",
      seen.events_of(ReasoningChunk)[0].text, "chain of thought")
check("S3 assistant text event",
      seen.events_of(AssistantText)[0].text, "Answer!")
check("S3 drive returns text", result, "Answer!")

# Scenario 3b: reasoning arriving in the 'reasoning' alias field, and in
# both a field and a thinking block at once — the agent joins them.
agent, seen, result = run_turn(
    {"role": "assistant", "content": mixed_shape, "reasoning": "field part"}
)
check("S3b field and block reasoning joined",
      seen.events_of(ReasoningChunk)[0].text, "field part\n" + THOUGHT)

# Scenario 3c: reasoning is never truncated by the engine — the 200-char
# collapse belongs to the console renderer.
long_thought = "z" * 900
agent, seen, result = run_turn(
    {"role": "assistant", "content": "ok", "reasoning_content": long_thought}
)
check("S3c full reasoning text yielded",
      seen.events_of(ReasoningChunk)[0].text, long_thought)

# Scenario 4: tool call whose message also carries block content — the
# history must round-trip the original blocks and the loop must continue.
tool_call = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "read_file", "arguments": '{"path": "x"}'},
}
FakeTools.dispatched = []
agent, seen, result = run_turn(
    [
        {"role": "assistant", "content": mixed_shape,
         "tool_calls": [tool_call]},
        {"role": "assistant", "content": "done after tool"},
    ]
)
check("S4 tool dispatched", len(FakeTools.dispatched), 1)
check("S4 event sequence", event_types(seen),
      ["ReasoningChunk", "AssistantText", "ToolStarted", "ToolCompleted",
       "AssistantText", "TurnEnded"])
check("S4 block content split on the tool-calling step",
      (seen.events_of(ReasoningChunk)[0].text,
       seen.events_of(AssistantText)[0].text),
      (THOUGHT, "Here is the overview."))
check("S4 tool events name the tool",
      (seen.events_of(ToolStarted)[0].name,
       seen.events_of(ToolCompleted)[0].name,
       seen.events_of(ToolCompleted)[0].status),
      ("read_file", "read_file", "ok"))
assistant_msgs = [m for m in agent.messages if m["role"] == "assistant"]
check("S4 first assistant kept raw blocks",
      assistant_msgs[0]["content"], mixed_shape)
check("S4 final text returned", result, "done after tool")
check("S4 exactly one terminal event", len(seen.events_of(TurnEnded)), 1)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} content test check(s) failed")
print("All checks passed.")
