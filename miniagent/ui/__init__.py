"""Front ends for the agent's event stream.

The engine no longer prints or prompts.  ``Agent.turn()`` is a generator that
yields :mod:`miniagent.events` events, and a *renderer* decides what (if
anything) a human sees.  This package holds the renderers and the single pump
that connects the two, so the choice of interface — a phone-sized terminal, a
verbose desktop log, a silent test harness — is a constructor argument rather
than an edit to the agent loop.

The renderer seam is one method::

    class Renderer:
        def handle(self, event): ...

``handle`` is called once per event, in order.  It returns
``None`` for informational events and a
:class:`~miniagent.events.PermissionAnswer` for
:class:`~miniagent.events.PermissionNeeded` — whatever it returns is
``send()``-ed back into the generator.  A renderer that cannot ask (no TTY, an
exhausted script) returns ``None``, which the engine treats as a deny-once.
That is the whole contract: no base class to inherit, no registration, no
lifecycle hooks.  Renderers hold their own state (the console renderer
remembers whether it has shown the permission legend yet), so a front end
creates one instance and reuses it across turns.
"""

from __future__ import annotations

from ..events import TurnEnded, TurnFailed
from .console import Console
from .headless import Headless
from .verbose import Verbose

# Name -> renderer class.  Names are what a user or a config file says
# ("--ui verbose"), so they are part of the CLI surface: keep them stable.
RENDERERS = {
    "console": Console,
    "verbose": Verbose,
    "headless": Headless,
}

#: The renderer used when nothing asks for a specific one.  Compact output is
#: the default because the primary target is a phone screen.
DEFAULT_RENDERER = "console"

__all__ = [
    "Console",
    "Verbose",
    "Headless",
    "RENDERERS",
    "DEFAULT_RENDERER",
    "get_renderer",
    "drive",
]


def get_renderer(name: str | None = None):
    """Return a new renderer instance for *name* (default ``"console"``).

    Raises :class:`ValueError` naming the valid choices for an unknown name,
    so a typo in a config file produces a usable message instead of a
    ``KeyError``.
    """
    key = (name or DEFAULT_RENDERER).strip().lower()
    try:
        cls = RENDERERS[key]
    except KeyError:
        valid = ", ".join(sorted(RENDERERS))
        raise ValueError(f"unknown renderer {name!r}; valid names: {valid}") from None
    return cls()


def drive(agent, user_text: str, renderer) -> str:
    """Pump one turn of *agent* through *renderer*, returning its final text.

    This is the only code that knows how the generator protocol works, so
    every front end — console loop, test harness, a future ``ui.View`` — gets
    the same semantics for free:

    * each event is handed to ``renderer.handle()`` and whatever it returns is
      sent back in, which is how a :class:`~miniagent.events.PermissionAnswer`
      reaches the suspended tool call;
    * the text of the terminal event (:class:`~miniagent.events.TurnEnded` or
      :class:`~miniagent.events.TurnFailed`) is returned, so a caller that
      only wants the reply never has to inspect events at all.

    ``gen.send(None)`` on a not-yet-started generator is valid and equivalent
    to ``next(gen)``, so the first iteration needs no special case.

    A :class:`KeyboardInterrupt` (the Pythonista stop button, Ctrl-C) closes
    the generator before propagating.  Without that, an interrupted turn would
    leave the generator suspended mid-tool-loop, holding its frame alive and
    running its ``finally`` blocks at some arbitrary later collection.
    """
    gen = agent.turn(user_text)
    to_send = None
    result = ""
    try:
        while True:
            try:
                event = gen.send(to_send)
            except StopIteration:
                break
            to_send = renderer.handle(event)
            if isinstance(event, (TurnEnded, TurnFailed)):
                # The protocol promises exactly one terminal event per turn;
                # remember its text as the turn's result.
                result = event.text
    except KeyboardInterrupt:
        gen.close()
        raise
    return result
