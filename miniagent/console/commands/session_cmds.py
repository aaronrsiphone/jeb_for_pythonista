"""Conversation and self-editing safety-net commands.

``:reset``, ``:resume``, ``:undo`` and ``:checkpoints`` — everything that
acts on the running conversation or the turn-checkpoint history, as opposed
to configuration or workspace inspection.
"""

from __future__ import annotations

from .. import resume as _resume
from ..registry import command


@command("reset")
def reset_command(ctx, arg):
    """Reset conversation context."""
    ctx.agent.reset()
    print("Conversation reset.")


@command("resume")
def resume_command(ctx, arg):
    """List recorded sessions for this workspace, 4 per page, and reinstate
    the chosen session's message history so the conversation picks up
    where it left off. Pick with 1-4, turn pages with 0 (previous) and
    5 (next), or press Enter to cancel. New messages are appended to the
    resumed session's log.
    """
    _resume.resume_command(ctx.agent, ctx.session_logger)


@command("undo")
def undo_command(ctx, arg):
    """Restore every file the last turn touched to its state from before that
    turn (a create_file is undone by deleting the file it created). Asks
    for confirmation first, since it discards whatever is on disk now.
    Undoing again goes one turn further back.
    """
    checkpoints = ctx.checkpoints
    if checkpoints is None:
        print("Checkpoints are not available in this session.")
        return
    groups = checkpoints.list_groups()
    if not groups:
        print("Nothing to undo.")
        return
    latest = groups[0]
    print(f"This will undo turn {latest['turn_id']} ({len(latest['files'])} file(s)):")
    for path in latest["files"]:
        print(f"  {path}")
    try:
        confirm = input("Undo? This discards current file state. [y/N]: ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm != "y":
        print("Cancelled.")
        return
    report = checkpoints.undo_last()
    if report is None:
        print("Nothing to undo.")
        return
    for path in report["restored"]:
        print(f"  restored: {path}")
    for path in report["deleted"]:
        print(f"  deleted:  {path}")
    for err in report["errors"]:
        print(f"  error:    {err['path']} ({err['error']})")
    print(f"Undid turn {report['turn_id']}: "
          f"{len(report['restored'])} restored, "
          f"{len(report['deleted'])} deleted, "
          f"{len(report['errors'])} error(s).")


@command("checkpoints")
def checkpoints_command(ctx, arg):
    """List the retained undo groups (newest first): turn id, timestamp and
    the files each one touched.
    """
    checkpoints = ctx.checkpoints
    if checkpoints is None:
        print("Checkpoints are not available in this session.")
        return
    groups = checkpoints.list_groups()
    if not groups:
        print("No checkpoints recorded yet.")
        return
    print(f"Retained checkpoints for this workspace (newest first, "
          f"{len(groups)} of {checkpoints.retention} max):")
    for group in groups:
        print(f"  {group['turn_id']}  ({group['started']})  "
              f"{len(group['files'])} file(s)")
        for path in group["files"]:
            print(f"      {path}")
