"""``overwrite_file``: replace a whole file's contents (gated, OVERWRITE)."""

from .. import permissions as _perm
from .registry import tool


@tool(capability=_perm.OVERWRITE)
def overwrite_file(ctx, path: str, content: str) -> dict:
    """Replace all contents of an existing file."""
    return ctx.workspace.overwrite_file(path, content)


@overwrite_file.preview
def _(ctx, path: str = "", content: str = "", **_kw):
    preview = ctx.workspace.preview_overwrite(path, content)
    return "OVERWRITE FILE", preview
