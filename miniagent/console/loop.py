"""The interactive console loop.

Parses each typed line into a command word plus the remaining argument text,
looks the word up in the :mod:`.registry` (honouring aliases), and calls its
handler with the live :class:`~miniagent.context.Context`. Anything not
starting with ``:`` is a prompt for the agent, pumped through
``miniagent.ui.drive``.

Importing :mod:`.commands` (for its side effect of registering every command)
happens here, once, so that importing this module is enough to make the full
command set available — the same convention :mod:`miniagent.tools` uses for
tools.
"""

from __future__ import annotations

from ..sessions import repair_messages
from ..ui import drive
from . import commands  # noqa: F401  (registers every command)
from .registry import QUIT, get_spec, render_command_list

_BANNER = """
=================================================
 MiniAgent — coding agent for: {root}
 Endpoint: {endpoint}
 Model: {model}
 Commands:
{commands}
=================================================
"""


def _banner(ctx) -> str:
    provider = ctx.provider
    endpoint = provider.base_url.rstrip("/") + "/" + provider.chat_path.lstrip("/")
    config = ctx.config
    model_label = (
        f"{config.provider}/{config.model}" if config.provider else config.model
    )
    return _BANNER.format(
        root=ctx.root,
        endpoint=endpoint,
        model=model_label,
        commands=render_command_list(),
    )


def _split_command(line: str):
    """Split a ``:``-prefixed line into (word, remaining-argument-text)."""
    word, _, rest = line[1:].partition(" ")
    return word, rest.strip()


def console_loop(ctx):
    """Run the interactive prompt loop until ``:quit`` or EOF.

    *ctx* is the live :class:`~miniagent.context.Context`; command handlers
    mutate it in place (switching provider, renderer, and so on) rather than
    returning replacements, so this loop's own state is just the loop.
    """
    print(_banner(ctx))

    while True:
        try:
            line = input("You> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            print("Use :quit to stop MiniAgent cleanly.")
            continue
        if not line:
            continue

        if line.startswith(":"):
            word, arg = _split_command(line)
            spec = get_spec(word)
            if spec is None:
                print(f"Unknown command: :{word}  (:help lists commands)")
                continue
            if spec(ctx, arg) is QUIT:
                break
            continue

        # Anything else is a prompt for the agent.  drive() pumps the
        # agent's event generator into the active renderer.
        if ctx.checkpoints is not None:
            # One snapshot group per turn: every mutation the agent makes
            # answering this prompt is undoable as a unit with :undo.
            ctx.checkpoints.begin_turn()
        try:
            drive(ctx.agent, line, ctx.renderer)
        except KeyboardInterrupt:
            print("\n[interrupted]")
            # The stop button can land mid-tool-loop, after an assistant
            # tool_calls message has already been appended but before its
            # results come back — heal the tail the same way Agent.turn()
            # does defensively, so the next prompt doesn't 400 (§1.4).
            if len(ctx.agent.messages) > 1:
                ctx.agent.messages = (ctx.agent.messages[:1]
                                       + repair_messages(ctx.agent.messages[1:]))
            continue
        print()
