"""``:perms`` and ``:clear-perms``."""

from __future__ import annotations

from ..registry import command


@command("perms")
def perms_command(ctx, arg):
    """Show permission state."""
    print(ctx.permissions.summary_json())


@command("clear-perms")
def clear_perms_command(ctx, arg):
    """Clear session and persistent permissions."""
    try:
        confirm = input("Clear permissions? [y/N]: ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm == "y":
        ctx.permissions.clear()
        print("Permissions cleared.")
