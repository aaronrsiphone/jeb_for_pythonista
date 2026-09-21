"""``multi_edit``: apply several edits to one file atomically (gated, EDIT)."""

from .. import permissions as _perm
from .registry import tool

_EDIT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
    },
    "required": ["old_text", "new_text"],
}


@tool(capability=_perm.EDIT, params={
    "edits": "Edits applied in order to the same file.",
}, overrides={
    "edits": {"items": _EDIT_ITEM_SCHEMA},
})
def multi_edit(ctx, path: str, edits: list) -> dict:
    """Apply a list of {old_text, new_text} edits to one existing file
    atomically: either every edit applies (each old_text must be a unique
    match in the file's content at the point it is applied, in order) or
    the file is left completely untouched and the error names which edit
    failed and why. One permission prompt covers the whole change instead
    of one per edit."""
    return ctx.workspace.multi_edit(path, edits)


@multi_edit.preview
def _(ctx, path: str = "", edits: list = None, **_kw):
    edits = edits or []
    preview = ctx.workspace.preview_multi_edit(path, edits)
    return "MULTI EDIT", f"{path}\n\n{preview}"
