"""``create_file``: create a new text file (gated, WRITE)."""

from .. import permissions as _perm
from .registry import tool


@tool(capability=_perm.WRITE)
def create_file(ctx, path: str, content: str) -> dict:
    """Create a new UTF-8 text file. Fails if it already exists."""
    outcome = ctx.workspace.create_file(path, content)
    ctx._record_created(path)
    return outcome


@create_file.preview
def _(ctx, path: str = "", content: str = "", **_kw):
    lines = content.splitlines()
    byte_count = len(content.encode("utf-8"))
    stat_line = f"{path}  {len(lines)} lines  {byte_count} bytes"
    head = lines[:8]
    body = "\n".join(head)
    if len(lines) > 8:
        body += f"\n... ({len(lines) - 8} more lines)"
    return "CREATE FILE", f"{stat_line}\n\n{body}"
