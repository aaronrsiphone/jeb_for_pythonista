"""Permission layer for MiniAgent tools.

Permissions are keyed by the canonical path of the project so that distinct
projects can have distinct persistent decisions.  The permission store lives
in the application-state directory, never inside a user project, so the agent
cannot edit its own permission policy through the file tools.
"""

from __future__ import annotations

import json
import os
from typing import Callable

# Distinct capabilities.
WRITE = "write"
EDIT = "edit"
OVERWRITE = "overwrite"
RUN_PYTHON = "run_python"

CAPABILITIES = (WRITE, EDIT, OVERWRITE, RUN_PYTHON)

# Internal decision tokens.  The user-facing prompt uses single-letter
# choices (y/s/a/n/d/x) that mirror the original jeb.py.
ONCE_ALLOW = "once_allow"
ONCE_DENY = "once_deny"
SESSION_ALLOW = "session_allow"
SESSION_DENY = "session_deny"
ALWAYS_ALLOW = "always_allow"
ALWAYS_DENY = "always_deny"

# Maximum characters of the permission details preview shown to the user.
_MAX_DIFF_CHARS = 10_000


def _trunc(value, limit):
    """Truncate a string to *limit*, appending a notice when cut."""
    text = str(value)
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return text[:limit] + f"\n... [{removed} chars truncated]"


class Permissions:
    """Permission store with interactive prompting."""

    def __init__(self, state_dir: str, project_root: str, prompt: Callable[[str], str] | None = None):
        self.state_dir = state_dir
        self.project_key = os.path.realpath(project_root)
        self.path = os.path.join(state_dir, "permissions.json")
        self._prompt = prompt or _default_prompt
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

    def authorize(self, capability: str, title: str, details: str) -> bool:
        """Return True if *capability* is allowed, possibly after prompting.

        Mirrors the original jeb.py ``PermissionManager.authorize`` signature
        and prompt: *title* is a short uppercase label (e.g. ``"CREATE FILE"``)
        and *details* is a multi-line preview shown beneath a rule.
        """
        if capability not in CAPABILITIES:
            return False

        proj = self._proj()
        sess = self._session_proj()

        # Persistent decisions take precedence.
        if proj.get(capability) == ALWAYS_ALLOW:
            return True
        if proj.get(capability) == ALWAYS_DENY:
            return False

        # Session-scoped decisions.
        if sess.get(capability) == SESSION_ALLOW:
            return True
        if sess.get(capability) == SESSION_DENY:
            return False

        # Interactive prompt.
        choice = self._ask(capability, title, details)
        return self._apply(capability, choice)

    # Backwards-compatible alias used by older callers.
    def check(self, capability: str, context: str = "") -> bool:
        return self.authorize(capability, capability.upper(), context)

    # -- prompting ----------------------------------------------------------

    def _ask(self, capability: str, title: str, details: str) -> str:
        print()
        print("=" * 68)
        print(f"PERMISSION REQUEST: {title}")
        print(f"Capability: {capability}")
        print("-" * 68)
        print(_trunc(details, _MAX_DIFF_CHARS))
        print("-" * 68)
        print("y  allow once")
        print("s  allow this capability for this session")
        print("a  always allow for this workspace")
        print("n  deny once")
        print("d  deny this capability for this session")
        print("x  always deny for this workspace")

        while True:
            raw = self._prompt("Permission [y/s/a/n/d/x]: ").strip().lower()

            mapping = {
                "y": ONCE_ALLOW,
                "s": SESSION_ALLOW,
                "a": ALWAYS_ALLOW,
                "": ONCE_DENY,
                "n": ONCE_DENY,
                "d": SESSION_DENY,
                "x": ALWAYS_DENY,
            }

            if raw in mapping:
                return mapping[raw]

            # Unknown input — re-prompt.
            continue

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


def _default_prompt(message: str) -> str:
    try:
        return input(message)
    except EOFError:
        return ""
