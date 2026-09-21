"""Permission policy for MiniAgent tools.

Permissions are keyed by the canonical path of the project so that distinct
projects can have distinct persistent decisions.  The permission store lives
in the application-state directory, never inside a user project, so the agent
cannot edit its own permission policy through the file tools.

This module owns **policy only** — it does no I/O and never prompts.  The
split is deliberate: asking the user is a front-end concern, so a gated tool
call that stored policy does not already decide becomes a
:class:`~miniagent.events.PermissionNeeded` event that a renderer answers
(see :mod:`miniagent.ui`).  Three methods carry the whole flow:

``decide(capability)``
    Answer from persistent or session policy, or ``None`` for "must ask".
``parse_answer(raw)``
    Turn a typed answer (``"y"``, ``"n. use another path"``) into a
    :class:`~miniagent.events.PermissionAnswer`.  The letter semantics live
    here, with the policy they select, rather than in each front end.
``apply_answer(capability, answer)``
    Record a session/persistent decision and report the outcome.
"""

from __future__ import annotations

import json
import os

from .events import PermissionAnswer

# Distinct capabilities.
WRITE = "write"
EDIT = "edit"
OVERWRITE = "overwrite"
RUN_PYTHON = "run_python"
ASK_IMAGE = "ask_image"

CAPABILITIES = (WRITE, EDIT, OVERWRITE, RUN_PYTHON, ASK_IMAGE)

# Internal decision tokens.  The user-facing prompt uses single-letter
# choices (y/s/a/n/d/x) that mirror the original jeb.py.
ONCE_ALLOW = "once_allow"
ONCE_DENY = "once_deny"
SESSION_ALLOW = "session_allow"
SESSION_DENY = "session_deny"
ALWAYS_ALLOW = "always_allow"
ALWAYS_DENY = "always_deny"

# Choice letter -> internal decision token.  The empty string (blank input,
# EOF) maps to deny-once for safety, as in the original jeb.py.
_CHOICE_MAP = {
    "y": ONCE_ALLOW,
    "s": SESSION_ALLOW,
    "a": ALWAYS_ALLOW,
    "": ONCE_DENY,
    "n": ONCE_DENY,
    "d": SESSION_DENY,
    "x": ALWAYS_DENY,
}

# Characters that may sit between the choice letter and a trailing comment,
# e.g. "y. But also check xyz" or "n - use another path".
_COMMENT_SEPARATORS = " \t.,:;-)"

# Maximum characters of the permission details preview shown to the user.
_MAX_DIFF_CHARS = 10_000


def _trunc(value, limit):
    """Truncate a string to *limit*, appending a notice when cut."""
    text = str(value)
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return text[:limit] + f"\n... [{removed} chars truncated]"


def _parse_choice(raw):
    """Split a prompt answer into ``(letter, comment)``.

    Accepts a bare choice letter (``"y"``), a blank answer (treated as a
    deny-once, the historical behaviour), or a choice letter followed by a
    separator and a free-text comment for the agent, e.g.
    ``"y. But also can you check xyz"`` or ``"n. Write it to foo/bar"``.
    The comment keeps its original case and is only cleared of leading
    separators, so trailing punctuation survives.

    Returns ``None`` when *raw* is not a recognisable answer (e.g. ``"yes"``
    or ``"nope"``, where the letter is not followed by a separator), so the
    caller can re-prompt.
    """
    text = raw.strip()
    if not text:
        return "", ""
    letter = text[0].lower()
    if letter not in _CHOICE_MAP:
        return None
    rest = text[1:]
    if not rest:
        return letter, ""
    if rest[0] not in _COMMENT_SEPARATORS:
        return None
    comment = rest.lstrip(_COMMENT_SEPARATORS).rstrip()
    return letter, comment


