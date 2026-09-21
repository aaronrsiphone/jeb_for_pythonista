"""``read_file``: read a text file with line numbers."""

from .registry import tool


@tool()
def read_file(ctx, path: str, start_line: int = None, end_line: int = None) -> dict:
    """Read a UTF-8 text file with line numbers."""
    return {
        "content": ctx.workspace.read_file(path, start_line=start_line, end_line=end_line)
    }
