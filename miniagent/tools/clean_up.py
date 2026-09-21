"""``clean_up``: move this session's own scratch files into to_delete.

Ungated: it may only move files this same session created with
``create_file`` (tracked on ``ctx._session_created``), so the agent can only
tidy away its own debris, and the move into the workspace's ``to_delete``
folder is recoverable rather than destructive.
"""

from ..workspace import WorkspaceError
from .registry import tool


@tool(params={
    "paths": "Relative paths of files to clean up.",
})
def clean_up(ctx, paths: list) -> dict:
    """Move scratch files you created with create_file during this session
    into the workspace to_delete folder (a recoverable trash area - nothing
    is permanently deleted). Use this at the end of a task to tidy away
    temporary files you no longer need. Only files created in the current
    session are accepted; anything else is refused and listed under
    'skipped' with a reason."""
    if (not isinstance(paths, list) or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)):
        raise WorkspaceError(
            "clean_up requires a non-empty 'paths' list of relative file paths"
        )

    trash = ctx.workspace.trash_dir()
    moved, skipped, seen = [], [], set()
    for raw in paths:
        rel = raw.strip()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            full = ctx.workspace.resolve(rel)
        except WorkspaceError as exc:
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        key = str(full)
        if full == trash or trash in full.parents:
            skipped.append({"path": rel, "reason": "path is inside to_delete"})
        elif key in ctx._session_cleaned:
            skipped.append({"path": rel, "reason": "already cleaned up this session"})
        elif key not in ctx._session_created:
            skipped.append({
                "path": rel,
                "reason": "not created with create_file in this session",
            })
        elif not full.exists():
            skipped.append({"path": rel, "reason": "no longer exists"})
        elif not full.is_file():
            skipped.append({"path": rel, "reason": "not a file"})
        else:
            outcome = ctx.workspace.clean_up(rel)
            ctx._session_created.discard(key)
            ctx._session_cleaned.add(key)
            moved.append({
                "path": outcome["path"],
                "moved_to": outcome["moved_to"],
            })
    return {"ok": True, "moved": moved, "skipped": skipped}
