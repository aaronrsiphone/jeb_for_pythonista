"""Tool schemas, dispatch and permission gating.

Permission enforcement is centralised in ``Tools.dispatch`` so that individual
tool implementations do not each re-implement the check.

``dispatch`` is a *generator*: it yields the events of one tool call's
lifecycle (:class:`~miniagent.events.ToolStarted`, optionally
:class:`~miniagent.events.PermissionNeeded`, then
:class:`~miniagent.events.ToolCompleted`) and ``return``s the JSON string the
model receives.  Callers drive it with ``result = yield from
tools.dispatch(call)``.  Asking the user is therefore no longer something the
tool layer does behind the agent's back with ``input()``: it is an event the
driver answers by ``send()``-ing a
:class:`~miniagent.events.PermissionAnswer` back in.
"""

from __future__ import annotations

import json

from . import permissions as _perm
from .events import PermissionNeeded, ToolCompleted, ToolStarted
from .knowledge import Knowledge, KnowledgeError
from .runner import Runner, RunnerError
from .vision import Vision, VisionError
from .workspace import Workspace, WorkspaceError

# Tool name -> permission capability.  Read-only tools are absent here, which
# means "no permission required".  The read-only `knowledge` tool is absent
# too: none of its actions writes a file or executes code.  `ask_image` is
# gated even though it is read-only because it uploads image data — possibly
# photos or clipboard images from outside the workspace — to the vision
# provider's API.
CAPABILITY_MAP = {
    "create_file": _perm.WRITE,
    "edit_file": _perm.EDIT,
    "overwrite_file": _perm.OVERWRITE,
    "run_python": _perm.RUN_PYTHON,
    "ask_image": _perm.ASK_IMAGE,
}

_RESULT_LIMIT = 40000


