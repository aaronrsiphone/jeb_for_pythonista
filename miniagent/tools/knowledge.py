"""``knowledge``: read-only access to this install's Pythonista reference
material. Ungated: no action writes a file or executes code."""

from ..knowledge import KnowledgeError
from .registry import tool

_ACTIONS = ("list", "read", "search", "docs")


def _required_arg(value, key: str, action: str) -> str:
    """Return the non-empty string argument *key*, or raise a clean error."""
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(
            f"knowledge action '{action}' requires a '{key}' argument"
        )
    return value


@tool(params={
    "action": "What to consult.",
    "path": "Knowledge file (list/read) or directory (search), relative to the knowledge root.",
    "pattern": "search: text or regex to find.",
    "include": "search: glob filter on file names.",
    "query": "docs: symbol to find in the documentation index (substring match).",
    "page": "docs: doc page to read, e.g. 'ui' or 'py3/ios/appex.html'.",
}, overrides={
    "action": {"enum": list(_ACTIONS)},
    "case_sensitive": {"default": False},
    "regex": {"default": False},
})
def knowledge(ctx, action: str, path: str = None, start_line: int = None,
              end_line: int = None, pattern: str = None, case_sensitive: bool = False,
              regex: bool = False, include: str = None, max_results: int = None,
              query: str = None, page: str = None) -> dict:
    """Read-only access to this install's Pythonista reference material; no
    permission required. Actions: 'list' (index the files under the
    optional, user-populated miniagent/knowledge/ directory, if present),
    'read' (line-numbered contents of one such file), 'search' (literal or
    regex across those files), 'docs' (offline search of Pythonista's
    bundled official documentation: 'query' returns ranked symbol matches,
    'page' returns a doc page's readable text, no arguments lists the
    Pythonista module doc pages). 'list'/'read'/'search' report a clear
    error when the knowledge directory is absent; 'docs' is independent of
    it and always works. No action writes files or executes code.
    Knowledge paths are relative to the knowledge root and cannot escape
    it."""
    kb = ctx._knowledge_base()

    if action == "list":
        return {"root": str(kb.root), "entries": kb.list(path or ".")}

    if action == "read":
        target = _required_arg(path, "path", action)
        return {
            "root": str(kb.root),
            "content": kb.read(target, start_line=start_line, end_line=end_line),
        }

    if action == "search":
        return kb.search(
            _required_arg(pattern, "pattern", action),
            path=path or ".",
            case_sensitive=case_sensitive,
            regex=regex,
            include=include,
            max_results=max_results,
        )

    if action == "docs":
        return kb.docs(query=query, page=page)

    raise KnowledgeError(
        "knowledge requires an 'action' of 'list', 'read', 'search' or 'docs'"
    )
