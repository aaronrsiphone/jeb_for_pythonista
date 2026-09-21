"""Compact terminal renderer — the default, tuned for a phone screen.

Pythonista's console is roughly forty columns wide and a dozen lines tall, so
fixed overhead is the enemy: a five-tool turn used to cost about ninety lines,
almost all of it boilerplate.  This renderer keeps one line per tool call, one
line per reasoning step, and shows the permission legend once per session
instead of before every prompt.  The formats here were tuned against real
phone output and are reproduced verbatim from the pre-event-stream engine —
change them deliberately, not incidentally.

The permission prompt is the only place a renderer blocks on the user.  The
letter semantics are *not* implemented here: the typed answer goes straight to
``Permissions.parse_answer``, which owns them, so a second front end can never
disagree with the first about what ``"d"`` means.
"""

from __future__ import annotations

from ..events import (
    AssistantText,
    PermissionNeeded,
    ReasoningChunk,
    StepLimitReached,
    ToolCompleted,
    ToolStarted,
    TurnEnded,
    TurnFailed,
)
from ..permissions import Permissions

# Maximum characters of the permission details preview shown to the user.
MAX_DETAIL_CHARS = 10_000

# Reasoning longer than this collapses to a character count instead of being
# printed.  The agent always yields the full text; the decision lives here.
REASONING_INLINE_LIMIT = 200

# Rule width used by the first (full) permission prompt.
RULE_WIDTH = 68


def trunc(value, limit: int = MAX_DETAIL_CHARS) -> str:
    """Truncate a string to *limit*, appending a notice when cut."""
    text = str(value)
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return text[:limit] + f"\n... [{removed} chars truncated]"


def print_legend():
    """Print the six choice-letter lines plus the comment-syntax hint.

    Shared by the first prompt of a session, by ``?`` at any prompt, and by
    the verbose renderer (which shows it every time), so the wording lives in
    exactly one place.
    """
    print("y  allow once")
    print("s  allow this capability for this session")
    print("a  always allow for this workspace")
    print("n  deny once")
    print("d  deny this capability for this session")
    print("x  always deny for this workspace")
    print()
    print("You may append a comment for the agent after any choice, e.g.")
    print('  "y. But also can you check xyz"  or  "n. Write it to foo/bar"')


def read_line(prompt: str) -> str:
    """Read one line, returning ``""`` at EOF.

    A blank answer parses to deny-once, so EOF (a piped stdin, a closed
    console) safely refuses rather than looping or raising.  Looked up through
    the builtin at call time so tests can stub ``builtins.input``.
    """
    try:
        return input(prompt)
    except EOFError:
        return ""


class Console:
    """Compact renderer: one line per event, legend once per instance."""

    def __init__(self):
        # Per-instance, not per-turn: the legend is boilerplate the user has
        # already read, so it is shown once for the life of the front end.
        self._legend_shown = False

    # -- the renderer seam --------------------------------------------------

    def handle(self, event):
        """Render *event*; return an answer for it, or ``None``.

        Only :class:`~miniagent.events.PermissionNeeded` produces a value;
        everything else is informational and returns ``None``, which the
        driver sends back into the generator.
        """
        if isinstance(event, ReasoningChunk):
            self.on_reasoning(event)
        elif isinstance(event, AssistantText):
            self.on_assistant_text(event)
        elif isinstance(event, ToolStarted):
            self.on_tool_started(event)
        elif isinstance(event, ToolCompleted):
            self.on_tool_completed(event)
        elif isinstance(event, StepLimitReached):
            self.on_step_limit(event)
        elif isinstance(event, TurnFailed):
            self.on_turn_failed(event)
        elif isinstance(event, TurnEnded):
            self.on_turn_ended(event)
        elif isinstance(event, PermissionNeeded):
            return self.on_permission(event)
        return None

    # -- informational events -----------------------------------------------

    def on_reasoning(self, event):
        text = event.text
        if len(text) > REASONING_INLINE_LIMIT:
            print(f"· thinking ({len(text)} chars)")
        else:
            # Flatten newlines so a short chain of thought stays one line.
            print(f"· {' '.join(text.split())}")

    def on_assistant_text(self, event):
        # Bare, with no "Assistant:" header and no leading blank line: on a
        # phone the reply is almost always what the user is looking at.
        print(event.text)

    def on_tool_started(self, event):
        # Nothing: the compact format prints a single line per tool call once
        # it completes, so request and result do not cost two lines each.
        pass

    def on_tool_completed(self, event):
        comment = str(event.comment or "").strip()
        note = f" — {comment}" if comment else ""
        print(f"→ {event.name}  {event.status}{note}")

    def on_step_limit(self, event):
        print("Per-turn agent step limit reached.")

    def on_turn_failed(self, event):
        print("Provider error:")
        print(event.error)

    def on_turn_ended(self, event):
        # Nothing: the final text already arrived as an AssistantText event.
        pass

    # -- the interactive event ----------------------------------------------

    def on_permission(self, event):
        """Show the request, then read and parse an answer."""
        prompt = self._show_prompt(event)
        return self._read_answer(prompt)

    def _show_prompt(self, event) -> str:
        """Print the request header; return the input prompt string."""
        if not self._legend_shown:
            # First prompt of this instance's lifetime: show everything,
            # including the choice-letter legend.
            print()
            print("=" * RULE_WIDTH)
            print(f"PERMISSION REQUEST: {event.title}")
            print(f"Capability: {event.capability}")
            print("-" * RULE_WIDTH)
            print(trunc(event.details))
            print("-" * RULE_WIDTH)
            print_legend()
            self._legend_shown = True
            return "Permission [y/s/a/n/d/x]: "

        # Subsequent prompts: compact header, no rules, no legend.  "?"
        # reprints the legend for anyone who needs it back.
        print(f"PERMISSION  {event.capability}  {event.title}")
        print(trunc(event.details))
        return "[y/s/a/n/d/x ?] "

    def _read_answer(self, prompt: str):
        """Loop until the user gives something ``parse_answer`` understands."""
        while True:
            raw = read_line(prompt)

            if raw.strip() == "?":
                # Not an answer — reprint the legend and ask again.
                print_legend()
                continue

            answer = Permissions.parse_answer(raw)
            if answer is None:
                print("Unrecognised answer. Use one of y/s/a/n/d/x, optionally")
                print('followed by a comment, e.g. "n. Use another path".')
                continue

            return answer