def _required_args() -> dict:
    """Map each tool name to the argument names its schema marks required.

    Derived from ``TOOL_SCHEMAS`` rather than hand-maintained, so a tool
    cannot declare a required argument and then fail to enforce it.
    """
    out = {}
    for schema in TOOL_SCHEMAS:
        function = schema.get("function", {}) or {}
        params = function.get("parameters", {}) or {}
        required = params.get("required") or []
        if required:
            out[function.get("name", "")] = tuple(required)
    return out

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
                "session into the workspace to_delete folder (a recoverable "
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
                "NOT sandboxed. Requires the run_python permission. The "
                "script's stdout/stderr are captured and returned; "
                "interactive input (input() or sys.stdin) is disabled and "
                "fails fast with an error instead of prompting. Never write "
                "validation scripts that read stdin; script any answers "
                "the code under test would prompt for."
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
    {
        "type": "function",
        "function": {
            "name": "ask_image",
            "description": (
                "Ask the configured vision model a question about one or "
                "more images. Image sources, in the order sent: 'images' "
                "(workspace-relative file paths and/or http(s) image URLs), "
                "'photo' (iOS photo-library image index; negative counts "
                "from the end, so -1 is the most recent photo), and/or "
                "'clipboard' (the image currently on the clipboard). The "
                "image data is uploaded to the vision provider's API "
                "(config.json 'vision_model', format '<provider>/"
                "<model-name>'). Oversized images are automatically shrunk "
                "and re-encoded as JPEG. Returns the model's answer plus "
                "token usage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to ask about the image(s).",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Image file paths inside the project workspace, "
                            "or http(s) image URLs."
                        ),
                    },
                    "photo": {
                        "type": "integer",
                        "description": (
                            "Photo-library image index; negative counts from "
                            "the end (-1 = most recent photo)."
                        ),
                    },
                    "clipboard": {
                        "type": "boolean",
                        "description": "Use the image currently on the clipboard.",
                    },
                    "system": {
                        "type": "string",
                        "description": "Optional system prompt for the vision request.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum answer tokens (default 1024).",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge",
            "description": (
                "Read-only access to this install's Pythonista reference "
                "material; no permission required. Actions: 'list' (index "
                "the files under the optional, user-populated "
                "miniagent/knowledge/ directory, if present), 'read' "
                "(line-numbered contents of one such file), 'search' "
                "(literal or regex across those files), 'docs' "
                "(offline search of Pythonista's bundled official "
                "documentation: 'query' returns ranked symbol matches, "
                "'page' returns a doc page's readable text, no arguments "
                "lists the Pythonista module doc pages). 'list'/'read'/"
                "'search' report a clear error when the knowledge "
                "directory is absent; 'docs' is independent of it and "
                "always works. No action writes files or executes code. "
                "Knowledge paths are relative to the knowledge root and "
                "cannot escape it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "search", "docs"],
                        "description": "What to consult.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Knowledge file (list/read) or directory "
                            "(search), relative to the knowledge root."
                        ),
                    },
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "pattern": {
                        "type": "string",
                        "description": "search: text or regex to find.",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "regex": {"type": "boolean", "default": False},
                    "include": {
                        "type": "string",
                        "description": "search: glob filter on file names.",
                    },
                    "max_results": {"type": "integer"},
                    "query": {
                        "type": "string",
                        "description": (
                            "docs: symbol to find in the documentation "
                            "index (substring match)."
                        ),
                    },
                    "page": {
                        "type": "string",
                        "description": (
                            "docs: doc page to read, e.g. 'ui' or "
                            "'py3/ios/appex.html'."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# Built once from the schemas above; see _required_args().
REQUIRED_ARGS = _required_args()


class Tools:
    """Holds tool implementations and centralised permission gating."""

    def __init__(self, workspace: Workspace, permissions: _perm.Permissions, runner: Runner,
                 knowledge: Knowledge = None, vision: Vision = None):
        self.workspace = workspace
        self.permissions = permissions
        self.runner = runner
        # Collaborator behind the read-only `knowledge` tool.  Optional so
        # tests can inject one rooted at a throwaway directory; by default
        # it is created lazily on first use from the package's knowledge
        # directory, which lives outside the project workspace.
        self._kb = knowledge
        # Collaborator behind the `ask_image` tool.  Unlike knowledge it
        # cannot be self-constructed (it needs the provider config and a
        # keychain-backed key loader), so app.py passes it in; when absent,
        # ask_image reports that vision is not configured.
        self._vision = vision
        # Files created with create_file during this session (canonical
        # absolute paths).  clean_up may only move these, so it needs no
        # permission prompt: the agent can only tidy away its own debris,
        # and the move into to_delete is recoverable.
        self._session_created = set()
        self._session_cleaned = set()

    @property
    def schemas(self):
        return TOOL_SCHEMAS

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, tool_call: dict):
        """Run one tool call as a generator, returning a JSON string.

        Yields :class:`ToolStarted`, then — only when stored policy does not
        already decide a gated capability — :class:`PermissionNeeded`, whose
        answer the driver ``send()``s back, and finally
        :class:`ToolCompleted`.  The JSON payload for the model is the
        generator's *return* value, so callers write::

            result = yield from tools.dispatch(call)
        """
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {}
        except ValueError:
            # Arguments never parsed, so there are none to report.
            yield ToolStarted(name, {})
            outcome = {"error": "Invalid JSON arguments", "raw": raw_args}
            yield ToolCompleted(name, "error", "", outcome)
            return _result(outcome)

        yield ToolStarted(name, args)

        # Required-argument gate.  The schemas already declare what each tool
        # needs, so enforce it here instead of letting _execute() raise a bare
        # KeyError that reaches the model as "Unexpected error in 'read_file':
        # 'path'" — a message it cannot act on, and one indistinguishable from
        # a genuine harness fault.  Checked before the permission gate so a
        # malformed call never becomes a question for the user.
        missing = [key for key in REQUIRED_ARGS.get(name, ())
                   if args.get(key) is None]
        if missing:
            outcome = {
                "ok": False,
                "error": (
                    f"{name} requires the argument(s) "
                    + ", ".join(repr(k) for k in missing)
                ),
            }
            yield ToolCompleted(name, "error", "", outcome)
            return _result(outcome)

        cap = CAPABILITY_MAP.get(name)
        user_comment = ""
        if cap is not None:
            allowed = self.permissions.decide(cap)
            if allowed is None:
                # Build the preview only when the user will actually see it:
                # _preview() reads the target file and renders a diff, work
                # that is wasted whenever stored policy already decides.
                title, details = self._preview(name, args)
                answer = yield PermissionNeeded(cap, title, details)
                allowed, user_comment = self.permissions.apply_answer(cap, answer)
            if not allowed:
                outcome = {"ok": False, "denied": True}
                if user_comment:
                    outcome["user_comment"] = user_comment
                yield ToolCompleted(name, "denied", user_comment, outcome)
                return _result(outcome)

        try:
            outcome = self._execute(name, args)
        except (WorkspaceError, RunnerError, KnowledgeError, VisionError) as exc:
            msg = str(exc)
            if msg.startswith("Blocked"):
                outcome = {"ok": False, "blocked": True, "error": msg}
            else:
                outcome = {"ok": False, "error": msg}
        except Exception as exc:  # defensive: never leak a traceback to the model
            outcome = {"ok": False, "error": f"Unexpected error in '{name}': {exc}"}

        yield ToolCompleted(name, _status_of(outcome), user_comment,
                            outcome if isinstance(outcome, dict) else {})
        return _result(_with_comment(outcome, user_comment))

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
                lines = content.splitlines()
                byte_count = len(content.encode("utf-8"))
                stat_line = f"{relative}  {len(lines)} lines  {byte_count} bytes"
                head = lines[:8]
                body = "\n".join(head)
                if len(lines) > 8:
                    body += f"\n... ({len(lines) - 8} more lines)"
                return "CREATE FILE", f"{stat_line}\n\n{body}"

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
                source = None
                scan_name = relative
                try:
                    full = self.workspace.resolve(relative)
                    source = full.read_text(encoding="utf-8")
                    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                    scan_name = full.name
                except Exception:
                    digest = "(unavailable)"

                if source is None:
                    scan_line = (
                        "Static scan: unavailable (file could not be read)."
                    )
                else:
                    findings = self.runner.static_scan(source, scan_name)
                    if findings:
                        scan_line = (
                            "Static scan findings (run will be refused): "
                            + "; ".join(findings)
                        )
                    else:
                        scan_line = (
                            "Static scan found no known termination constructs."
                        )

                details = (
                    f"File: {relative}\n"
                    f"Arguments: {argv}\n"
                    f"SHA-256: {digest}\n\n"
                    f"{scan_line}\n"
                    "Interactive input: disabled at runtime — input() and\n"
                    "sys.stdin reads raise an error instead of hanging.\n\n"
                    "WARNING: This remains unsandboxed in-process execution."
                )
                return "RUN PYTHON", details

            if name == "ask_image":
                question = args.get("question", "")
                images = args.get("images") or []
                photo = args.get("photo")
                lines = [f"Question: {question}"]
                for image in images:
                    lines.append(f"Image: {image}")
                if photo is not None:
                    lines.append(
                        f"Photo library index: {photo} (negative = from the end)"
                    )
                if args.get("clipboard"):
                    lines.append("Clipboard image: yes")
                if self._vision is not None:
                    target = self._vision.target_label()
                else:
                    target = "(vision not configured)"
                lines.append(f"Vision target: {target}")
                lines.append("")
                lines.append(
                    "The image data will be uploaded to the vision "
                    "provider's API."
                )
                return "ASK IMAGE", "\n".join(lines)

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

        if name == "ask_image":
            return self._ask_image(args)

        if name == "knowledge":
            return self._knowledge(args)

        if name == "clean_up":
            return self._clean_up(args)

        return {"error": f"Unknown tool: {name}"}

    # -- ask_image tool -------------------------------------------------------

    def _ask_image(self, args: dict) -> dict:
        """Dispatch the vision tool: validate sources, delegate to Vision."""
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            raise VisionError("ask_image requires a non-empty 'question'")

        images = args.get("images") or []
        if not isinstance(images, list) or not all(
            isinstance(p, str) and p.strip() for p in images
        ):
            raise VisionError("'images' must be a list of file paths or URLs")

        photo = args.get("photo")
        if photo is not None and (isinstance(photo, bool) or not isinstance(photo, int)):
            raise VisionError("'photo' must be an integer photo-library index")

        clipboard = bool(args.get("clipboard"))
        if not images and photo is None and not clipboard:
            raise VisionError(
                "ask_image needs at least one image source: 'images' "
                "(workspace paths or URLs), 'photo' or 'clipboard'"
            )

        if self._vision is None:
            raise VisionError(
                "vision is not configured in this session (no Vision "
                "collaborator was passed to Tools)"
            )

        file_paths: list[str] = []
        urls: list[str] = []
        for item in images:
            item = item.strip()
            if item.lower().startswith(("http://", "https://")):
                urls.append(item)
            else:
                # Workspace confinement happens here: resolve() rejects
                # paths that escape the project root.
                file_paths.append(str(self.workspace.resolve(item)))

        return self._vision.ask(
            question.strip(),
            file_paths=file_paths,
            urls=urls,
            photo=photo,
            clipboard=clipboard,
            system=args.get("system"),
            max_tokens=args.get("max_tokens"),
        )

    # -- knowledge tool ------------------------------------------------------

    def _knowledge_base(self) -> Knowledge:
        """The knowledge collaborator, created lazily on first use."""
        if self._kb is None:
            self._kb = Knowledge()
        return self._kb

    def _knowledge(self, args: dict) -> dict:
        """Dispatch the read-only knowledge tool's actions."""
        kb = self._knowledge_base()
        action = args.get("action")

        if action == "list":
            return {
                "root": str(kb.root),
                "entries": kb.list(args.get("path") or "."),
            }

        if action == "read":
            path = _required_arg(args, "path", action)
            return {
                "root": str(kb.root),
                "content": kb.read(
                    path,
                    start_line=args.get("start_line"),
                    end_line=args.get("end_line"),
                ),
            }

        if action == "search":
            return kb.search(
                _required_arg(args, "pattern", action),
                path=args.get("path") or ".",
                case_sensitive=bool(args.get("case_sensitive", False)),
                regex=bool(args.get("regex", False)),
                include=args.get("include"),
                max_results=args.get("max_results"),
            )

        if action == "docs":
            return kb.docs(query=args.get("query"), page=args.get("page"))

        raise KnowledgeError(
            "knowledge requires an 'action' of 'list', 'read', 'search' "
            "or 'docs'"
        )

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
        """Move session-created scratch files into the workspace to_delete."""
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
                skipped.append({"path": rel, "reason": "path is inside to_delete"})
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


def _required_arg(args: dict, key: str, action: str) -> str:
    """Return the non-empty string argument *key*, or raise a clean error."""
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(
            f"knowledge action '{action}' requires a '{key}' argument"
        )
    return value


def _status_of(outcome) -> str:
    """Classify a tool outcome as ``ok``/``denied``/``blocked``/``error``.

    Read straight off the outcome dict the tool layer already holds, in the
    same precedence the console status line used when it re-parsed the
    serialized result.  A non-dict (nothing produces one today) counts as
    ``ok``, matching the old "otherwise" branch.
    """
    if not isinstance(outcome, dict):
        return "ok"
    if outcome.get("ok"):
        return "ok"
    if outcome.get("denied"):
        return "denied"
    if outcome.get("blocked"):
        return "blocked"
    if outcome.get("error"):
        return "error"
    return "ok"


def _with_comment(outcome, comment: str):
    """Attach the user's permission-prompt comment to a tool result.

    The comment a user appended to their permission answer (e.g. the redirect
    in ``"n. Write it to foo/bar"`` or the addendum in ``"y. Also check
    xyz"``) is relayed to the model under the ``"user_comment"`` key so it
    can act on the instruction.  Results are left untouched when there is no
    comment.
    """
    if comment and isinstance(outcome, dict):
        outcome = dict(outcome)
        outcome.setdefault("user_comment", comment)
    return outcome


def _result(obj: dict) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > _RESULT_LIMIT:
        text = text[:_RESULT_LIMIT] + '...["truncated"]}'
    return text
