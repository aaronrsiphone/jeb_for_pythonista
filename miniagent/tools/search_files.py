"""``search_files``: search file contents inside the project workspace."""

from .registry import tool


@tool(params={
    "pattern": "Text or regular expression to search for.",
    "path": "Relative directory to search. Defaults to '.'.",
    "regex": "Interpret pattern as a regular expression.",
    "include": "Optional glob filter on file names, e.g. '*.py'.",
    "max_results": (
        "Cap on returned matching lines (default 200). The result carries "
        "a truncated flag when the cap is hit."
    ),
}, overrides={
    "case_sensitive": {"default": False},
    "regex": {"default": False},
})
def search_files(ctx, pattern: str, path: str = ".", case_sensitive: bool = False,
                  regex: bool = False, include: str = None, max_results: int = None) -> dict:
    """Search the contents of files inside the project workspace for a
    pattern, returning matching lines with file path and line number.
    Case-insensitive literal search by default; set regex=true for
    regular expressions. Skips non-UTF-8 and very large files."""
    return ctx.workspace.search_files(
        pattern,
        path=path,
        case_sensitive=case_sensitive,
        regex=regex,
        include=include,
        max_results=max_results,
    )
