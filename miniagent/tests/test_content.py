"""Tests for Provider.split_content and agent display handling.

Safe to run via run_python: no network, no file writes, no console loop.
It re-imports the miniagent package fresh so it exercises the CURRENT source
files, then simulates agent turns with a fake provider. Lives permanently in
miniagent/tests/; run it directly or through run_all.py.
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
from miniagent.provider import Provider  # noqa: E402

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
    schemas = []
    dispatched = []

    def dispatch(self, call):
        self.dispatched.append(call)
        return json.dumps({"ok": True})


def run_turn(message):
    provider = FakeProvider(message if isinstance(message, list) else [message])
    agent = Agent(provider, FakeTools(), "system prompt")
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = agent.turn("hi")
    return agent, buf.getvalue(), result


# Scenario 1: the bug — content is a list with only a thinking block.
agent, out, result = run_turn({"role": "assistant", "content": bug_shape})
check("S1 reasoning printed as text",
      "· The user sent" in out, True)
check("S1 no raw json dump", "'type': 'thinking'" not in out, True)
check("S1 no Assistant header (no text part)", "Assistant:" not in out, True)
check("S1 turn returns placeholder", result, "(no response)")
check("S1 history keeps raw content",
      agent.messages[-1]["content"], bug_shape)
check("S1 last_assistant_text is str", agent.last_assistant_text(), "")

# Scenario 2: thinking block + text block in the same content list.
agent, out, result = run_turn({"role": "assistant", "content": mixed_shape})
check("S2 reasoning printed", "· The user sent" in out, True)
check("S2 assistant text printed",
      "Here is the overview." in out, True)
check("S2 turn returns text part", result, "Here is the overview.")
check("S2 last_assistant_text", agent.last_assistant_text(),
      "Here is the overview.")
check("S2 no raw json dump", "'type': 'thinking'" not in out, True)

# Scenario 3: classic DeepSeek-style reasoning_content field still works.
agent, out, result = run_turn(
    {"role": "assistant", "content": "Answer!",
     "reasoning_content": "chain of thought"}
)
check("S3 reasoning_content printed",
      "· chain of thought" in out, True)
check("S3 text printed", "Answer!" in out, True)
check("S3 turn returns text", result, "Answer!")

# Scenario 4: tool call whose message also carries block content — the
# history must round-trip the original blocks and the loop must continue.
tool_call = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "read_file", "arguments": '{"path": "x"}'},
}
agent, out, result = run_turn(
    [
        {"role": "assistant", "content": mixed_shape,
         "tool_calls": [tool_call]},
        {"role": "assistant", "content": "done after tool"},
    ]
)
check("S4 tool dispatched", len(FakeTools.dispatched), 1)
assistant_msgs = [m for m in agent.messages if m["role"] == "assistant"]
check("S4 first assistant kept raw blocks",
      assistant_msgs[0]["content"], mixed_shape)
check("S4 final text returned", result, "done after tool")
check("S4 no raw json dump", "'type': 'thinking'" not in out, True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} content test check(s) failed")
print("All checks passed.")
