"""Tool schemas, dispatch and permission gating.

Permission enforcement is centralised in ``Tools.dispatch`` so that individual
tool implementations do not each re-implement the check.
"""

from __future__ import annotations

import json

from . import permissions as _perm
from .runner import Runner, RunnerError
from .workspace import Workspace, WorkspaceError

# Tool name -> permission capability.  Read-only tools are absent here, which
# means "no permission required".
CAPABILITY_MAP = {
    "create_file": _perm.WRITE,
    "edit_file": _perm.EDIT,
    "overwrite_file": _perm.OVERWRITE,
    "run_python": _perm.RUN_PYTHON,
}

_RESULT_LIMIT = 40000

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the workspace. Defaults to '.'."},
                    "recursive": {"type": "boolean", "default": False},
                    "max_depth": {"type": "integer", "description": "Maximum depth when recursive."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search the contents of files inside the project workspace for "
                "a pattern, returning matching lines with file path and line "
                "number. Case-insensitive literal search by default; set "
                "regex=true for regular expressions. Skips non-UTF-8 and very "
                "large files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regular expression to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative directory to search. Defaults to '.'.",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "regex": {
                        "type": "boolean",
                        "default": False,
                        "description": "Interpret pattern as a regular expression.",
                    },
                    "include": {
                        "type": "string",
                        "description": "Optional glob filter on file names, e.g. '*.py'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Cap on returned matching lines (default 200). The "
                            "result carries a truncated flag when the cap is hit."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new UTF-8 text file. Fails if it already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace exactly one unique text occurrence in an existing file. "
                "Fails if old_text is absent or occurs more than once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "overwrite_file",
            "description": "Replace all contents of an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_up",
            "description": (
                "Move scratch files you created with create_file during this "
                "session into the workspace .to_delete folder (a recoverable "
                "trash area - nothing is permanently deleted). Use this at "
                "the end of a task to tidy away temporary files you no longer "
                "need. Only files created in the current session are "
                "accepted; anything else is refused and listed under "
                "'skipped' with a reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relative paths of files to clean up.",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a .py file in-process inside the project workspace. "
                "NOT sandboxed. Requires the run_python permission."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path"],
            },
        },
    },
]


