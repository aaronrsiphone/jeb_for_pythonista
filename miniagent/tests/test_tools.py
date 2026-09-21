"""Dispatch every declared tool for real — the §1.1 regression test.

Safe to run via run_python: no network, no console loop, and no writes
outside the system temp directory (the workspace, the knowledge directory
and the permission state dir are all throwaway temp trees).

The `knowledge` tool shipped broken because an instance attribute shadowed
the method that implemented it: every call raised TypeError, was swallowed by
the defensive ``except Exception`` in ``Tools.dispatch``, and came back to the
model as ``"Unexpected error in 'knowledge'"``.  Nothing noticed, because no
test dispatched every name in TOOL_SCHEMAS.  This file does.

For every entry in TOOL_SCHEMAS it dispatches at least one call with
plausible, valid arguments and asserts that:

* the result never contains ``"Unexpected error in"`` — that string means the
  call blew up in the dispatcher instead of reaching its implementation, which
  is exactly the §1.1 signature;
* every ToolCompleted.status is one of ok / denied / blocked / error.

A plain ``error`` is a legitimate outcome here: ask_image has no vision
provider configured in this environment and the bundled Pythonista docs do
not exist outside Pythonista.  What is never legitimate is a tool that cannot
be reached at all.

CASES below must name every declared tool; the test fails loudly if a new
tool is added to TOOL_SCHEMAS without a case, so this coverage cannot rot.
Lives permanently in miniagent/tests/; run it directly or through
run_all.py.
"""

import json
import shutil
import sys
import tempfile
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

from miniagent.events import (  # noqa: E402
    PermissionAnswer,
    PermissionNeeded,
    ToolCompleted,
    ToolStarted,
)
from miniagent.knowledge import Knowledge  # noqa: E402
from miniagent.permissions import Permissions  # noqa: E402
from miniagent.runner import Runner  # noqa: E402
from miniagent.tools import CAPABILITY_MAP, TOOL_SCHEMAS, Tools  # noqa: E402
from miniagent.workspace import Workspace  # noqa: E402

failures = []

VALID_STATUSES = {"ok", "denied", "blocked", "error"}

# The dispatcher's catch-all wraps anything that is not a collaborator error
# in this phrase.  It always means the call never reached its implementation.
DISPATCH_BUG = "Unexpected error in"


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


class Dispatch:
    """One dispatch run: the events it yielded and the JSON it returned."""

    def __init__(self, events, result):
        self.events = events
        self.result = result
        self.payload = json.loads(result)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]

    @property
    def status(self):
        completed = self.of(ToolCompleted)
        return completed[-1].status if completed else None

    @property
    def prompts(self):
        return self.of(PermissionNeeded)


