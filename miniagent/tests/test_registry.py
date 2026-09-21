"""Consistency checks over the tool registry (tools.py wiring).

These are the cheapest tests in the suite: no network, no writes, no
temporary directories, nothing that touches the filesystem beyond importing
the package.  They assert that the three hand-maintained tables in
``tools.py`` agree with each other and with the wire format:

* every ``TOOL_SCHEMAS`` name is unique, and reaches a real branch of
  ``Tools._execute`` rather than falling through to "Unknown tool";
* every ``CAPABILITY_MAP`` key is a declared tool and every value is a real
  capability in ``permissions.CAPABILITIES``;
* every schema has the ``type``/``function``/``name``/``description``/
  ``parameters`` shape an OpenAI-compatible server expects, and the whole
  list is JSON-serialisable.

Reachability is checked by dispatching each name and asserting the result is
not the dispatcher's ``{"error": "Unknown tool: ..."}`` fallback.  The
collaborators are stubs, so most tools come back with an error — that is
fine here; ``test_tools.py`` is the one that runs them for real.  Lives
permanently in miniagent/tests/; run it directly or through run_all.py.
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

from miniagent import permissions as perms  # noqa: E402
from miniagent.events import PermissionNeeded  # noqa: E402
from miniagent.knowledge import KnowledgeError  # noqa: E402
from miniagent.tools import CAPABILITY_MAP, TOOL_SCHEMAS, Tools  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


# --- stub collaborators (no I/O) -----------------------------------------


class AllowAllPermissions:
    """Policy stub that authorises everything without asking.

    Gating happens before ``_execute``, so a reachability probe has to get
    past it; with this stub no ``PermissionNeeded`` is ever raised and every
    dispatch lands in the implementation table.
    """

    def decide(self, capability):
        return True

    def apply_answer(self, capability, answer):  # pragma: no cover - unused
        return False, ""


class StubKnowledge:
    """Stands in for Knowledge so nothing stats the real knowledge dir.

    Every action raises the collaborator's own error type, which ``dispatch``
    turns into a clean ``{"error": ...}`` result — the same shape a real
    unavailable knowledge directory produces.
    """

    root = "(stub)"

    def _fail(self, *args, **kwargs):
        raise KnowledgeError("stub knowledge base")

    list = read = search = docs = _fail


def dispatch_result(tools, name, args=None):
    """Run one dispatch to completion and return the parsed result payload."""
    call = {
        "id": f"call_{name}",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }
    gen = tools.dispatch(call)
    to_send = None
    while True:
        try:
            event = gen.send(to_send)
        except StopIteration as stop:
            return json.loads(stop.value)
        if isinstance(event, PermissionNeeded):  # pragma: no cover - stubbed
            failures.append(f"unexpected permission prompt for {name}")
        to_send = None


tools = Tools(None, AllowAllPermissions(), None, knowledge=StubKnowledge())

names = [schema["function"]["name"] for schema in TOOL_SCHEMAS]


# --- schema names ---------------------------------------------------------

check("tool names are unique", len(names), len(set(names)))
check("every tool name is a non-empty string",
      [n for n in names if not isinstance(n, str) or not n.strip()], [])
check("registry is not empty", len(names) > 0, True)


# --- schema shape ---------------------------------------------------------

for schema in TOOL_SCHEMAS:
    label = schema.get("function", {}).get("name", repr(schema))
    check(f"{label}: top level is a function tool", schema.get("type"),
          "function")
    check(f"{label}: top-level keys", sorted(schema), ["function", "type"])
    function = schema.get("function")
    check(f"{label}: function is a dict", isinstance(function, dict), True)
    if not isinstance(function, dict):
        continue
    check(f"{label}: has name/description/parameters",
          sorted(k for k in ("name", "description", "parameters")
                 if k in function),
          ["description", "name", "parameters"])
    check(f"{label}: description is non-empty text",
          isinstance(function.get("description"), str)
          and bool(function["description"].strip()), True)
    params = function.get("parameters")
    check(f"{label}: parameters is an object schema",
          isinstance(params, dict) and params.get("type") == "object", True)
    if isinstance(params, dict):
        check(f"{label}: properties is a dict",
              isinstance(params.get("properties", {}), dict), True)
        required = params.get("required", [])
        check(f"{label}: required is a list of property names",
              isinstance(required, list)
              and all(r in params.get("properties", {}) for r in required),
              True)

try:
    json.dumps(TOOL_SCHEMAS)
    check("schemas are JSON-serialisable", True, True)
except (TypeError, ValueError) as exc:  # pragma: no cover - would be a bug
    check("schemas are JSON-serialisable", f"raised {exc}", True)


# --- capability map -------------------------------------------------------

check("every CAPABILITY_MAP key is a declared tool",
      sorted(k for k in CAPABILITY_MAP if k not in names), [])
check("every CAPABILITY_MAP value is a real capability",
      sorted(v for v in CAPABILITY_MAP.values()
             if v not in perms.CAPABILITIES), [])
check("CAPABILITY_MAP values are the capability constants",
      sorted(set(CAPABILITY_MAP.values())),
      sorted({perms.ASK_IMAGE, perms.EDIT, perms.OVERWRITE,
              perms.RUN_PYTHON, perms.WRITE}))
check("permissions.CAPABILITIES has no duplicates",
      len(perms.CAPABILITIES), len(set(perms.CAPABILITIES)))
# decide() refuses anything outside CAPABILITIES, so a capability that is
# mapped but not declared would silently deny every gated call.
check("no capability is mapped but undeclared",
      sorted(set(CAPABILITY_MAP.values()) - set(perms.CAPABILITIES)), [])


# --- implementation reachability -----------------------------------------

def unknown_tool_error(payload):
    """True when *payload* is the dispatcher's unknown-tool fallback."""
    error = payload.get("error")
    return isinstance(error, str) and error.startswith("Unknown tool:")


for name in names:
    payload = dispatch_result(tools, name)
    check(f"{name}: reaches an implementation",
          unknown_tool_error(payload), False)

# Positive control: the check above only means something if an undeclared
# name really does produce the fallback.
check("an undeclared name falls through to Unknown tool",
      dispatch_result(tools, "no_such_tool_xyz").get("error"),
      "Unknown tool: no_such_tool_xyz")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} registry check(s) failed")
print("All checks passed.")
