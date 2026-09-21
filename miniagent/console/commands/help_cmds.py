"""``:help`` and ``:quit`` (plus its ``:q``/``:exit`` aliases)."""

from __future__ import annotations

from ..registry import QUIT, command, render_help


@command("help")
def help_command(ctx, arg):
    """Show commands."""
    print(render_help())


@command("quit", aliases=("q", "exit"))
def quit_command(ctx, arg):
    """Stop MiniAgent normally. This does not invoke exit()."""
    print("MiniAgent stopped.")
    return QUIT
