"""Undo safety net for self-editing (§5.2 of docs/rearchitecture.md).

An agent editing the harness *from inside* the harness has no rollback
without this: ``to_delete/`` (see ``Workspace.clean_up``) only covers scratch
files the agent moved there itself, not a botched ``edit_file`` on a file
that is still needed.

:class:`Checkpoints` snapshots a file's content the first time it is touched
in a "turn" (a group of related mutations — normally one user turn of the
console loop) and can restore that whole group later with :meth:`undo_last`.
Snapshots live **outside the project workspace**, under::

    <state_dir>/checkpoints/<workspace-key>/<turn-id>/
        manifest.json
        0000.bin
        0001.bin
        ...

— the same reasoning that already keeps ``permissions.json`` and the session
logs out of the workspace: the agent's own file tools cannot reach or tamper
with its own undo history.  ``<workspace-key>`` reuses
``sessions.workspace_key`` so a project's checkpoints live in the same place
its sessions do, and ``<state_dir>`` reuses ``config.default_state_dir``.

``manifest.json`` records, per touched path, whether the file existed before
the turn and (if so) which numbered content file holds its prior bytes; a
path that did not exist has no content file, so undoing a ``create_file``
deletes the file it created rather than leaving an empty one behind.

Like ``SessionLogger.record``, every public method here is defensive: a
snapshot failure disables further recording (one warning, printed once) but
never raises, so a broken or full disk can degrade the safety net without
breaking the write it was meant to protect.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .config import default_state_dir
from .sessions import workspace_key

CHECKPOINTS_DIRNAME = "checkpoints"
MANIFEST_NAME = "manifest.json"

# How many turn-groups to retain per workspace before the oldest are pruned.
# Unbounded growth on a phone (where this state dir is a Files-app-visible
# folder with real storage pressure) is a real cost, not a theoretical one.
DEFAULT_RETENTION = 20


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


_SEQUENCE_NAME = ".seq"


def _next_sequence(root: Path) -> int:
    """Return the next value of a small persistent counter under *root*.

    Turn ids need to sort in creation order even after older groups have
    been pruned from disk (see ``retention``) — a purely timestamp-based id
    like ``sessions._new_session_id`` uses would let a *later* turn reuse an
    *earlier* turn's id once that directory is gone, which would silently
    corrupt "newest first" ordering.  A monotonic, never-reused counter
    sidesteps that: it only ever goes up, on disk or off it.
    """
    path = root / _SEQUENCE_NAME
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        current = 0
    nxt = current + 1
    path.write_text(str(nxt), encoding="utf-8")
    return nxt


def _new_turn_id(root: Path) -> str:
    """Return a fresh, never-reused turn id (also the group's directory name).

    Timestamp-prefixed so it reads well in ``:checkpoints``, suffixed with a
    monotonic sequence number so lexical order always matches creation
    order, even across turns pruned from disk in between.
    """
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    seq = _next_sequence(root)
    return f"{stamp}-{seq:06d}"


class Checkpoints:
    """Per-workspace turn snapshots, stored outside the workspace.

    One instance is normally shared for the life of a session and handed to
    the :class:`~miniagent.workspace.Workspace` it protects.  ``begin_turn``
    starts a new snapshot group; ``snapshot`` records a path's prior state
    (auto-starting a group if none is open, so a caller that never calls
    ``begin_turn`` — a script, a test — still gets protection);
    ``undo_last`` restores and consumes the most recent group.
    """

    def __init__(self, workspace_root, state_dir=None, retention: int = DEFAULT_RETENTION):
        self.workspace_root = Path(workspace_root).resolve()
        self.state_dir = state_dir if state_dir is not None else default_state_dir()
        self.key = workspace_key(self.workspace_root)
        self.root = Path(self.state_dir) / CHECKPOINTS_DIRNAME / self.key
        self.retention = retention

        self._current = None      # current turn id, or None between turns
        self._touched = set()     # abs path strings already snapshotted this turn
        self._manifest = []       # entries accumulated for the current turn
        self._started = ""
        self._broken = False      # True once a failure has disabled recording

    # -- introspection -------------------------------------------------

    @property
    def current_turn(self):
        """The currently open turn id, or ``None`` between turns."""
        return self._current

    # -- recording -------------------------------------------------------

    def begin_turn(self, turn_id=None):
        """Start a new snapshot group and return its turn id.

        Never raises: on failure it prints one warning, disables further
        recording, and returns ``None``.  Pruning of old groups (see
        ``retention``) happens here, once per turn, rather than on every
        ``snapshot`` call.
        """
        if self._broken:
            return None
        try:
            os.makedirs(self.root, exist_ok=True)
            self._current = turn_id or _new_turn_id(self.root)
            os.makedirs(self.root / self._current, exist_ok=True)
            self._touched = set()
            self._manifest = []
            self._started = _now_iso()
            self._write_manifest()
            self._prune()
            return self._current
        except Exception as exc:  # a safety net must never itself break a turn
            self._disable(exc)
            return None

    def snapshot(self, path) -> bool:
        """Record *path*'s current on-disk state, once per open group.

        Call this **before** mutating *path*.  A second call for the same
        path within the same group is a no-op — "undo the turn" means
        restoring the state from before the turn's *first* touch, not the
        state before its most recent one.  Returns whether a new snapshot
        was actually recorded; never raises.
        """
        if self._broken:
            return False
        try:
            if self._current is None:
                if self.begin_turn() is None:
                    return False

            full = str(Path(path).resolve())
            if full in self._touched:
                return False

            group_dir = self.root / self._current
            existed = os.path.isfile(full)
            entry = {"path": full, "existed": existed, "content_file": None}
            if existed:
                data = Path(full).read_bytes()
                content_name = f"{len(self._manifest):04d}.bin"
                (group_dir / content_name).write_bytes(data)
                entry["content_file"] = content_name

            self._manifest.append(entry)
            self._write_manifest()
            self._touched.add(full)
            return True
        except Exception as exc:
            self._disable(exc)
            return False

    def _disable(self, exc):
        self._broken = True
        print(f"Checkpoint recording disabled: {exc}")

    # -- manifest I/O ------------------------------------------------------

    def _manifest_path(self, turn_id=None) -> Path:
        return self.root / (turn_id or self._current) / MANIFEST_NAME

    def _write_manifest(self):
        data = {
            "turn_id": self._current,
            "started": self._started,
            "files": self._manifest,
        }
        with open(self._manifest_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)

    @staticmethod
    def _read_manifest(group_dir: Path) -> dict:
        try:
            with open(group_dir / MANIFEST_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {"turn_id": group_dir.name, "started": "", "files": []}
        if not isinstance(data, dict):
            return {"turn_id": group_dir.name, "started": "", "files": []}
        data.setdefault("turn_id", group_dir.name)
        data.setdefault("started", "")
        if not isinstance(data.get("files"), list):
            data["files"] = []
        return data

    def _group_dirs(self) -> list:
        """Every retained group directory, oldest first."""
        if not self.root.is_dir():
            return []
        try:
            return sorted(p for p in self.root.iterdir() if p.is_dir())
        except OSError:
            return []

    def _prune(self):
        dirs = self._group_dirs()
        excess = len(dirs) - self.retention
        if excess <= 0:
            return
        for d in dirs[:excess]:
            if d.name == self._current:
                continue
            shutil.rmtree(d, ignore_errors=True)

    # -- restoring -----------------------------------------------------

    def undo_last(self):
        """Restore the most recently recorded group, and consume it.

        Rewrites every touched file back to its pre-turn content, and
        deletes files that did not exist before the turn.  Returns a report
        dict — ``None`` when there is nothing to undo — with enough detail
        for a UI to say what happened::

            {"turn_id": ..., "started": ..., "restored": [...],
             "deleted": [...], "errors": [...]}

        Restoring is plain filesystem I/O (``Path.write_bytes``/``os.remove``
        below), never a call back into ``Workspace``'s mutating methods, so
        it cannot itself open a new snapshot group.  The restored group is
        removed afterwards: a second ``:undo`` goes further back in history
        rather than redoing the same restore.
        """
        groups = self._group_dirs()
        if not groups:
            return None
        group_dir = groups[-1]
        manifest = self._read_manifest(group_dir)

        restored, deleted, errors = [], [], []
        for entry in manifest.get("files", []):
            path = entry.get("path", "")
            try:
                if entry.get("existed"):
                    content_file = entry.get("content_file")
                    data = (
                        (group_dir / content_file).read_bytes()
                        if content_file else b""
                    )
                    target = Path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    restored.append(path)
                else:
                    if os.path.exists(path):
                        os.remove(path)
                    deleted.append(path)
            except Exception as exc:
                errors.append({"path": path, "error": str(exc)})

        shutil.rmtree(group_dir, ignore_errors=True)
        if self._current == group_dir.name:
            self._current = None
            self._touched = set()
            self._manifest = []

        return {
            "turn_id": manifest.get("turn_id", group_dir.name),
            "started": manifest.get("started", ""),
            "restored": restored,
            "deleted": deleted,
            "errors": errors,
        }

    # -- introspection for :checkpoints --------------------------------

    def list_groups(self):
        """Every retained group, newest first, with turn id/timestamp/files."""
        out = []
        for group_dir in reversed(self._group_dirs()):
            manifest = self._read_manifest(group_dir)
            out.append({
                "turn_id": manifest.get("turn_id", group_dir.name),
                "started": manifest.get("started", ""),
                "files": [e.get("path", "") for e in manifest.get("files", [])],
            })
        return out
