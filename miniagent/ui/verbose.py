"""Expanded renderer — the debugging / desktop view.

The compact renderer trims everything a phone cannot afford: reasoning becomes
a character count, tool arguments vanish, and the permission legend is shown
once.  On a real terminal, while debugging a tool or a prompt, those are
exactly the things you want back.  This renderer restores them and reproduces
the transcript the engine produced before the mobile pass (blank-line spacing,
``Reasoning:`` / ``Assistant:`` headers, ``Tool request:`` / ``Tool result:``
pairs), with the arguments a tool was actually called with added — the one
thing the old format never showed.

It subclasses :class:`~miniagent.ui.console.Console` and overrides only the
per-event methods.  The permission prompt loop is inherited untouched, so the
two renderers can never drift apart on how an answer is parsed.
"""

from __future__ import annotations

import json

from .console import Console, trunc

# Tool arguments are shown inline on one line, so they are clipped harder than
# a permission preview: enough to see which file or command was meant.
MAX_ARG_CHARS = 400


def format_args(args) -> str:
    """Render tool arguments compactly, tolerating anything unserialisable."""
    if not args:
        return "{}"
    try:
        text = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # Never let a display concern break a turn.
        text = repr(args)
    return trunc(text, MAX_ARG_CHARS)


class Verbose(Console):
    """Console renderer with nothing collapsed and nothing suppressed."""

    def on_reasoning(self, event):
        print()
        print("Reasoning:")
        print(event.text)

    def on_assistant_text(self, event):
        print()
        print("Assistant:")
        print(event.text)

    def on_tool_started(self, event):
        print()
        print(f"Tool request: {event.name}")
        print(f"  args: {format_args(event.args)}")

    def on_tool_completed(self, event):
        comment = str(event.comment or "").strip()
        note = f" — {comment}" if comment else ""
        print(f"Tool result: {event.status}{note}")

    def on_step_limit(self, event):
        print()
        print("Per-turn agent step limit reached.")

    def on_turn_failed(self, event):
        print()
        print("Provider error:")
        print(event.error)

    def _show_prompt(self, event):
        # The verbose view never suppresses boilerplate: clearing the
        # once-only flag gives every prompt the full header, the untruncated
        # rules and the legend, rather than the compact one-line form.
        self._legend_shown = False
        return super()._show_prompt(event)
