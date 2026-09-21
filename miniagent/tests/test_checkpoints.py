"""Tests for the undo safety net (checkpoints.py) — the §5.2 regression test.

Safe to run via run_python: no network, no console loop, and no writes
outside the system temp directory — every ``Checkpoints`` here is pointed at
a throwaway state dir, and every ``Workspace`` at a throwaway project root.

What it pins down:

* ``snapshot()`` records a file's prior state once per turn group — a second
  mutation of the same file in the same turn does not overwrite the recorded
  "before" state;
* ``undo_last()`` rewrites a modified file back to its pre-turn bytes, and
  *deletes* a file that did not exist before the turn (so undoing a
  create_file removes the file instead of leaving it empty);
* ``clean_up`` is snapshotted too, so undoing a turn that cleaned up a file
  puts it back at its original location;
* retention prunes the oldest groups beyond the configured cap;
* the checkpoint store lives outside the workspace root entirely, mirroring
  how permissions.json and the session logs are kept out of the agent's own
  reach;
* a snapshot failure disables further recording but never breaks the write
  it was protecting (mirrors ``SessionLogger.record``'s defensive posture).

Lives permanently in miniagent/tests/; run it directly or through
run_all.py.
"""

import shutil
import sys
import tempfile
import time
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

from miniagent.checkpoints import Checkpoints  # noqa: E402
from miniagent.workspace import Workspace  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


tmp_roots = []


def fresh(prefix="ckpt", retention=20):
    """A (workspace root, state dir, Checkpoints, Workspace) set on temp dirs."""
    root = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_ws_"))
    state = Path(tempfile.mkdtemp(prefix=f"ma_{prefix}_state_"))
    tmp_roots.extend([root, state])
    checkpoints = Checkpoints(root, state_dir=str(state), retention=retention)
    workspace = Workspace(root, checkpoints=checkpoints)
    return root, state, checkpoints, workspace


