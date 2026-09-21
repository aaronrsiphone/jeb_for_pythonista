"""Tests for the editing primitives that fail less (§5.1) and the compile
gate that protects a self-edit from a harness that will not start (§5.2).

Safe to run via run_python: no network, no console loop, and no writes
outside the system temp directory — every ``Workspace`` here is rooted at a
throwaway project dir.

What it pins down:

* a ``.py`` write with a syntax error is refused before anything touches
  disk, and the original file (or the absence of one) survives untouched;
  the identical content written to a non-``.py`` path is unaffected;
* ``edit_file``'s ``replace_all`` and ``occurrence`` parameters, including
  that they are mutually exclusive and that the default (neither given)
  behaviour is unchanged;
* the improved failure messages actually name the near-miss (a close but
  not exact match) and every occurrence's line number (a non-unique match).

Lives permanently in miniagent/tests/; run it directly or through
run_all.py.
"""

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

from miniagent.workspace import Workspace, WorkspaceError  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


tmp_roots = []


def fresh_ws(prefix="edit"):
    root = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_ws_"))
    tmp_roots.append(root)
    return root, Workspace(root)


try:

    # --- the compile gate ----------------------------------------------------

    root, ws = fresh_ws("compile")

    BAD_PY = "def broken(:\n    pass\n"

    try:
        ws.create_file("bad.py", BAD_PY)
        raised = False
    except WorkspaceError as exc:
        raised = True
        message = str(exc)
    check("a syntax-error .py create is refused", raised, True)
    check("the error names the file", "bad.py" in message, True)
    check("the error names a line number", "line" in message, True)
    check("bad.py was never created", (root / "bad.py").exists(), False)
    check("no leftover temp file was left behind",
          [p.name for p in root.iterdir() if p.name.startswith(".mw_")], [])

    # The identical content is fine in a non-.py file: the gate only looks
    # at the target path's suffix.
    ws.create_file("bad.txt", BAD_PY)
    check("the same content is written fine to a non-.py path",
          (root / "bad.txt").read_text(encoding="utf-8"), BAD_PY)

    # A syntax error in edit_file must leave the original .py file untouched.
    ws.create_file("good.py", "x = 1\n")
    before = (root / "good.py").read_bytes()
    try:
        ws.edit_file("good.py", "x = 1", "def broken(:")
        raised = False
    except WorkspaceError:
        raised = True
    check("a syntax-error edit_file is refused", raised, True)
    check("the original .py file survives byte-for-byte",
          (root / "good.py").read_bytes(), before)

    # And overwrite_file, same story.
    try:
        ws.overwrite_file("good.py", "def broken(:\n")
        raised = False
    except WorkspaceError:
        raised = True
    check("a syntax-error overwrite_file is refused", raised, True)
    check("overwrite_file left the original .py file untouched",
          (root / "good.py").read_bytes(), before)

    # Valid Python is written normally — the gate does not just reject
    # everything ending in .py.
    ws.overwrite_file("good.py", "x = 2\n")
    check("valid Python is written normally",
          (root / "good.py").read_text(encoding="utf-8"), "x = 2\n")

    # --- replace_all ----------------------------------------------------------

    root, ws = fresh_ws("replace_all")
    ws.create_file("r.txt", "cat cat cat\n")
    result = ws.edit_file("r.txt", "cat", "dog", replace_all=True)
    check("replace_all rewrites every occurrence",
          (root / "r.txt").read_text(encoding="utf-8"), "dog dog dog\n")
    check("replace_all reports how many replacements were made",
          result["replacements"], 3)

    # --- occurrence -------------------------------------------------------

    root, ws = fresh_ws("occurrence")
    ws.create_file("o.txt", "cat cat cat\n")
    result = ws.edit_file("o.txt", "cat", "dog", occurrence=2)
    check("occurrence=2 replaces only the second match",
          (root / "o.txt").read_text(encoding="utf-8"), "cat dog cat\n")
    check("occurrence reports exactly one replacement", result["replacements"], 1)

    try:
        ws.edit_file("o.txt", "cat", "dog", occurrence=5)
        raised = False
    except WorkspaceError as exc:
        raised = True
        oob_message = str(exc)
    check("an out-of-range occurrence is refused", raised, True)
    check("the out-of-range error names the actual count",
          "only 2 time" in oob_message, True)
    check("an out-of-range occurrence changed nothing",
          (root / "o.txt").read_text(encoding="utf-8"), "cat dog cat\n")

    # replace_all and occurrence are mutually exclusive.
    try:
        ws.edit_file("o.txt", "cat", "dog", replace_all=True, occurrence=1)
        raised = False
    except WorkspaceError as exc:
        raised = True
        mutex_message = str(exc)
    check("replace_all and occurrence together are refused", raised, True)
    check("the mutual-exclusion error is clear",
          "mutually exclusive" in mutex_message, True)

    # --- default behaviour is unchanged when neither is given ---------------

    root, ws = fresh_ws("default")
    ws.create_file("d.txt", "unique line\nother line\n")
    result = ws.edit_file("d.txt", "unique line", "changed line")
    check("a unique match still needs neither parameter",
          (root / "d.txt").read_text(encoding="utf-8"),
          "changed line\nother line\n")
    check("a unique default edit reports one replacement",
          result["replacements"], 1)

    # --- a non-unique match without replace_all/occurrence names the lines --

    root, ws = fresh_ws("nonunique")
    ws.create_file("n.txt", "alpha\nbeta\nalpha\nbeta\nalpha\n")
    try:
        ws.edit_file("n.txt", "alpha", "ALPHA")
        raised = False
    except WorkspaceError as exc:
        raised = True
        dup_message = str(exc)
    check("a non-unique match without a disambiguator is refused", raised, True)
    check("the error names how many times it occurs", "3 times" in dup_message, True)
    check("the error names every occurrence's line number",
          all(f"{n}" in dup_message for n in ("1", "3", "5")), True)
    check("the error suggests both disambiguators",
          ("replace_all" in dup_message and "occurrence" in dup_message), True)
    check("a refused ambiguous edit changed nothing",
          (root / "n.txt").read_text(encoding="utf-8"),
          "alpha\nbeta\nalpha\nbeta\nalpha\n")

    # --- no match at all names the closest region and what differs ----------

    root, ws = fresh_ws("closematch")
    ws.create_file("c.txt", "def greet(name):\n    print('hello ' + name)\n")
    try:
        # A near-miss: one extra space, a smart-quote-free typo — the kind
        # of whitespace/text mismatch that used to cost a full round trip.
        ws.edit_file("c.txt", "def greet(name):\n    print('hi ' + name)\n", "pass")
        raised = False
    except WorkspaceError as exc:
        raised = True
        miss_message = str(exc)
    check("a near-miss old_text is refused", raised, True)
    check("the error says old_text was not found", "not found" in miss_message, True)
    check("the error names the closest region",
          "c.txt:1-2" in miss_message, True)
    check("the error shows a similarity score",
          "%" in miss_message, True)
    check("the error includes a diff of what differs",
          ("hello" in miss_message and "hi " in miss_message), True)
    check("a failed no-match edit changed nothing",
          (root / "c.txt").read_text(encoding="utf-8"),
          "def greet(name):\n    print('hello ' + name)\n")

    # An empty file has nothing to compare against — still a clean error,
    # not a crash.
    root, ws = fresh_ws("empty")
    ws.create_file("e.txt", "")
    try:
        ws.edit_file("e.txt", "anything", "something")
        raised = False
    except WorkspaceError as exc:
        raised = True
        empty_message = str(exc)
    check("no match in an empty file is a clean error", raised, True)
    check("the empty-file error says so", "empty" in empty_message, True)

    # --- preview_edit reflects replace_all / occurrence too ------------------

    root, ws = fresh_ws("preview")
    ws.create_file("p.txt", "cat cat cat\n")
    preview = ws.preview_edit("p.txt", "cat", "dog", replace_all=True)
    check("preview_edit shows the replacement count for replace_all",
          "3 replacement" in preview, True)
    bad_preview = ws.preview_edit("p.txt", "nope", "x")
    check("preview_edit reports a clean message for a no-match preview",
          bad_preview.startswith("(cannot preview:"), True)

finally:
    for path in tmp_roots:
        shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} editing check(s) failed")
print("All checks passed.")
