"""Per-session tool limits: ``run(tools=..., exclude_tools=...)``.

Covers the three pieces that make a limit real:

* ``registry.resolve_enabled`` turns the launcher's arguments into a set of
  names, and refuses unknown names (a typo in ``exclude_tools`` must not
  silently leave the tool enabled);
* ``Tools(enabled=...)`` offers the model only the enabled schemas, and
  ``dispatch`` refuses a disabled tool before the argument gate, the
  permission gate or the implementation;
* ``build_system_prompt(..., enabled_tools)`` tells the model which tools it
  actually has.

No network, no writes, no console loop.  Lives permanently in
miniagent/tests/; run it directly or through run_all.py.
"""

import json
import sys
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

from miniagent.agent import build_system_prompt  # noqa: E402
from miniagent.events import PermissionNeeded, ToolCompleted  # noqa: E402
from miniagent.knowledge import KnowledgeError  # noqa: E402
from miniagent.tools import TOOL_SCHEMAS, Tools  # noqa: E402
from miniagent.tools.registry import resolve_enabled  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


ALL = [schema["function"]["name"] for schema in TOOL_SCHEMAS]


# --- resolve_enabled ------------------------------------------------------

check("no arguments means no limit", resolve_enabled(), None)
check("allowlist", resolve_enabled(["read_file", "list_files"]),
      frozenset({"read_file", "list_files"}))
check("a single name is accepted as a string", resolve_enabled("read_file"),
      frozenset({"read_file"}))
check("denylist alone removes from every tool",
      resolve_enabled(exclude_tools=["run_python"]),
      frozenset(ALL) - {"run_python"})
check("denylist applies after the allowlist",
      resolve_enabled(["read_file", "run_python"], ["run_python"]),
      frozenset({"read_file"}))
check("an empty allowlist means no tools", resolve_enabled([]), frozenset())


def raises_value_error(**kwargs):
    try:
        resolve_enabled(**kwargs)
    except ValueError as exc:
        return str(exc)
    return None


message = raises_value_error(tools=["read_file", "reed_file"])
check("unknown allowlist name raises",
      message is not None and "'reed_file'" in message, True)
check("the error lists the registered tools",
      message is not None and all(n in message for n in ALL), True)
check("unknown denylist name raises (a typo must not leave the tool on)",
      raises_value_error(exclude_tools=["run_pyton"]) is not None, True)


# --- Tools.schemas and dispatch ------------------------------------------


class RecordingPermissions:
    """Allows everything, and records every capability it is asked about."""

    def __init__(self):
        self.asked = []

    def decide(self, capability):
        self.asked.append(capability)
        return True

    def apply_answer(self, capability, answer):  # pragma: no cover - unused
        return True, ""


class StubKnowledge:
    root = "(stub)"

    def _fail(self, *args, **kwargs):
        raise KnowledgeError("stub knowledge base")

    list = read = search = docs = _fail


def run_dispatch(tools, name, args=None):
    """Return ``(events, payload)`` for one dispatch run to completion."""
    call = {"id": "c1",
            "function": {"name": name, "arguments": json.dumps(args or {})}}
    gen = tools.dispatch(call)
    events = []
    while True:
        try:
            events.append(gen.send(None))
        except StopIteration as stop:
            return events, json.loads(stop.value)


def schema_names(tools):
    return [schema["function"]["name"] for schema in tools.schemas]


unlimited = Tools(None, RecordingPermissions(), None, knowledge=StubKnowledge())
check("enabled=None offers every schema, in order",
      schema_names(unlimited), ALL)

perms = RecordingPermissions()
limited = Tools(None, perms, None, knowledge=StubKnowledge(),
                enabled=resolve_enabled(["knowledge", "read_file"]))
check("limited schemas keep registration order",
      schema_names(limited), [n for n in ALL if n in ("knowledge", "read_file")])
check("an empty set offers no schemas",
      Tools(None, perms, None, enabled=frozenset()).schemas, [])

# run_python is gated and needs `path`: a disabled call must be refused
# before either gate sees it.
events, payload = run_dispatch(limited, "run_python")
check("disabled tool is refused",
      payload, {"ok": False,
                "error": "tool 'run_python' is not available in this session"})
check("disabled tool never asks permission",
      (perms.asked, any(isinstance(e, PermissionNeeded) for e in events)),
      ([], False))
check("disabled tool completes with status error",
      [e.status for e in events if isinstance(e, ToolCompleted)], ["error"])

# An enabled tool still reaches its implementation (the stub's own error).
_, payload = run_dispatch(limited, "knowledge", {"action": "list"})
check("enabled tool reaches its implementation",
      payload.get("error"), "stub knowledge base")


# --- system prompt --------------------------------------------------------

marker = "Tools available in this session:"
check("no limit adds no tools line",
      marker in build_system_prompt("/p"), False)
prompt = build_system_prompt("/p", "", ["read_file", "list_files"])
check("a limit names the available tools",
      "Tools available in this session: read_file, list_files." in prompt, True)
check("an empty limit says there are no tools",
      "No tools are available in this session" in
      build_system_prompt("/p", "", []), True)
check("the tools line comes before JEB.md context",
      build_system_prompt("/p", "JEB-TEXT", ["read_file"]).index(marker)
      < build_system_prompt("/p", "JEB-TEXT", ["read_file"]).index("JEB-TEXT"),
      True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} tool-limit check(s) failed")
print("All checks passed.")
