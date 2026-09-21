"""Tests for the permission policy layer (permissions.py).

Safe to run via run_python: no network, no console loop, no prompting, and
no writes outside the system temp directory — every Permissions instance is
pointed at a throwaway state dir, so the real ~/Documents/miniagent
permissions.json is never touched.

Permissions is policy only now: it does no I/O beyond its own JSON store and
never asks anything.  The whole flow is three calls — ``decide`` (what does
stored policy say?), ``parse_answer`` (what did the user type?) and
``apply_answer`` (record it, report the outcome) — so this file exercises
them directly, with no renderer and no input stubbing anywhere.  Lives
permanently in miniagent/tests/; run it directly or through run_all.py.
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

from miniagent.events import PermissionAnswer  # noqa: E402
from miniagent.permissions import (  # noqa: E402
    ALWAYS_ALLOW,
    ALWAYS_DENY,
    CAPABILITIES,
    ONCE_ALLOW,
    ONCE_DENY,
    SESSION_ALLOW,
    SESSION_DENY,
    Permissions,
)

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


tmp_roots = []


def fresh(prefix="perm"):
    """A (state_dir, project_root, Permissions) triple on throwaway dirs."""
    state = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_state_"))
    project = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_proj_"))
    tmp_roots.extend([state, project])
    return state, project, Permissions(str(state), str(project))


try:
    # --- decide() before anything is stored --------------------------------

    state, project, perms = fresh("blank")
    check("no stored policy means ask",
          [perms.decide(cap) for cap in CAPABILITIES],
          [None] * len(CAPABILITIES))

    # An unknown capability is refused outright rather than turned into a
    # question the user might approve: decide() answers False without asking.
    check("unknown capability refused without asking",
          perms.decide("teleport"), False)
    check("unknown capability cannot be granted",
          perms.apply_answer("teleport", PermissionAnswer(ALWAYS_ALLOW)),
          (False, ""))
    check("refusing an unknown capability stores nothing",
          perms.summary(), {"session": {}, "persistent": {}})

    # --- parse_answer: the six choice letters -------------------------------

    expected_decisions = {
        "y": ONCE_ALLOW,
        "s": SESSION_ALLOW,
        "a": ALWAYS_ALLOW,
        "n": ONCE_DENY,
        "d": SESSION_DENY,
        "x": ALWAYS_DENY,
    }
    for letter, decision in expected_decisions.items():
        answer = Permissions.parse_answer(letter)
        check(f"parse {letter!r} -> {decision}",
              (answer.decision, answer.comment), (decision, ""))
        upper = Permissions.parse_answer(letter.upper())
        check(f"parse {letter.upper()!r} (case-insensitive)",
              upper.decision, decision)

    # --- the six letters through apply_answer, and what decide() says after -

    # "once" decisions leave no trace at all.
    for letter, allowed in (("y", True), ("n", False)):
        _, _, once = fresh("once")
        result = once.apply_answer("write", Permissions.parse_answer(letter))
        check(f"apply {letter!r} returns {allowed}", result, (allowed, ""))
        check(f"{letter!r} leaves no stored decision",
              once.decide("write"), None)
        check(f"{letter!r} stores nothing at all", once.summary(),
              {"session": {}, "persistent": {}})

    # "session" decisions persist for this instance only.
    for letter, allowed, token in (("s", True, SESSION_ALLOW),
                                   ("d", False, SESSION_DENY)):
        s_state, s_project, sess = fresh("sess")
        check(f"apply {letter!r} returns {allowed}",
              sess.apply_answer("edit", Permissions.parse_answer(letter)),
              (allowed, ""))
        check(f"{letter!r} decides this capability afterwards",
              sess.decide("edit"), allowed)
        check(f"{letter!r} does not touch other capabilities",
              sess.decide("write"), None)
        check(f"{letter!r} recorded in the session scope",
              sess.summary()["session"], {"edit": token})
        check(f"{letter!r} recorded in no persistent scope",
              sess.summary()["persistent"], {})
        # Session scope is in-memory: a new instance over the same state dir
        # knows nothing about it, and nothing was written to disk.
        again = Permissions(str(s_state), str(s_project))
        check(f"{letter!r} does not survive a fresh instance",
              again.decide("edit"), None)
        check(f"{letter!r} writes no permissions.json",
              (s_state / "permissions.json").exists(), False)

    # "always" decisions survive a fresh Permissions over the same state dir.
    for letter, allowed, token in (("a", True, ALWAYS_ALLOW),
                                   ("x", False, ALWAYS_DENY)):
        a_state, a_project, always = fresh("always")
        check(f"apply {letter!r} returns {allowed}",
              always.apply_answer("run_python",
                                  Permissions.parse_answer(letter)),
              (allowed, ""))
        check(f"{letter!r} decides this capability afterwards",
              always.decide("run_python"), allowed)
        check(f"{letter!r} recorded in the persistent scope",
              always.summary()["persistent"], {"run_python": token})
        check(f"{letter!r} recorded in no session scope",
              always.summary()["session"], {})

        # The persistence round trip, through the file on disk.
        store = a_state / "permissions.json"
        check(f"{letter!r} wrote permissions.json", store.exists(), True)
        data = json.loads(store.read_text(encoding="utf-8"))
        check(f"{letter!r} stored under the project's real path",
              data["projects"][str(Path(a_project).resolve())],
              {"run_python": token})
        reloaded = Permissions(str(a_state), str(a_project))
        check(f"{letter!r} survives a fresh instance",
              reloaded.decide("run_python"), allowed)
        other_project = Path(tempfile.mkdtemp(prefix="ma_other_proj_"))
        tmp_roots.append(other_project)
        check(f"{letter!r} is scoped to this project",
              Permissions(str(a_state), str(other_project)).decide("run_python"),
              None)

    # Persistent decisions win over session ones, whichever way round.
    _, _, mixed = fresh("prec")
    mixed.apply_answer("write", PermissionAnswer(SESSION_DENY))
    check("session deny decides before anything persistent",
          mixed.decide("write"), False)
    mixed.apply_answer("write", PermissionAnswer(ALWAYS_ALLOW))
    check("persistent allow outranks a session deny",
          mixed.decide("write"), True)

    # --- comment parsing ------------------------------------------------------

    comment_cases = [
        ("n. Write it to foo/bar", ONCE_DENY, "Write it to foo/bar"),
        ("y. But also check xyz", ONCE_ALLOW, "But also check xyz"),
        ("n - use another path", ONCE_DENY, "use another path"),
        ("a: always fine", ALWAYS_ALLOW, "always fine"),
        ("d, stop asking", SESSION_DENY, "stop asking"),
        ("s; for this session", SESSION_ALLOW, "for this session"),
        ("x) never", ALWAYS_DENY, "never"),
        ("y Keep The CamelCase", ONCE_ALLOW, "Keep The CamelCase"),
        ("n.   padded   ", ONCE_DENY, "padded"),
        ("y. ends with a period.", ONCE_ALLOW, "ends with a period."),
        ("  y. leading space ", ONCE_ALLOW, "leading space"),
    ]
    for raw, decision, comment in comment_cases:
        answer = Permissions.parse_answer(raw)
        check(f"parse {raw!r}", (answer.decision, answer.comment),
              (decision, comment))

    # The comment is relayed back by apply_answer alongside the outcome.
    _, _, commented = fresh("comment")
    check("apply_answer relays the comment",
          commented.apply_answer(
              "write", Permissions.parse_answer("n. Write it to foo/bar")),
          (False, "Write it to foo/bar"))
    check("a commented allow still allows",
          commented.apply_answer(
              "write", Permissions.parse_answer("y. and check xyz")),
          (True, "and check xyz"))

    # --- unrecognisable answers ------------------------------------------------

    for raw in ("yes", "nope", "q", "maybe", "?", "1", "  ok  ", "no"):
        check(f"parse {raw!r} is unrecognisable",
              Permissions.parse_answer(raw), None)

    # --- blank and missing answers are deny-once ---------------------------------

    blank = Permissions.parse_answer("")
    check("blank answer parses to deny-once",
          (blank.decision, blank.comment), (ONCE_DENY, ""))
    check("whitespace-only answer parses to deny-once",
          Permissions.parse_answer("   \t ").decision, ONCE_DENY)

    _, _, blanks = fresh("blank2")
    check("blank answer denies once", blanks.apply_answer("edit", blank),
          (False, ""))
    check("blank answer stores nothing", blanks.decide("edit"), None)
    # A driver whose front end could not ask sends None; that is a deny-once
    # too, and so is anything that is not a PermissionAnswer.
    check("None answer denies once", blanks.apply_answer("edit", None),
          (False, ""))
    check("raw string answer denies once (not a PermissionAnswer)",
          blanks.apply_answer("edit", "y"), (False, ""))
    check("a refused answer still stores nothing", blanks.decide("edit"), None)

    # --- clear() wipes both scopes -------------------------------------------------

    c_state, c_project, clearing = fresh("clear")
    clearing.apply_answer("write", PermissionAnswer(ALWAYS_ALLOW))
    clearing.apply_answer("edit", PermissionAnswer(SESSION_ALLOW))
    check("both scopes populated before clear",
          (clearing.decide("write"), clearing.decide("edit")), (True, True))
    clearing.clear()
    check("clear wipes the persistent scope", clearing.decide("write"), None)
    check("clear wipes the session scope", clearing.decide("edit"), None)
    check("clear empties the summary", clearing.summary(),
          {"session": {}, "persistent": {}})
    check("clear is persisted to disk",
          json.loads((c_state / "permissions.json").read_text(
              encoding="utf-8")).get("projects", {}).get(
                  str(Path(c_project).resolve())), None)
    check("a fresh instance sees the cleared store",
          Permissions(str(c_state), str(c_project)).decide("write"), None)

    # --- describe()/summary_json() are readable, not asserted on verbatim ---------

    d_state, d_project, described = fresh("describe")
    described.apply_answer("overwrite", PermissionAnswer(ALWAYS_DENY))
    text = described.describe()
    check("describe names the project",
          str(Path(d_project).resolve()) in text, True)
    check("describe lists every capability",
          all(cap in text for cap in CAPABILITIES), True)
    check("summary_json round-trips",
          json.loads(described.summary_json()),
          {"session": {}, "persistent": {"overwrite": ALWAYS_DENY}})

    # --- a corrupt store degrades to "ask" rather than raising ---------------------

    b_state, b_project, _ = fresh("broken")
    (b_state / "permissions.json").write_text("{not json", encoding="utf-8")
    broken = Permissions(str(b_state), str(b_project))
    check("corrupt permissions.json falls back to asking",
          broken.decide("write"), None)

finally:
    for root in tmp_roots:
        shutil.rmtree(root, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} permission check(s) failed")
print("All checks passed.")