class Tools:
    """Holds tool implementations and centralised permission gating."""

    def __init__(self, workspace: Workspace, permissions: _perm.Permissions, runner: Runner):
        self.workspace = workspace
        self.permissions = permissions
        self.runner = runner
        # Files created with create_file during this session (canonical
        # absolute paths).  clean_up may only move these, so it needs no
        # permission prompt: the agent can only tidy away its own debris,
        # and the move into .to_delete is recoverable.
        self._session_created = set()
        self._session_cleaned = set()

    @property
    def schemas(self):
        return TOOL_SCHEMAS

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, tool_call: dict) -> str:
        """Execute one tool call, returning a JSON string for the model."""
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        call_id = tool_call.get("id", "")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {}
        except ValueError:
            return _result({"error": "Invalid JSON arguments", "raw": raw_args})

        cap = CAPABILITY_MAP.get(name)
        if cap is not None:
            title, details = self._preview(name, args)
            if not self.permissions.authorize(cap, title, details):
                return _result({"ok": False, "denied": True})

        try:
            outcome = self._execute(name, args)
            return _result(outcome)
        except (WorkspaceError, RunnerError) as exc:
            msg = str(exc)
            if msg.startswith("Blocked"):
                return _result({"ok": False, "blocked": True, "error": msg})
            return _result({"ok": False, "error": msg})
        except Exception as exc:  # defensive: never leak a traceback to the model
            return _result({"ok": False, "error": f"Unexpected error in '{name}': {exc}"})

    # -- preview for permission prompt --------------------------------------

    def _preview(self, name: str, args: dict) -> tuple[str, str]:
        """Return ``(title, details)`` for the permission prompt.

        Mirrors the original jeb.py Dispatcher: *title* is a short uppercase
        label and *details* is a multi-line preview shown to the user.
        """
        try:
            if name == "create_file":
                relative = args.get("path", "")
                content = args.get("content", "")
                preview = "\n".join(content.splitlines()[:40])
                return "CREATE FILE", f"File: {relative}\n\n{preview}"

            if name == "edit_file":
                relative = args.get("path", "")
                preview = self.workspace.preview_edit(
                    relative, args.get("old_text", ""), args.get("new_text", "")
                )
                return "EDIT FILE", preview

            if name == "overwrite_file":
                relative = args.get("path", "")
                preview = self.workspace.preview_overwrite(
                    relative, args.get("content", "")
                )
                return "OVERWRITE FILE", preview

            if name == "run_python":
                import hashlib

                relative = args.get("path", "")
                argv = args.get("args", [])
                try:
                    full = self.workspace.resolve(relative)
                    source = full.read_text(encoding="utf-8")
                    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                except Exception:
                    digest = "(unavailable)"
                details = (
                    f"File: {relative}\n"
                    f"Arguments: {argv}\n"
                    f"SHA-256: {digest}\n\n"
                    "Static scan found no known termination constructs.\n\n"
                    "WARNING: This remains unsandboxed in-process execution."
                )
                return "RUN PYTHON", details

        except WorkspaceError as exc:
            return name.upper(), f"{name}: {exc}"
        return name.upper(), f"{name}: {args}"

    # -- implementations -----------------------------------------------------

    def _execute(self, name: str, args: dict) -> dict:
        if name == "list_files":
            entries = self.workspace.list_files(
                args.get("path", "."),
                recursive=bool(args.get("recursive", False)),
                max_depth=args.get("max_depth"),
            )
            return {"entries": entries}

        if name == "read_file":
            return {
                "content": self.workspace.read_file(
                    args["path"],
                    start_line=args.get("start_line"),
                    end_line=args.get("end_line"),
                )
            }

        if name == "search_files":
            return self.workspace.search_files(
                args.get("pattern"),
                path=args.get("path", "."),
                case_sensitive=bool(args.get("case_sensitive", False)),
                regex=bool(args.get("regex", False)),
                include=args.get("include"),
                max_results=args.get("max_results"),
            )

        if name == "create_file":
            outcome = self.workspace.create_file(args["path"], args.get("content", ""))
            self._record_created(args["path"])
            return outcome

        if name == "edit_file":
            return self.workspace.edit_file(
                args["path"], args["old_text"], args["new_text"]
            )

        if name == "overwrite_file":
            return self.workspace.overwrite_file(args["path"], args.get("content", ""))

        if name == "run_python":
            return self.runner.run(args["path"], args.get("args", []))

        if name == "clean_up":
            return self._clean_up(args)

        return {"error": f"Unknown tool: {name}"}

    # -- session bookkeeping for clean_up -------------------------------------

    def _record_created(self, path: str):
        """Remember that *path* was created this session (for clean_up)."""
        try:
            key = str(self.workspace.resolve(path))
        except WorkspaceError:
            return
        self._session_created.add(key)
        self._session_cleaned.discard(key)

    def _clean_up(self, args: dict) -> dict:
        """Move session-created scratch files into the workspace .to_delete."""
        paths = args.get("paths")
        if (not isinstance(paths, list) or not paths
                or not all(isinstance(p, str) and p.strip() for p in paths)):
            raise WorkspaceError(
                "clean_up requires a non-empty 'paths' list of relative file paths"
            )

        trash = self.workspace.trash_dir()
        moved, skipped, seen = [], [], set()
        for raw in paths:
            rel = raw.strip()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                full = self.workspace.resolve(rel)
            except WorkspaceError as exc:
                skipped.append({"path": rel, "reason": str(exc)})
                continue
            key = str(full)
            if full == trash or trash in full.parents:
                skipped.append({"path": rel, "reason": "path is inside .to_delete"})
            elif key in self._session_cleaned:
                skipped.append({"path": rel, "reason": "already cleaned up this session"})
            elif key not in self._session_created:
                skipped.append({
                    "path": rel,
                    "reason": "not created with create_file in this session",
                })
            elif not full.exists():
                skipped.append({"path": rel, "reason": "no longer exists"})
            elif not full.is_file():
                skipped.append({"path": rel, "reason": "not a file"})
            else:
                outcome = self.workspace.clean_up(rel)
                self._session_created.discard(key)
                self._session_cleaned.add(key)
                moved.append({
                    "path": outcome["path"],
                    "moved_to": outcome["moved_to"],
                })
        return {"ok": True, "moved": moved, "skipped": skipped}


def _result(obj: dict) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > _RESULT_LIMIT:
        text = text[:_RESULT_LIMIT] + '...["truncated"]}'
    return text
