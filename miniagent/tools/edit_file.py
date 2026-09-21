"""``edit_file``: replace one unique text occurrence (gated, EDIT)."""

from .. import permissions as _perm
from .registry import tool


@tool(capability=_perm.EDIT, params={
    "replace_all": "Replace every occurrence of old_text.",
    "occurrence": (
        "Replace only the Nth (1-based) occurrence of old_text. Mutually "
        "exclusive with replace_all."
    ),
}, overrides={
    "replace_all": {"default": False},
})
def edit_file(ctx, path: str, old_text: str, new_text: str,
              replace_all: bool = False, occurrence: int = None) -> dict:
    """Replace text in an existing file. By default old_text must occur
    exactly once; a non-unique match reports every occurrence's line
    number instead of just failing, and no match reports the
    closest-matching region of the file with a diff of what differs. Set
    replace_all=true to replace every occurrence, or occurrence=<1-based
    index> to pick one specific occurrence (mutually exclusive with
    replace_all)."""
    return ctx.workspace.edit_file(
        path, old_text, new_text, replace_all=replace_all, occurrence=occurrence
    )


@edit_file.preview
def _(ctx, path: str = "", old_text: str = "", new_text: str = "",
      replace_all: bool = False, occurrence: int = None, **_kw):
    preview = ctx.workspace.preview_edit(
        path, old_text, new_text, replace_all=replace_all, occurrence=occurrence
    )
    return "EDIT FILE", preview
