"""``list_files``: list a directory inside the project workspace."""

from .registry import tool


@tool(params={
    "path": "Relative path within the workspace. Defaults to '.'.",
    "max_depth": "Maximum depth when recursive.",
}, overrides={
    "recursive": {"default": False},
})
def list_files(ctx, path: str = ".", recursive: bool = False, max_depth: int = None) -> dict:
    """List files and directories inside the project workspace."""
    entries = ctx.workspace.list_files(path, recursive=recursive, max_depth=max_depth)
    return {"entries": entries}
