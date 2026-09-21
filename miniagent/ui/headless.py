"""Silent renderer with scripted answers — for tests and automation.

This is the renderer that justifies the event stream.  Before it existed, a
test that exercised a permission-gated tool had to monkeypatch
``builtins.input`` and scrape stdout; ``docs/testing.md`` documented that
workaround as the supported technique.  Here a turn is just a list of events
and a list of answers, so a test asserts on typed objects.

It is also the front end for any non-interactive run (a scheduled task, a
notification handler): drive a turn with no answers and every gated tool is
denied, which is the safe outcome when nobody is watching.
"""

from __future__ import annotations

from ..events import PermissionAnswer, PermissionNeeded, TurnEnded, TurnFailed
from ..permissions import Permissions


class Headless:
    """Record every event, print nothing, answer from a scripted queue.

    *answers* is consumed in order, one entry per
    :class:`~miniagent.events.PermissionNeeded`.  Each entry is either a raw
    typed answer (``"y"``, ``"n. use another path"``), parsed by
    ``Permissions.parse_answer`` exactly as the console renderer parses
    keystrokes, or an already-built
    :class:`~miniagent.events.PermissionAnswer` for a test that wants to skip
    the parsing.

    When the queue is empty ``handle`` returns ``None``, and so does an entry
    the parser does not recognise.  The engine treats ``None`` as a deny-once:
    that is the safe default, it matches what a blank answer has always meant
    at the console, and it makes "what happens when nobody answers?" a thing a
    test can assert on rather than a thing that hangs.
    """

    def __init__(self, answers=None):
        #: Every event handed to this renderer, in order.  Public on purpose.
        self.events = []
        self._answers = list(answers or [])

    # -- the renderer seam --------------------------------------------------

    def handle(self, event):
        self.events.append(event)
        if isinstance(event, PermissionNeeded):
            return self._next_answer()
        return None

    def _next_answer(self):
        if not self._answers:
            return None  # nobody left to ask: deny-once
        raw = self._answers.pop(0)
        if isinstance(raw, PermissionAnswer):
            return raw
        # parse_answer returns None for unrecognisable input; a headless run
        # has no one to re-prompt, so that also becomes a deny-once.
        return Permissions.parse_answer(raw)

    # -- accessors for tests ------------------------------------------------

    @property
    def answers_left(self) -> int:
        """How many scripted answers have not been consumed."""
        return len(self._answers)

    def events_of(self, cls):
        """Return the recorded events that are instances of *cls*."""
        return [event for event in self.events if isinstance(event, cls)]

    @property
    def text(self) -> str:
        """The terminal event's text, or ``""`` if the turn has not ended.

        Mirrors what :func:`miniagent.ui.drive` returns, so a test can use
        either without caring which one it kept a reference to.
        """
        for event in reversed(self.events):
            if isinstance(event, (TurnEnded, TurnFailed)):
                return event.text
        return ""
