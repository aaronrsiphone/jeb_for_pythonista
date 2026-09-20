"""Scratch self-test for the new search_files tool (Workspace + Tools wiring).

Safe to run via run_python: no network, no console loop, no workspace writes.
It builds a throwaway tree inside the system temp directory, exercises the
search paths, then removes the tree. Delete this file when done.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these new modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

parent = Path(__file__).resolve().parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent.tools import CAPABILITY_MAP, TOOL_SCHEMAS, Tools  # noqa: E402
from miniagent.workspace import (  # noqa: E402
    Workspace,
    WorkspaceError,
    _clip_line,
)

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


# --- fixture -------------------------------------------------------------

tmp = tempfile.mkdtemp(prefix="ma_search_test_")
try:
    os.makedirs(os.path.join(tmp, "sub"))
    Path(tmp, "alpha.py").write_text(
        "def hello():\n    print('Hello World')\nx = 1\n", encoding="utf-8"
    )
    Path(tmp, "big.py").write_text(
        "print('big')\n", encoding="utf-8"
    )
    Path(tmp, "notes.txt").write_text(
        "TODO: fix the parser\nnothing to see here\n", encoding="utf-8"
    )
    Path(tmp, "sub", "beta.md").write_text(
        "# Notes\nsearch me maybe\nplain line\n", encoding="utf-8"
    )
    Path(tmp, "blob.bin").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    # Just over the 2 MiB skip limit; contains a pattern nothing else has.
    Path(tmp, "huge.txt").write_text(
        "needle" + "x" * (2 * 1024 * 1024 + 1), encoding="utf-8"
    )

    ws = Workspace(tmp)

    # --- literal search, case-insensitive by default ---------------------
    res = ws.search_files("hello")
    check("default finds both hello lines",
          [m["path"] for m in res["matches"]], ["alpha.py", "alpha.py"])
    check("line numbers are correct",
          [m["line"] for m in res["matches"]], [1, 2])

    # --- case-sensitive ----------------------------------------------------
    res = ws.search_files("hello", case_sensitive=True)
    check("case-sensitive narrows to one",
          [(m["path"], m["line"]) for m in res["matches"]],
          [("alpha.py", 1)])

    # --- regex ---------------------------------------------------------------
    res = ws.search_files(r"\bTODO\b", regex=True)
    check("regex finds TODO",
          (res["matches"][0]["path"], res["matches"][0]["text"]),
          ("notes.txt", "TODO: fix the parser"))

    try:
        ws.search_files("[unclosed", regex=True)
        check("invalid regex raises", "no error", "WorkspaceError")
    except WorkspaceError:
        check("invalid regex raises", "WorkspaceError", "WorkspaceError")

    # --- include glob ---------------------------------------------------------
    res = ws.search_files("hello", include="*.py")
    check("include glob filters", sorted({m["path"] for m in res["matches"]}),
          ["alpha.py"])
    check("include glob can exclude everything",
          ws.search_files("hello", include="*.md")["matches"], [])

    # --- max_results truncation -------------------------------------------------
    res = ws.search_files("hello", max_results=1)
    check("max_results caps matches", len(res["matches"]), 1)
    check("truncated flag set", res["truncated"], True)

    # --- counters: binary + oversized skipped ------------------------------------
    res = ws.search_files("hello")
    check("binary and oversized files counted as skipped",
          res["files_skipped"], 2)
    check("readable text files counted as searched",
          res["files_searched"], 4)

    # the oversized file's pattern must not leak into results
    res = ws.search_files("needle")
    check("oversized file not searched", res["matches"], [])
    check("binary + oversized skips counted", res["files_skipped"], 2)

    # --- error paths ------------------------------------------------------------
    for label, kwargs in [
        ("missing path", {"path": "nope"}),
        ("file as path", {"path": "alpha.py"}),
        ("escaping path", {"path": ".."}),
    ]:
        try:
            ws.search_files("hello", **kwargs)
            check(f"{label} raises", "no error", "WorkspaceError")
        except WorkspaceError:
            check(f"{label} raises", "WorkspaceError", "WorkspaceError")

    try:
        ws.search_files("")
        check("empty pattern raises", "no error", "WorkspaceError")
    except WorkspaceError:
        check("empty pattern raises", "WorkspaceError", "WorkspaceError")

    # --- line clipping -----------------------------------------------------------
    check("clip_line passes short lines", _clip_line("abc"), "abc")
    clipped = _clip_line("a" * 400)
    check("clip_line marks long lines",
          clipped.endswith("...[line truncated]"), True)
    check("clip_line keeps prefix", clipped[:300], "a" * 300)

    # --- symlink escape is skipped, not followed ------------------------------
    outside = tempfile.mkdtemp(prefix="ma_search_out_")
    try:
        Path(outside, "secret.txt").write_text(
            "hello from outside\n", encoding="utf-8"
        )
        try:
            os.symlink(Path(outside, "secret.txt"), Path(tmp, "escape.txt"))
            res = ws.search_files("hello from outside")
            check("escaping symlink not searched",
                  res["matches"], [])
        except (OSError, NotImplementedError):
            print("SKIP: symlinks not available on this filesystem")
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    # --- Tools wiring: schema, capability map, dispatch -------------------------
    check("search_files needs no permission",
          "search_files" in CAPABILITY_MAP, False)
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    check("schema registered", "search_files" in names, True)
    try:
        json.dumps(TOOL_SCHEMAS)
        check("schemas JSON-serializable", True, True)
    except (TypeError, ValueError):
        check("schemas JSON-serializable", False, True)

    tools = Tools(ws, None, None)  # permissions/runner unused by search_files
    call = {
        "id": "call_search_1",
        "function": {
            "name": "search_files",
            "arguments": json.dumps({"pattern": "search me", "path": "sub"}),
        },
    }
    parsed = json.loads(tools.dispatch(call))
    check("dispatch returns matches", len(parsed["matches"]), 1)
    check("dispatch match detail", parsed["matches"][0],
          {"path": "sub/beta.md", "line": 2, "text": "search me maybe"})
    check("dispatch not truncated", parsed["truncated"], False)

    call_bad = {
        "id": "call_search_2",
        "function": {"name": "search_files", "arguments": "{}"},
    }
    parsed = json.loads(tools.dispatch(call_bad))
    check("missing pattern gives clean error", parsed.get("ok", False), False)
    check("error mentions pattern",
          "pattern" in parsed.get("error", "").lower(), True)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
else:
    print("All checks passed.")