def dispatch(tools, name, args, answers=()):
    """Run one tool call to completion, answering prompts from *answers*.

    Entries of *answers* are raw typed strings (parsed exactly as a front end
    parses a keystroke) or ready-made PermissionAnswers.  An exhausted queue
    sends None, which the permission layer treats as a deny-once.
    """
    call = {
        "id": f"call_{name}",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    queue = list(answers)
    events, to_send = [], None
    gen = tools.dispatch(call)
    while True:
        try:
            event = gen.send(to_send)
        except StopIteration as stop:
            return Dispatch(events, stop.value)
        events.append(event)
        to_send = None
        if isinstance(event, PermissionNeeded):
            if queue:
                raw = queue.pop(0)
                to_send = (raw if isinstance(raw, PermissionAnswer)
                           else Permissions.parse_answer(raw))


tmp_roots = []


def fresh_tools(prefix="tools"):
    """A (workspace root, permissions, tools, knowledge root) set on temp dirs."""
    root = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_ws_"))
    state = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_state_"))
    kb_root = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_kb_"))
    tmp_roots.extend([root, state, kb_root])

    (root / "sub").mkdir()
    (root / "notes.txt").write_text("alpha\nbeta needle\ngamma\n",
                                    encoding="utf-8")
    (root / "sub" / "inner.md").write_text("# inner\nneedle here\n",
                                           encoding="utf-8")
    (root / "script.py").write_text(
        "print('ran the script')\n", encoding="utf-8")
    (root / "replace_me.txt").write_text("old contents\n", encoding="utf-8")
    (root / "edit_me.txt").write_text("keep\nchange me\nkeep\n",
                                      encoding="utf-8")
    (root / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (kb_root / "env.md").write_text("# environment\nneedle in knowledge\n",
                                    encoding="utf-8")

    workspace = Workspace(str(root))
    permissions = Permissions(str(state), str(root))
    tools = Tools(workspace, permissions, Runner(workspace),
                  knowledge=Knowledge(root=str(kb_root)))
    return root, permissions, tools, kb_root.resolve()


try:
    root, permissions, tools, kb_root = fresh_tools()

    # --- one plausible, valid call per declared tool ----------------------

    # Every name in TOOL_SCHEMAS must appear here.  Values are (args, note);
    # the note says what outcome this environment can legitimately produce.
    CASES = {
        "list_files": ({"path": ".", "recursive": True}, "ok"),
        "read_file": ({"path": "notes.txt"}, "ok"),
        "search_files": ({"pattern": "needle"}, "ok"),
        "create_file": ({"path": "made.txt", "content": "hello\n"}, "ok"),
        "edit_file": ({"path": "edit_me.txt", "old_text": "change me",
                       "new_text": "changed"}, "ok"),
        "multi_edit": ({"path": "notes.txt", "edits": [
            {"old_text": "alpha", "new_text": "ALPHA"},
            {"old_text": "gamma", "new_text": "GAMMA"},
        ]}, "ok"),
        "overwrite_file": ({"path": "replace_me.txt",
                            "content": "brand new\n"}, "ok"),
        "clean_up": ({"paths": ["made.txt"]}, "ok"),
        "run_python": ({"path": "script.py", "args": []}, "ok"),
        # No vision provider is configured in this environment, so this is
        # expected to come back as a clean error — but it must be the
        # implementation's error, not the dispatcher's.
        "ask_image": ({"question": "what is this?",
                       "images": ["picture.png"]}, "error"),
        "knowledge": ({"action": "list"}, "ok"),
    }

    declared = [s["function"]["name"] for s in TOOL_SCHEMAS]
    check("every declared tool has a case here",
          sorted(n for n in declared if n not in CASES), [])
    check("no case names a tool that is not declared",
          sorted(n for n in CASES if n not in declared), [])

    # "a" = always-allow, so a gated tool is authorised once and the stored
    # decision covers the rest of the run; ungated tools never see a prompt.
    for name in declared:
        args, expected = CASES[name]
        run = dispatch(tools, name, args, answers=["a"])
        check(f"{name}: result is not a dispatcher blow-up",
              DISPATCH_BUG in run.result, False)
        check(f"{name}: status is a known status",
              run.status in VALID_STATUSES, True)
        check(f"{name}: expected outcome in this environment",
              run.status, expected)
        check(f"{name}: yields exactly one ToolStarted/ToolCompleted pair",
              (len(run.of(ToolStarted)), len(run.of(ToolCompleted))), (1, 1))
        check(f"{name}: events name the tool",
              {e.name for e in run.of(ToolStarted) + run.of(ToolCompleted)},
              {name})
        check(f"{name}: result parses as a JSON object",
              isinstance(run.payload, dict), True)
        if expected == "error":
            check(f"{name}: the error came from the implementation",
                  DISPATCH_BUG in str(run.payload.get("error", "")), False)

    # The knowledge tool's other actions reach their implementations too —
    # §1.1 would have broken all four identically.
    kb_cases = {
        "read": ({"action": "read", "path": "env.md"}, "ok"),
        "search": ({"action": "search", "pattern": "needle"}, "ok"),
        # The bundled Pythonista documentation does not exist off-device, so
        # a clean error here is the right answer.
        "docs": ({"action": "docs", "query": "ui"}, None),
        "bad action": ({"action": "nope"}, "error"),
    }
    for label, (args, expected) in kb_cases.items():
        run = dispatch(tools, "knowledge", args)
        check(f"knowledge/{label}: not a dispatcher blow-up",
              DISPATCH_BUG in run.result, False)
        check(f"knowledge/{label}: status is a known status",
              run.status in VALID_STATUSES, True)
        if expected is not None:
            check(f"knowledge/{label}: expected outcome", run.status, expected)
    check("knowledge list reached the real knowledge root",
          json.loads(dispatch(tools, "knowledge",
                              {"action": "list"}).result).get("root"),
          str(kb_root))

    # --- the side effects really happened ---------------------------------

    check("create_file wrote the file (then clean_up moved it)",
          (root / "made.txt").exists(), False)
    check("clean_up moved it into to_delete",
          any(p.name.startswith("made") for p in (root / "to_delete").iterdir()),
          True)
    check("edit_file changed the file",
          (root / "edit_me.txt").read_text(encoding="utf-8"),
          "keep\nchanged\nkeep\n")
    check("multi_edit applied both edits atomically",
          (root / "notes.txt").read_text(encoding="utf-8"),
          "ALPHA\nbeta needle\nGAMMA\n")
    check("overwrite_file replaced the file",
          (root / "replace_me.txt").read_text(encoding="utf-8"),
          "brand new\n")

    # --- gated tools ask, ungated tools do not ----------------------------

    gated_names = sorted(CAPABILITY_MAP)
    check("the gated set is the capability map",
          gated_names,
          sorted(["ask_image", "create_file", "edit_file", "multi_edit",
                  "overwrite_file", "run_python"]))

    for name in gated_names:
        # A fresh policy each time, so no stored decision short-circuits it.
        g_root, g_perms, g_tools, _ = fresh_tools("gate")
        args = dict(CASES[name][0])
        if name == "create_file":
            args["path"] = "gated_new.txt"
        run = dispatch(g_tools, name, args, answers=["y"])
        check(f"{name}: raises PermissionNeeded when policy has no answer",
              len(run.prompts), 1)
        prompt = run.prompts[0]
        check(f"{name}: prompt names the right capability",
              prompt.capability, CAPABILITY_MAP[name])
        check(f"{name}: prompt has a title and details",
              (bool(prompt.title.strip()), bool(prompt.details.strip())),
              (True, True))
        check(f"{name}: the prompt precedes the completion",
              [type(e).__name__ for e in run.events],
              ["ToolStarted", "PermissionNeeded", "ToolCompleted"])

    for name in declared:
        if name in CAPABILITY_MAP:
            continue
        u_root, _, u_tools, _ = fresh_tools("ungated")
        run = dispatch(u_tools, name, CASES[name][0])
        check(f"{name}: ungated, never prompts", run.prompts, [])

    # --- denying a gated tool prevents the side effect --------------------

    d_root, d_perms, d_tools, _ = fresh_tools("deny")
    run = dispatch(d_tools, "create_file",
                   {"path": "never.txt", "content": "should not exist"},
                   answers=["n"])
    check("denied create_file reports denied", run.status, "denied")
    check("denied create_file did not write the file",
          (d_root / "never.txt").exists(), False)
    check("denied result tells the model it was denied",
          run.payload, {"ok": False, "denied": True})

    run = dispatch(d_tools, "edit_file",
                   {"path": "edit_me.txt", "old_text": "change me",
                    "new_text": "must not apply"},
                   answers=["n"])
    check("denied edit_file reports denied", run.status, "denied")
    check("denied edit_file left the file alone",
          (d_root / "edit_me.txt").read_text(encoding="utf-8"),
          "keep\nchange me\nkeep\n")

    run = dispatch(d_tools, "overwrite_file",
                   {"path": "replace_me.txt", "content": "must not apply"},
                   answers=["n"])
    check("denied overwrite_file left the file alone",
          (d_root / "replace_me.txt").read_text(encoding="utf-8"),
          "old contents\n")

    run = dispatch(d_tools, "run_python", {"path": "script.py"},
                   answers=["n"])
    check("denied run_python reports denied", run.status, "denied")
    check("denied run_python produced no output",
          "ran the script" in run.result, False)

    # An unanswered prompt (nobody watching) denies too.
    run = dispatch(d_tools, "create_file",
                   {"path": "unanswered.txt", "content": "x"})
    check("an unanswered prompt denies", run.status, "denied")
    check("an unanswered prompt writes nothing",
          (d_root / "unanswered.txt").exists(), False)

    # A comment on the answer is relayed to the model, and a session-scoped
    # denial stops the tool being asked about again.
    run = dispatch(d_tools, "create_file",
                   {"path": "redirected.txt", "content": "x"},
                   answers=["n. Write it to other/place instead"])
    check("the answer's comment reaches the model",
          run.payload.get("user_comment"), "Write it to other/place instead")
    check("the comment rides along on the completion event",
          run.of(ToolCompleted)[0].comment, "Write it to other/place instead")

    run = dispatch(d_tools, "create_file", {"path": "a.txt", "content": "x"},
                   answers=["d"])
    check("a session denial denies the call", run.status, "denied")
    run = dispatch(d_tools, "create_file", {"path": "b.txt", "content": "x"})
    check("a session denial is not asked about again", run.prompts, [])
    check("a session denial keeps denying", run.status, "denied")
    check("a session denial still writes nothing",
          (d_root / "b.txt").exists(), False)

    # An allow-for-the-session lets the same capability through untouched.
    a_root, _, a_tools, _ = fresh_tools("allow")
    run = dispatch(a_tools, "create_file", {"path": "one.txt", "content": "1"},
                   answers=["s"])
    check("a session allow permits the call", run.status, "ok")
    run = dispatch(a_tools, "create_file", {"path": "two.txt", "content": "2"})
    check("a session allow is not asked about again", run.prompts, [])
    check("a session allow keeps allowing", run.status, "ok")
    check("both files were written",
          ((a_root / "one.txt").exists(), (a_root / "two.txt").exists()),
          (True, True))

    # --- malformed arguments are a clean error, not a blow-up --------------

    bad = {"id": "call_bad",
           "function": {"name": "read_file", "arguments": "{not json"}}
    events, to_send = [], None
    gen = tools.dispatch(bad)
    while True:
        try:
            events.append(gen.send(to_send))
        except StopIteration as stop:
            bad_result = stop.value
            break
    check("invalid JSON arguments give a clean error",
          json.loads(bad_result).get("error"), "Invalid JSON arguments")
    check("invalid JSON arguments still yield the event pair",
          [type(e).__name__ for e in events],
          ["ToolStarted", "ToolCompleted"])
    check("invalid JSON arguments are an error status",
          events[-1].status, "error")
    check("invalid JSON arguments are not a dispatcher blow-up",
          DISPATCH_BUG in bad_result, False)

    # A confinement violation comes back as the workspace's own error, not as
    # an exception escaping the dispatcher.
    run = dispatch(tools, "read_file", {"path": "../outside.txt"})
    check("escaping the workspace is a clean error", run.status, "error")
    check("the error says the path escaped the root",
          "escapes workspace root" in run.payload.get("error", ""), True)
    check("an escaping path is not a dispatcher blow-up",
          DISPATCH_BUG in run.result, False)

    # The fourth status, `blocked`, comes from a collaborator error whose
    # message starts with "Blocked" — today only the runner's static scan.
    b_root, _, b_tools, _ = fresh_tools("blocked")
    (b_root / "exiting.py").write_text(
        "import sys\nsys.exit(1)\n", encoding="utf-8")
    run = dispatch(b_tools, "run_python", {"path": "exiting.py"},
                   answers=["y"])
    check("a refused run_python is blocked", run.status, "blocked")
    check("the blocked result names the finding",
          "Blocked termination call" in run.payload.get("error", ""), True)
    check("a blocked run is not a dispatcher blow-up",
          DISPATCH_BUG in run.result, False)

    # A call with a required argument missing is refused by the
    # required-argument gate in dispatch(), which derives what each tool
    # needs from its own schema.  It must be a clean, actionable error that
    # names the argument -- never the dispatcher's catch-all, whose message
    # ("Unexpected error in 'read_file': 'path'") is a bare KeyError the
    # model cannot act on and cannot tell apart from a harness fault.
    run = dispatch(tools, "read_file", {})
    check("a missing required argument is a clean error",
          run.status, "error")
    check("a missing required argument is not a dispatcher blow-up",
          DISPATCH_BUG in run.result, False)
    check("a missing required argument names the argument",
          "'path'" in run.payload.get("error", ""), True)
    check("a missing required argument never reaches the tool",
          "requires the argument" in run.payload.get("error", ""), True)

    # Several missing at once are reported together, so the model can fix
    # the call in one step instead of discovering them one round trip apart.
    run = dispatch(tools, "edit_file", {"path": "notes.txt"})
    check("every missing required argument is named",
          ("'old_text'" in run.payload.get("error", "")
           and "'new_text'" in run.payload.get("error", "")), True)
    check("a malformed gated call is refused without prompting the user",
          [type(e).__name__ for e in run.events],
          ["ToolStarted", "ToolCompleted"])

    # --- multi_edit is atomic: a failing edit leaves the file untouched ----

    m_root, _, m_tools, _ = fresh_tools("multi")
    before_bytes = (m_root / "edit_me.txt").read_bytes()
    run = dispatch(m_tools, "multi_edit", {
        "path": "edit_me.txt",
        "edits": [
            {"old_text": "change me", "new_text": "changed"},
            {"old_text": "does not exist anywhere", "new_text": "x"},
        ],
    }, answers=["a"])
    check("a multi_edit with a failing edit reports an error", run.status, "error")
    check("the error names the failing edit's index",
          "edit #2" in run.payload.get("error", ""), True)
    check("multi_edit leaves the file byte-identical on failure",
          (m_root / "edit_me.txt").read_bytes(), before_bytes)

    # Each edit sees the running (already-edited) text: the second edit here
    # only becomes unique because the first one already ran.
    run = dispatch(m_tools, "multi_edit", {
        "path": "edit_me.txt",
        "edits": [
            {"old_text": "change me", "new_text": "changed"},
            {"old_text": "changed\nkeep", "new_text": "changed\nKEPT"},
        ],
    })
    check("a successful multi_edit applies every edit in order",
          (m_root / "edit_me.txt").read_text(encoding="utf-8"),
          "keep\nchanged\nKEPT\n")
    check("multi_edit reports how many edits and replacements were applied",
          (run.payload.get("edits_applied"), run.payload.get("replacements")),
          (2, 2))

finally:
    for path in tmp_roots:
        shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} tool dispatch check(s) failed")
print("All checks passed.")
