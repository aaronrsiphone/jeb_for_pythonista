"""Session recording and resumption.

Conversations are recorded to JSON-lines files under::

    <state_dir>/sessions/<workspace-key>/<session-id>.jsonl

``<state_dir>`` is the application state directory (``~/Documents/miniagent``
by default, so sessions normally live in ``~/Documents/miniagent/sessions/``).
``<workspace-key>`` is the workspace path relative to ``~/Documents`` with
every path separator replaced by a dash (``projects/foo`` becomes
``projects-foo``); workspaces outside ``~/Documents`` fall back to their full
path treated the same way.  ``<session-id>`` is the file stem: a timestamp
such as ``2025-06-07_21-14-03``.

Each file is a session.  Recording is append-only — every conversation
message becomes exactly one line — so nothing is ever rewritten and a crash
loses at most the line being written.  The first line is a small ``meta``
object (start time, workspace, provider, model); message lines follow as they
happen::

    {"type": "meta", "version": 1, "started": "...", ...}
    {"type": "message", "message": {"role": "user", "content": "..."}}
    {"type": "message", "message": {"role": "assistant", ...}}

The system prompt is never recorded: it is rebuilt fresh on every start, so
a resumed session picks up the *current* JEB.md instructions rather than
stale ones.  Session files live outside the project workspace on purpose —
the agent's own file tools cannot read or edit them, just like
``permissions.json``.

``SessionLogger.record`` never raises: the first write failure prints one
warning and disables logging, so a logging problem can never break a turn.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import default_state_dir

SESSION_VERSION = 1
SESSIONS_DIRNAME = "sessions"
SESSION_SUFFIX = ".jsonl"

# Longest excerpt of the first user prompt shown in the :resume listing.
_PREVIEW_LIMIT = 72


class SessionError(Exception):
    """Raised when a recorded session cannot be loaded or attached."""


def workspace_key(workspace_root, base_dir=None) -> str:
    """Return the per-workspace sessions directory name.

    The key is the workspace path relative to *base_dir* (``~/Documents`` by
    default) with each path separator replaced by a dash, so
    ``~/Documents/projects/foo`` becomes ``projects-foo``.  Workspaces outside
    the base directory fall back to their full path, treated the same way.
    """
    root = Path(workspace_root).resolve()
    if base_dir is None:
        base_dir = Path.home() / "Documents"
    base = Path(base_dir).resolve()
    try:
        rel = root.relative_to(base)
    except ValueError:
        rel = root
    key = str(rel).replace(os.sep, "-").replace("/", "-").strip("-")
    if key in ("", "."):
        key = "root"
    return key


def repair_messages(messages) -> list:
    """Return *messages* reduced to a provider-safe conversation history.

    - System messages are dropped; the caller supplies a fresh system prompt.
    - An assistant message carrying ``tool_calls`` must be followed
      immediately by one ``tool``-role result per call, matching
      ``tool_call_id`` in order.  When that run is incomplete (a session cut
      off mid-turn) the assistant message and the partial results are
      dropped, so the provider never sees a tool call without its result.
    - Orphan ``tool`` messages (results whose requesting assistant message is
      gone) are dropped as well.

    Everything else is kept in the recorded order.
    """
    out = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue
        role = msg.get("role")
        calls = msg.get("tool_calls")
        if role == "system":
            i += 1
            continue
        if role == "assistant" and calls:
            expected = [c.get("id", "") for c in calls if isinstance(c, dict)]
            j = i + 1
            results = []
            while (j < n and len(results) < len(expected)
                   and isinstance(messages[j], dict)
                   and messages[j].get("role") == "tool"):
                results.append(messages[j])
                j += 1
            if [r.get("tool_call_id", "") for r in results] == expected:
                out.append(msg)
                out.extend(results)
            # else: incomplete tool sequence — dropped entirely
            i = j
            continue
        if role == "tool":
            i += 1  # orphan tool result — dropped
            continue
        out.append(msg)
        i += 1
    return out


class SessionInfo:
    """Summary of one recorded session (as shown by the ``:resume`` listing)."""

    __slots__ = ("id", "path", "started", "last_activity",
                 "message_count", "preview")

    def __init__(self, id, path, started, last_activity, message_count,
                 preview):
        self.id = id                      # file stem, e.g. 2025-06-07_21-14-03
        self.path = path
        self.started = started           # ISO timestamp from the meta line
        self.last_activity = last_activity  # file mtime, seconds since epoch
        self.message_count = message_count
        self.preview = preview           # first user prompt, one line

    def __repr__(self):
        return (f"<SessionInfo {self.id!r} messages={self.message_count} "
                f"preview={self.preview!r}>")


class SessionLogger:
    """Records a conversation to per-workspace JSON-lines session files.

    One session is one ``.jsonl`` file; messages are appended one line at a
    time.  The file (and its ``meta`` header line) is created lazily on the
    first recorded message, so a session in which nothing is said leaves
    nothing behind.
    """

    def __init__(self, workspace_root, state_dir=None, provider="", model=""):
        self.workspace_root = Path(workspace_root).resolve()
        self.state_dir = (state_dir if state_dir is not None
                          else default_state_dir())
        self.sessions_dir = (Path(self.state_dir) / SESSIONS_DIRNAME
                             / workspace_key(self.workspace_root))
        self._provider = provider or ""
        self._model = model or ""
        self._session_id = None   # current file stem; None = not started yet
        self._broken = False      # True once recording has failed

    # -- introspection ---------------------------------------------------

    @property
    def active_id(self):
        """Id of the session being recorded, or None before the first message."""
        return self._session_id

    def session_path(self, session_id) -> Path:
        """File path of the session with *session_id* (its stem)."""
        return self.sessions_dir / (session_id + SESSION_SUFFIX)

    # -- recording ---------------------------------------------------------

    def record(self, message):
        """Append one conversation message to the current session file.

        Never raises: the first failure prints a warning and disables
        logging for the rest of the process.
        """
        if self._broken or not isinstance(message, dict):
            return
        if message.get("role") == "system":
            return
        try:
            path = self._ensure_session()
            with open(path, "a", encoding="utf-8") as f:
                f.write(_line({"type": "message", "message": message}) + "\n")
        except Exception as exc:  # logging must never break the agent
            self._disable(exc)

    def rotate(self):
        """Finish the current session; the next message starts a fresh file.

        Called by ``Agent.reset`` so a post-``:reset`` conversation does not
        corrupt the recorded history of the segment that came before it.
        """
        self._session_id = None

    def attach(self, session_id):
        """Continue recording into the existing session *session_id*.

        Used after ``:resume`` so the resumed conversation keeps growing its
        original file.  Raises ``SessionError`` for an invalid or unknown id.
        """
        path = self._path_for(session_id)
        self._session_id = path.stem

    def _ensure_session(self) -> Path:
        """Create the session file (with its meta line) if none is active."""
        if self._session_id is None:
            os.makedirs(self.sessions_dir, exist_ok=True)
            self._session_id = _new_session_id(self.sessions_dir)
            meta = {
                "type": "meta",
                "version": SESSION_VERSION,
                "started": _now_iso(),
                "workspace": str(self.workspace_root),
                "provider": self._provider,
                "model": self._model,
            }
            with open(self.session_path(self._session_id), "a",
                      encoding="utf-8") as f:
                f.write(_line(meta) + "\n")
        return self.session_path(self._session_id)

    def _disable(self, exc):
        self._broken = True
        print(f"Session logging disabled: {exc}")

    # -- listing / loading -------------------------------------------------

    def list_sessions(self):
        """Summaries of every recorded session, most recent activity first.

        Files with no recorded messages (meta-only leftovers) are skipped.
        Never raises; an unreadable or missing directory yields an empty list.
        """
        sessions = []
        if not self.sessions_dir.is_dir():
            return sessions
        try:
            entries = sorted(self.sessions_dir.iterdir())
        except OSError:
            return sessions
        for entry in entries:
            if entry.suffix != SESSION_SUFFIX or not entry.is_file():
                continue
            info = self._scan(entry)
            if info is not None:
                sessions.append(info)
        sessions.sort(key=lambda s: (s.last_activity, s.id), reverse=True)
        return sessions

    def _scan(self, path):
        """Build a :class:`SessionInfo` for *path*, or None if unresumable."""
        started = ""
        count = 0
        preview = ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    kind = obj.get("type")
                    if kind == "meta" and not started:
                        started = str(obj.get("started") or "")
                    elif kind == "message":
                        count += 1
                        if not preview:
                            msg = obj.get("message")
                            if (isinstance(msg, dict)
                                    and msg.get("role") == "user"):
                                preview = _preview_text(msg.get("content"))
        except OSError:
            return None
        if count == 0:
            return None
        try:
            last_activity = path.stat().st_mtime
        except OSError:
            last_activity = 0.0
        return SessionInfo(
            id=path.stem,
            path=path,
            started=started,
            last_activity=last_activity,
            message_count=count,
            preview=preview,
        )

    def load(self, session_id):
        """Return the recorded conversation as a provider-safe message list.

        Raises ``SessionError`` when the id is invalid or the file unreadable.
        """
        path = self._path_for(session_id)
        messages = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if (isinstance(obj, dict) and obj.get("type") == "message"
                            and isinstance(obj.get("message"), dict)):
                        messages.append(obj["message"])
        except OSError as exc:
            raise SessionError(f"Cannot read session {session_id!r}: {exc}")
        return repair_messages(messages)

    def _path_for(self, session_id):
        """Validate *session_id* and return the path of its session file."""
        if (not isinstance(session_id, str)
                or not session_id.strip()
                or session_id.strip() != session_id
                or "/" in session_id or "\\" in session_id
                or session_id in (".", "..")
                or Path(session_id).name != session_id):
            raise SessionError(f"Invalid session id: {session_id!r}")
        path = self.session_path(session_id)
        if not path.is_file():
            raise SessionError(f"No recorded session named {session_id!r}")
        return path


# ---------------------------------------------------
# helpers
# ---------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_session_id(sessions_dir: Path) -> str:
    """Return a fresh, unused session id (also the file stem).

    Timestamp-shaped so ids sort chronologically and read well in the
    ``:resume`` listing; a counter suffix disambiguates same-second starts.
    """
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    candidate = stamp
    n = 1
    while (sessions_dir / (candidate + SESSION_SUFFIX)).exists():
        candidate = f"{stamp}-{n}"
        n += 1
    return candidate


def _line(obj) -> str:
    """Serialise *obj* to one JSONL line (no trailing newline)."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _content_text(content) -> str:
    """Extract displayable text from message content (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return " ".join(parts)
    return ""


def _preview_text(content) -> str:
    """One-line excerpt of *content* for the session listing."""
    text = _content_text(content)
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if line:
            if len(line) > _PREVIEW_LIMIT:
                line = line[: _PREVIEW_LIMIT - 3].rstrip() + "..."
            return line
    return ""
