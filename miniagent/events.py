"""Typed events exchanged between the agent loop and a front end.

This module is the contract that decouples the engine from its user
interface.  It is a leaf: it imports nothing from the rest of the package,
so every other module (and every test) can depend on it freely.

``Agent.turn()`` is a generator that yields the :class:`Event` subclasses
below.  A *driver* pumps the generator and hands each event to a renderer
(see :mod:`miniagent.ui`).  One event — :class:`PermissionNeeded` — expects
an answer: the driver must ``send()`` a :class:`PermissionAnswer` back into
the generator.  Every other event is informational and the driver sends
``None``.

The turn protocol
-----------------

Every turn yields **exactly one terminal event**: either :class:`TurnEnded`
(the model produced a final reply, or the step limit was hit) or
:class:`TurnFailed` (the provider call failed).  A driver can therefore rely
on seeing one of the two and use it as the turn's result.  A typical
sequence::

    ReasoningChunk      (optional, one per model step that reasoned)
    AssistantText       (optional, one per model step that emitted text)
    ToolStarted         \\
    PermissionNeeded     |  per tool call, PermissionNeeded only when the
    ToolCompleted       /   stored policy does not already decide it
    ...                     (loop continues while the model calls tools)
    TurnEnded

Why events instead of ``print()``
---------------------------------

Before this existed, the engine printed directly and read permission
answers with ``input()`` from six frames below the console loop.  That made
the output format a property of the agent loop, made a non-TTY front end
impossible, and forced tests to monkeypatch ``builtins.input``.  Everything
the engine wants to *say* is now an event, and everything it wants to *ask*
is an event with an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Event:
    """Base class for everything the agent loop yields.

    Exists so a renderer can type-check against a single base and so
    ``isinstance(obj, Event)`` distinguishes engine events from the
    :class:`PermissionAnswer` that travels the other way.
    """


# -- informational events ---------------------------------------------------


@dataclass(frozen=True)
class ReasoningChunk(Event):
    """The model's reasoning/thinking text for one step.

    Normalised by ``Provider.split_content`` from any of the three shapes a
    provider may use (a ``reasoning_content`` field, a ``reasoning`` field,
    or typed thinking blocks inside the content list).  Renderers decide
    whether to show it in full, collapse it to a line, or drop it.
    """

    text: str


@dataclass(frozen=True)
class AssistantText(Event):
    """Assistant prose for one step (not the final-reply marker).

    A step that also calls tools can still emit text; this event carries it
    either way.  The turn's final text arrives again on :class:`TurnEnded`.
    """

    text: str


@dataclass(frozen=True)
class ToolStarted(Event):
    """A tool call is about to be dispatched.

    Yielded before any permission check, so a renderer can show what is
    being attempted even if it is then denied.  The compact renderer
    ignores this and prints one line on :class:`ToolCompleted` instead; the
    verbose renderer prints both.
    """

    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCompleted(Event):
    """A tool call finished (or was refused).

    *status* is one of ``"ok"``, ``"denied"``, ``"blocked"`` or ``"error"``,
    derived from the result payload exactly as the old status line was.
    *comment* is the note the user attached to a permission answer, if any.
    *payload* is the parsed result dict (empty when it could not be parsed).
    """

    name: str
    status: str
    comment: str = ""
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StepLimitReached(Event):
    """The per-turn step budget was exhausted.

    Informational, not terminal: a :class:`TurnEnded` follows it.
    """

    limit: int


# -- terminal events --------------------------------------------------------


@dataclass(frozen=True)
class TurnEnded(Event):
    """The turn finished normally.  *text* is the assistant's final reply.

    *usage* is the token accounting accumulated across every model call in
    this turn (provider-shaped; empty when the provider reported none).
    """

    text: str
    usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TurnFailed(Event):
    """The turn could not complete because a provider call failed.

    *text* is the synthetic assistant message recorded in its place, so the
    conversation and the session log stay balanced.
    """

    error: str
    text: str = ""


# -- the one event that expects an answer -----------------------------------


@dataclass(frozen=True)
class PermissionNeeded(Event):
    """A gated tool needs authorization that stored policy does not cover.

    The driver must ``send()`` a :class:`PermissionAnswer` back into the
    generator.  Sending ``None`` (or anything else) is treated as a
    deny-once, matching the historical behaviour of a blank answer at the
    console prompt.

    *title* is a short uppercase label (``"EDIT FILE"``); *details* is the
    multi-line preview of what the tool is about to do.
    """

    capability: str
    title: str
    details: str


@dataclass(frozen=True)
class PermissionAnswer:
    """A user's response to :class:`PermissionNeeded`.

    *decision* is one of the internal decision tokens in
    :mod:`miniagent.permissions` (``ONCE_ALLOW``, ``SESSION_DENY``, ...).
    Front ends do not build these by hand: they pass the raw typed answer
    to ``Permissions.parse_answer()``, which owns the letter semantics and
    the comment-splitting, and hand the result back.

    *comment* is the free-text note the user appended to their answer (the
    redirect in ``"n. Write it to foo/bar"``), relayed to the model in the
    tool result.
    """

    decision: str
    comment: str = ""