try:

    # --- snapshot-once-per-group semantics ---------------------------------

    root, state, ckpt, ws = fresh("once")
    (root / "f.txt").write_text("v1\n", encoding="utf-8")

    ckpt.begin_turn()
    ws.edit_file("f.txt", "v1", "v2")
    check("first edit applied", (root / "f.txt").read_text(encoding="utf-8"), "v2\n")
    ws.edit_file("f.txt", "v2", "v3")
    check("second edit applied", (root / "f.txt").read_text(encoding="utf-8"), "v3\n")

    groups = ckpt.list_groups()
    check("one group open for both edits", len(groups), 1)
    check("the file was recorded once, not twice", len(groups[0]["files"]), 1)

    report = ckpt.undo_last()
    check("undo restores the pre-turn content, not the mid-turn one",
          (root / "f.txt").read_text(encoding="utf-8"), "v1\n")
    check("undo reports the file as restored", report["restored"], [str((root / "f.txt").resolve())])

    # --- snapshot() with no open group auto-begins one ----------------------

    root, state, ckpt, ws = fresh("auto")
    (root / "g.txt").write_text("hello\n", encoding="utf-8")
    check("no group open before any mutation", ckpt.current_turn, None)
    ws.overwrite_file("g.txt", "bye\n")
    check("a group was auto-begun by the mutation's snapshot() call",
          ckpt.current_turn is not None, True)
    report = ckpt.undo_last()
    check("auto-begun group restores correctly",
          (root / "g.txt").read_text(encoding="utf-8"), "hello\n")

    # --- undo deletes a file that did not exist before the turn ------------

    root, state, ckpt, ws = fresh("create")
    ckpt.begin_turn()
    ws.create_file("new.txt", "brand new\n")
    check("create_file wrote the file", (root / "new.txt").exists(), True)
    report = ckpt.undo_last()
    check("undo deletes a file that was created this turn",
          (root / "new.txt").exists(), False)
    check("undo reports the path as deleted, not restored",
          (report["deleted"], report["restored"]),
          ([str((root / "new.txt").resolve())], []))

    # --- undo restoring a modified file -------------------------------------

    root, state, ckpt, ws = fresh("modify")
    (root / "m.txt").write_text("original content\n", encoding="utf-8")
    ckpt.begin_turn()
    ws.overwrite_file("m.txt", "clobbered\n")
    check("overwrite applied", (root / "m.txt").read_text(encoding="utf-8"), "clobbered\n")
    ckpt.undo_last()
    check("undo restores the modified file byte-for-byte",
          (root / "m.txt").read_bytes(), b"original content\n")

    # --- undo after clean_up puts the file back where it was ----------------

    root, state, ckpt, ws = fresh("cleanup")
    (root / "scratch.txt").write_text("scratch data\n", encoding="utf-8")
    ckpt.begin_turn()
    outcome = ws.clean_up("scratch.txt")
    check("clean_up moved the file into to_delete",
          (root / "scratch.txt").exists(), False)
    trashed = root / outcome["moved_to"]
    check("the trashed copy exists", trashed.exists(), True)
    ckpt.undo_last()
    check("undo restores the file at its original location",
          (root / "scratch.txt").read_text(encoding="utf-8"), "scratch data\n")

    # --- retention prunes the oldest groups beyond the cap -------------------

    root, state, ckpt, ws = fresh("retain", retention=3)
    turn_ids = []
    for i in range(5):
        tid = ckpt.begin_turn()
        (root / f"r{i}.txt").write_text(f"v{i}\n", encoding="utf-8")
        ckpt.snapshot(root / f"r{i}.txt")
        turn_ids.append(tid)
        time.sleep(0.01)  # keep turn ids ordered even within the same second

    groups = ckpt.list_groups()
    check("retention caps the retained groups", len(groups), 3)
    kept_ids = [g["turn_id"] for g in groups]
    check("the three most recent turns survive, newest first",
          kept_ids, list(reversed(turn_ids[-3:])))
    check("the oldest turns were pruned from disk",
          [(ckpt.root / tid).exists() for tid in turn_ids[:2]], [False, False])

    # --- snapshots live outside the workspace --------------------------------

    root, state, ckpt, ws = fresh("outside")
    try:
        ckpt.root.resolve().relative_to(root.resolve())
        inside_workspace = True
    except ValueError:
        inside_workspace = False
    check("the checkpoint store is not inside the workspace root",
          inside_workspace, False)

    before_listing = sorted(p.name for p in root.iterdir())
    ckpt.begin_turn()
    ws.create_file("touched.txt", "x\n")
    ckpt.snapshot(root / "touched.txt")
    after_listing = sorted(p.name for p in root.iterdir())
    check("no checkpoint files were written inside the workspace",
          set(after_listing) - set(before_listing), {"touched.txt"})
    check("the workspace's own file tools cannot see the checkpoint directory",
          any("checkpoint" in name.lower() for name in after_listing), False)

    # --- a snapshot failure never breaks the write it was protecting --------

    root, state, ckpt, ws = fresh("broken")
    # Force every future begin_turn()/snapshot() to fail: put a plain file
    # where the checkpoints root directory needs to be created, so
    # os.makedirs(self.root, ...) raises instead of succeeding.  This is a
    # realistic on-disk failure mode (a stray file, a full/read-only volume),
    # not a monkeypatch of the collaborator's own defences.
    ckpt.root.parent.mkdir(parents=True, exist_ok=True)
    ckpt.root.write_text("not a directory", encoding="utf-8")

    (root / "protected.txt").write_text("before\n", encoding="utf-8")
    outcome = ws.overwrite_file("protected.txt", "after\n")
    check("the write succeeds even though its snapshot failed",
          outcome.get("ok"), True)
    check("the file content actually changed",
          (root / "protected.txt").read_text(encoding="utf-8"), "after\n")
    check("the checkpoints collaborator recorded itself as broken",
          ckpt._broken, True)
    check("a broken collaborator answers 'nothing to undo' rather than raising",
          ckpt.undo_last(), None)
    check("list_groups() degrades to empty rather than raising",
          ckpt.list_groups(), [])

    # --- undo_last() on an empty store ---------------------------------------

    root, state, ckpt, ws = fresh("empty")
    check("undo_last() with nothing recorded returns None", ckpt.undo_last(), None)
    check("list_groups() with nothing recorded returns []", ckpt.list_groups(), [])

    # --- undoing consumes the group; a second undo goes further back --------

    root, state, ckpt, ws = fresh("stack")
    (root / "s.txt").write_text("t0\n", encoding="utf-8")
    ckpt.begin_turn()
    ws.overwrite_file("s.txt", "t1\n")
    ckpt.begin_turn()
    ws.overwrite_file("s.txt", "t2\n")
    check("two turns recorded", len(ckpt.list_groups()), 2)
    ckpt.undo_last()
    check("first undo restores the second turn's prior content",
          (root / "s.txt").read_text(encoding="utf-8"), "t1\n")
    check("the restored group was consumed", len(ckpt.list_groups()), 1)
    ckpt.undo_last()
    check("second undo goes one turn further back",
          (root / "s.txt").read_text(encoding="utf-8"), "t0\n")
    check("both groups now consumed", len(ckpt.list_groups()), 0)
    check("undoing did not itself open a new group", ckpt.current_turn, None)

finally:
    for path in tmp_roots:
        shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} checkpoint check(s) failed")
print("All checks passed.")