class Permissions:
    """Persistent + session permission policy for one project.  No I/O."""

    def __init__(self, state_dir: str, project_root: str):
        self.state_dir = state_dir
        self.project_key = os.path.realpath(project_root)
        self.path = os.path.join(state_dir, "permissions.json")
        self._data = self._load()
        self._session: dict[str, dict[str, str]] = {}

    # -- persistence --------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        return {"projects": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- lookup -------------------------------------------------------------

    def _proj(self) -> dict:
        return self._data.setdefault("projects", {}).setdefault(self.project_key, {})

    def _session_proj(self) -> dict:
        return self._session.setdefault(self.project_key, {})

    # -- public API ---------------------------------------------------------

    def decide(self, capability: str):
        """Return the stored decision for *capability*, or ``None`` to ask.

        Persistent (per-workspace) decisions take precedence over
        session-scoped ones, matching the historical order.  ``None`` means
        no stored policy covers this capability yet, so the caller must
        raise a :class:`~miniagent.events.PermissionNeeded` event and feed
        the answer back through :meth:`apply_answer`.

        An unknown capability is refused outright (``False``) rather than
        prompted for, so a typo in a tool's capability map can never turn
        into a question the user might approve.
        """
        if capability not in CAPABILITIES:
            return False

        proj = self._proj()
        sess = self._session_proj()

        if proj.get(capability) == ALWAYS_ALLOW:
            return True
        if proj.get(capability) == ALWAYS_DENY:
            return False
        if sess.get(capability) == SESSION_ALLOW:
            return True
        if sess.get(capability) == SESSION_DENY:
            return False
        return None

    @staticmethod
    def parse_answer(raw):
        """Parse a typed permission answer into a ``PermissionAnswer``.

        Accepts a bare choice letter (``"y"``), a blank answer (deny-once,
        the historical behaviour), or a letter followed by a separator and a
        free-text comment for the agent (``"n. Write it to foo/bar"``).
        Returns ``None`` when *raw* is not a recognisable answer (``"yes"``,
        ``"nope"``), so a front end can re-prompt.

        This is deliberately the only place that knows what the letters
        mean: front ends collect the keystroke and hand it straight here.
        """
        parsed = _parse_choice(raw)
        if parsed is None:
            return None
        letter, comment = parsed
        return PermissionAnswer(_CHOICE_MAP[letter], comment)

    def apply_answer(self, capability: str, answer) -> tuple[bool, str]:
        """Record *answer* for *capability* and return ``(allowed, comment)``.

        Session and persistent decisions are stored so the same capability
        is not asked about again; once-decisions are not stored.  A missing
        or malformed answer (including ``None``, which is what a driver
        sends when its front end could not ask) is treated as a deny-once —
        the same safe default a blank answer has always had.
        """
        if capability not in CAPABILITIES:
            return False, ""
        if not isinstance(answer, PermissionAnswer):
            return False, ""
        return self._apply(capability, answer.decision), answer.comment

    def _apply(self, capability: str, decision: str) -> bool:
        """Persist/record *decision* and return whether it is an allow."""
        if decision == ONCE_ALLOW:
            return True
        if decision == ONCE_DENY:
            return False
        if decision == SESSION_ALLOW:
            self._session_proj()[capability] = SESSION_ALLOW
            return True
        if decision == SESSION_DENY:
            self._session_proj()[capability] = SESSION_DENY
            return False
        if decision == ALWAYS_ALLOW:
            self._proj()[capability] = ALWAYS_ALLOW
            self._save()
            return True
        if decision == ALWAYS_DENY:
            self._proj()[capability] = ALWAYS_DENY
            self._save()
            return False
        return False

    # -- introspection ------------------------------------------------------

    def describe(self) -> str:
        proj = self._proj()
        sess = self._session_proj()
        lines = [f"Project: {self.project_key}"]
        for cap in CAPABILITIES:
            persistent = proj.get(cap, "-")
            session = sess.get(cap, "-")
            lines.append(f"  {cap}: persistent={persistent} session={session}")
        return "\n".join(lines)

    def summary(self) -> dict:
        """Return a JSON-serialisable snapshot, like the old jeb.py."""
        return {
            "session": self._session_proj(),
            "persistent": self._proj(),
        }

    def summary_json(self) -> str:
        """Return the permission snapshot as indented JSON."""
        return json.dumps(self.summary(), indent=2)

    def clear(self):
        """Clear both session and persistent permissions for this project."""
        self._session.pop(self.project_key, None)
        self._data.get("projects", {}).pop(self.project_key, None)
        self._save()
