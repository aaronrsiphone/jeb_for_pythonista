import ast
import builtins
import contextlib
import difflib
import hashlib
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import requests

try:
    import console
except ImportError:
    console = None


try:

# ----------------------------------------------------------------------
# Paths / limits
# ----------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
WORKSPACE = APP_DIR / "workspace"

CONFIG_PATH = APP_DIR / "miniagent_config.json"
PERMISSIONS_PATH = APP_DIR / "miniagent_permissions.json"

MAX_READ_CHARS = 28_000
MAX_TOOL_RESULT_CHARS = 30_000
MAX_DIFF_CHARS = 10_000
MAX_LIST_ENTRIES = 500
MAX_AGENT_STEPS = 20


SYSTEM_PROMPT = """You are a coding agent running inside Pythonista on iOS.

Important environment constraints:

- There is no shell.
- There is no subprocess support.
- Use only the provided tools for project operations.
- Your filesystem access is confined to the project workspace.
- Python execution occurs inside the SAME Pythonista process as the agent.
- run_python is NOT a sandbox.

Never add or intentionally execute process/application termination behavior:
- exit()
- quit()
- SystemExit
- sys.exit()
- os._exit()
- os.abort()
- os.kill()
- os.fork()
- os.exec*()

Some Python or native operations can destabilize Pythonista, so execute code only
when useful for validating the user's task.

Coding behavior:

- Inspect relevant files before changing them.
- Prefer edit_file for focused modifications.
- Use create_file only when creating a new path.
- Use overwrite_file only when replacing an entire existing file is appropriate.
- Run changed code when useful and permission is granted.
- Recover from tool errors instead of blindly repeating the same call.
- Keep changes focused.
"""


# ----------------------------------------------------------------------
# Exceptions / helpers
# ----------------------------------------------------------------------

class ToolError(Exception):
    pass


class ProviderError(Exception):
    pass


class RunBlocked(Exception):
    pass


def trunc(value, limit):
    text = str(value)

    if len(text) <= limit:
        return text

    removed = len(text) - limit
    return text[:limit] + f"\n... [{removed} chars truncated]"


def safe_input(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def secure_input(prompt):
    if console is not None and hasattr(console, "secure_input"):
        return console.secure_input(prompt)

    try:
        import getpass
        return getpass.getpass(prompt)
    except Exception:
        print("WARNING: secure input unavailable.")
        return safe_input(prompt)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

def credential_service(base_url):
    digest = hashlib.sha256(
        base_url.encode("utf-8")
    ).hexdigest()[:16]

    return f"MiniAgent:{digest}"


def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    print("First-run MiniAgent configuration")
    print()

    base_url = ""

    while not base_url:
        base_url = safe_input(
            "API base URL, e.g. https://host.example/v1: "
        ).strip()

    model = ""

    while not model:
        model = safe_input(
            "Model name/id: "
        ).strip()

    chat_path = safe_input(
        "Chat completions path [/chat/completions]: "
    ).strip()

    if not chat_path:
        chat_path = "/chat/completions"

    use_auth = safe_input(
        "Use API-key authentication? [Y/n]: "
    ).strip().lower()

    config = {
        "base_url": base_url.rstrip("/"),
        "chat_path": chat_path,
        "model": model,
        "auth_enabled": use_auth not in ("n", "no"),

        # Editable for providers using another header scheme.
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",

        "timeout": 120,

        # Compatibility escape hatches.
        "extra_headers": {},
        "extra_body": {},
    }

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return config


def load_api_key(config):
    if not config.get("auth_enabled", True):
        return None

    service = credential_service(config["base_url"])
    account = "api_key"

    if keychain is not None:
        try:
            value = keychain.get_password(service, account)
        except Exception:
            value = None

        if value:
            return value

        value = secure_input(
            "API key (will be stored in Pythonista keychain): "
        ).strip()

        if not value:
            raise RuntimeError("API key required.")

        keychain.set_password(
            service,
            account,
            value,
        )

        return value

    print(
        "WARNING: keychain unavailable; "
        "API key will only exist for this run."
    )

    value = secure_input("API key: ").strip()

    if not value:
        raise RuntimeError("API key required.")

    return value


# ----------------------------------------------------------------------
# Workspace confinement
# ----------------------------------------------------------------------

class Workspace:

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.real_root = os.path.realpath(
            str(self.root)
        )

    def resolve(
        self,
        relative_path,
        must_exist=False,
    ):
        if relative_path is None:
            raise ToolError("path is required")

        relative_path = str(relative_path)

        if "\x00" in relative_path:
            raise ToolError(
                "NUL bytes are not allowed in paths"
            )

        if os.path.isabs(relative_path):
            raise ToolError(
                "absolute paths are not allowed"
            )

        candidate = os.path.realpath(
            os.path.join(
                self.real_root,
                relative_path,
            )
        )

        try:
            common = os.path.commonpath(
                [
                    self.real_root,
                    candidate,
                ]
            )
        except ValueError:
            raise ToolError(
                "path is outside workspace"
            )

        if common != self.real_root:
            raise ToolError(
                "path escapes workspace"
            )

        path = Path(candidate)

        if must_exist and not path.exists():
            raise ToolError(
                f"path does not exist: {relative_path}"
            )

        return path

    def relative(self, path):
        return os.path.relpath(
            str(path),
            self.real_root,
        ).replace(
            os.sep,
            "/",
        )


# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------

class PermissionManager:

    CAPABILITIES = {
        "write",
        "edit",
        "overwrite",
        "run_python",
    }

    def __init__(self, path, workspace):
        self.path = Path(path)

        self.workspace_key = os.path.realpath(
            str(workspace)
        )

        self.session = {}
        self.saved = self._load()

    def _load(self):
        if not self.path.exists():
            return {}

        try:
            with self.path.open(
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, dict):
                return data

        except Exception:
            pass

        return {}

    def _save(self):
        with self.path.open(
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                self.saved,
                f,
                indent=2,
                sort_keys=True,
            )

    def project_rules(self):
        rules = self.saved.get(
            self.workspace_key
        )

        if not isinstance(rules, dict):
            rules = {}

            self.saved[
                self.workspace_key
            ] = rules

        return rules

    def authorize(
        self,
        capability,
        title,
        details,
    ):
        if capability not in self.CAPABILITIES:
            raise ToolError(
                f"unknown capability: {capability}"
            )

        saved = self.project_rules().get(
            capability,
            "ask",
        )

        if saved == "allow":
            return True

        if saved == "deny":
            return False

        if capability in self.session:
            return self.session[capability]

        print()
        print("=" * 68)
        print(f"PERMISSION REQUEST: {title}")
        print(f"Capability: {capability}")
        print("-" * 68)

        print(
            trunc(
                details,
                MAX_DIFF_CHARS,
            )
        )

        print("-" * 68)
        print("y  allow once")
        print("s  allow this capability for this session")
        print("a  always allow for this workspace")
        print("n  deny once")
        print("d  deny this capability for this session")
        print("x  always deny for this workspace")

        while True:
            choice = safe_input(
                "Permission [y/s/a/n/d/x]: "
            ).strip().lower()

            if choice == "y":
                return True

            if choice == "s":
                self.session[capability] = True
                return True

            if choice == "a":
                self.project_rules()[
                    capability
                ] = "allow"

                self._save()
                return True

            if choice in ("", "n"):
                return False

            if choice == "d":
                self.session[capability] = False
                return False

            if choice == "x":
                self.project_rules()[
                    capability
                ] = "deny"

                self._save()
                return False

    def clear(self):
        self.session.clear()

        self.saved.pop(
            self.workspace_key,
            None,
        )

        self._save()

    def summary(self):
        return {
            "session": self.session,
            "persistent": self.project_rules(),
        }


# ----------------------------------------------------------------------
# Atomic writes / previews
# ----------------------------------------------------------------------

def atomic_write(path, content):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temporary = tempfile.mkstemp(
        prefix=".miniagent-",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="",
        ) as f:
            f.write(content)
            f.flush()

        os.replace(
            temporary,
            str(path),
        )

    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass

        raise


def diff_text(old, new, path):
    diff = difflib.unified_diff(
        old.splitlines(True),
        new.splitlines(True),
        fromfile=path + " (before)",
        tofile=path + " (after)",
    )

    result = "".join(diff)

    if not result:
        result = "[contents unchanged]"

    return trunc(
        result,
        MAX_DIFF_CHARS,
    )


# ----------------------------------------------------------------------
# Read tools
# ----------------------------------------------------------------------

def list_files(workspace, args):
    relative = args.get(
        "path",
        ".",
    )

    recursive = args.get(
        "recursive",
        True,
    )

    depth_limit = int(
        args.get(
            "max_depth",
            4,
        )
    )

    depth_limit = max(
        0,
        min(
            depth_limit,
            12,
        ),
    )

    base = workspace.resolve(
        relative,
        must_exist=True,
    )

    if not base.is_dir():
        raise ToolError(
            "path is not a directory"
        )

    entries = []
    base_depth = len(base.parts)

    if recursive:
        iterator = base.rglob("*")
    else:
        iterator = base.iterdir()

    for path in iterator:
        depth = (
            len(path.parts)
            - base_depth
        )

        if recursive and depth > depth_limit:
            continue

        name = workspace.relative(path)

        if path.is_dir():
            name += "/"

        entries.append(name)

        if len(entries) >= MAX_LIST_ENTRIES:
            break

    entries.sort()

    return {
        "ok": True,
        "entries": entries,
        "truncated": (
            len(entries)
            >= MAX_LIST_ENTRIES
        ),
    }


def read_file(workspace, args):
    relative = args["path"]

    path = workspace.resolve(
        relative,
        must_exist=True,
    )

    if not path.is_file():
        raise ToolError(
            "path is not a file"
        )

    try:
        text = path.read_text(
            encoding="utf-8"
        )
    except UnicodeDecodeError:
        raise ToolError(
            "file is not UTF-8 text"
        )

    lines = text.splitlines()

    start = max(
        1,
        int(
            args.get(
                "start_line",
                1,
            )
        ),
    )

    if args.get("end_line") is None:
        end = len(lines)
    else:
        end = min(
            int(args["end_line"]),
            len(lines),
        )

    if end < start:
        raise ToolError(
            "end_line precedes start_line"
        )

    output = []
    size = 0

    actual_end = start - 1

    for number in range(
        start,
        end + 1,
    ):
        line = (
            f"{number:6d} | "
            + lines[number - 1]
        )

        if (
            size
            + len(line)
            + 1
            > MAX_READ_CHARS
        ):
            break

        output.append(line)

        size += len(line) + 1
        actual_end = number

    return {
        "ok": True,
        "path": relative,
        "start_line": start,
        "end_line": actual_end,
        "total_lines": len(lines),
        "truncated": actual_end < end,
        "content": "\n".join(output),
    }


# ----------------------------------------------------------------------
# Python execution preflight
# ----------------------------------------------------------------------

def dotted_name(node):
    parts = []

    while isinstance(
        node,
        ast.Attribute,
    ):
        parts.append(node.attr)
        node = node.value

    if isinstance(node, ast.Name):
        parts.append(node.id)

        return tuple(
            reversed(parts)
        )

    return ()


def scan_python(source, filename):
    try:
        tree = ast.parse(
            source,
            filename=filename,
        )
    except SyntaxError as exc:
        return [
            (
                f"SyntaxError line "
                f"{exc.lineno}: {exc.msg}"
            )
        ]

    issues = []

    dangerous_calls = {
        ("sys", "exit"),
        ("os", "_exit"),
        ("os", "abort"),
        ("os", "kill"),
        ("os", "fork"),
        ("builtins", "exit"),
        ("builtins", "quit"),
    }

    for node in ast.walk(tree):
        line = getattr(
            node,
            "lineno",
            "?",
        )

        if isinstance(node, ast.Call):

            if (
                isinstance(
                    node.func,
                    ast.Name,
                )
                and node.func.id
                in {"exit", "quit"}
            ):
                issues.append(
                    f"line {line}: "
                    f"{node.func.id}() blocked"
                )

            dotted = dotted_name(
                node.func
            )

            if dotted in dangerous_calls:
                issues.append(
                    f"line {line}: "
                    f"{'.'.join(dotted)}() blocked"
                )

            if (
                len(dotted) == 2
                and dotted[0] == "os"
                and dotted[1].startswith(
                    "exec"
                )
            ):
                issues.append(
                    f"line {line}: "
                    f"{'.'.join(dotted)}() blocked"
                )

        if isinstance(node, ast.Raise):
            target = node.exc

            if isinstance(
                target,
                ast.Call,
            ):
                target = target.func

            dotted = dotted_name(
                target
            )

            if (
                dotted
                and dotted[-1]
                == "SystemExit"
            ):
                issues.append(
                    f"line {line}: "
                    "SystemExit blocked"
                )

        if isinstance(
            node,
            ast.ImportFrom,
        ):
            module = node.module or ""

            names = {
                alias.name
                for alias in node.names
            }

            if (
                module == "sys"
                and "exit" in names
            ):
                issues.append(
                    f"line {line}: "
                    "direct sys.exit import blocked"
                )

            if (
                module == "builtins"
                and names.intersection(
                    {
                        "exit",
                        "quit",
                        "SystemExit",
                    }
                )
            ):
                issues.append(
                    f"line {line}: "
                    "termination builtin import blocked"
                )

            if (
                module == "os"
                and names.intersection(
                    {
                        "_exit",
                        "abort",
                        "kill",
                        "fork",
                        "execl",
                        "execle",
                        "execlp",
                        "execlpe",
                        "execv",
                        "execve",
                        "execvp",
                        "execvpe",
                    }
                )
            ):
                issues.append(
                    f"line {line}: "
                    "process-control os import blocked"
                )

    # Stable deduplication.
    return list(
        dict.fromkeys(issues)
    )


def blocked_termination(
    *args,
    **kwargs,
):
    raise RunBlocked(
        "termination blocked by MiniAgent"
    )


def purge_workspace_modules(workspace):
    """Avoid stale imports after the agent edits local modules."""

    remove = []

    for name, module in list(
        sys.modules.items()
    ):
        filename = getattr(
            module,
            "__file__",
            None,
        )

        if not filename:
            continue

        try:
            real = os.path.realpath(
                filename
            )

            if (
                os.path.commonpath(
                    [
                        workspace.real_root,
                        real,
                    ]
                )
                == workspace.real_root
            ):
                remove.append(name)

        except Exception:
            pass

    for name in remove:
        sys.modules.pop(
            name,
            None,
        )


def execute_python(
    workspace,
    path,
    argv,
):
    """
    In-process execution.

    This reduces a few known hazards but is NOT a sandbox.
    """

    source = path.read_text(
        encoding="utf-8"
    )

    code = compile(
        source,
        str(path),
        "exec",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()

    old_argv = list(sys.argv)
    old_cwd = os.getcwd()
    old_path = list(sys.path)
    old_sys_exit = sys.exit

    patched_builtins = {}

    for name in (
        "exit",
        "quit",
    ):
        if hasattr(
            builtins,
            name,
        ):
            patched_builtins[
                name
            ] = getattr(
                builtins,
                name,
            )

            setattr(
                builtins,
                name,
                blocked_termination,
            )

    patched_os = {}

    for name in (
        "_exit",
        "abort",
        "kill",
    ):
        if hasattr(os, name):
            patched_os[
                name
            ] = getattr(
                os,
                name,
            )

            setattr(
                os,
                name,
                blocked_termination,
            )

    safe_builtins = dict(
        vars(builtins)
    )

    safe_builtins[
        "exit"
    ] = blocked_termination

    safe_builtins[
        "quit"
    ] = blocked_termination

    namespace = {
        "__name__": "__main__",
        "__file__": str(path),
        "__package__": None,
        "__builtins__": safe_builtins,
    }

    purge_workspace_modules(
        workspace
    )

    status = "success"
    exception = ""

    try:
        sys.exit = blocked_termination

        sys.argv = (
            [str(path)]
            + list(argv)
        )

        os.chdir(
            str(path.parent)
        )

        sys.path.insert(
            0,
            str(path.parent),
        )

        with (
            contextlib.redirect_stdout(
                stdout
            ),
            contextlib.redirect_stderr(
                stderr
            ),
        ):
            try:
                exec(
                    code,
                    namespace,
                    namespace,
                )

            except Exception:
                status = "error"

                exception = (
                    traceback.format_exc()
                )

    finally:
        sys.exit = old_sys_exit

        sys.argv[:] = old_argv

        os.chdir(old_cwd)

        sys.path[:] = old_path

        for name, value in (
            patched_builtins.items()
        ):
            setattr(
                builtins,
                name,
                value,
            )

        for name, value in (
            patched_os.items()
        ):
            setattr(
                os,
                name,
                value,
            )

    return {
        "ok": status == "success",
        "status": status,
        "stdout": trunc(
            stdout.getvalue(),
            MAX_TOOL_RESULT_CHARS // 2,
        ),
        "stderr": trunc(
            stderr.getvalue(),
            MAX_TOOL_RESULT_CHARS // 2,
        ),
        "exception": trunc(
            exception,
            MAX_TOOL_RESULT_CHARS // 2,
        ),
    }


# ----------------------------------------------------------------------
# Tool schemas
# ----------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories "
                "inside the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "recursive": {
                        "type": "boolean"
                    },
                    "max_depth": {
                        "type": "integer"
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 file "
                "with line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "start_line": {
                        "type": "integer"
                    },
                    "end_line": {
                        "type": "integer"
                    },
                },
                "required": [
                    "path"
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a new UTF-8 text file. "
                "Fails if it already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "content": {
                        "type": "string"
                    },
                },
                "required": [
                    "path",
                    "content",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace exactly one unique "
                "text occurrence in an existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "old_text": {
                        "type": "string"
                    },
                    "new_text": {
                        "type": "string"
                    },
                },
                "required": [
                    "path",
                    "old_text",
                    "new_text",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "overwrite_file",
            "description": (
                "Replace all contents "
                "of an existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "content": {
                        "type": "string"
                    },
                },
                "required": [
                    "path",
                    "content",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute an existing .py file "
                "inside the current Pythonista process. "
                "There is no process isolation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "args": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                    },
                },
                "required": [
                    "path"
                ],
            },
        },
    },
]


# ----------------------------------------------------------------------
# Dispatcher: all consequential operations pass through here
# ----------------------------------------------------------------------

class Dispatcher:

    def __init__(
        self,
        workspace,
        permissions,
    ):
        self.workspace = workspace
        self.permissions = permissions

    def dispatch(
        self,
        name,
        args,
    ):
        try:
            if not isinstance(
                args,
                dict,
            ):
                raise ToolError(
                    "arguments must be an object"
                )

            if name == "list_files":
                return list_files(
                    self.workspace,
                    args,
                )

            if name == "read_file":
                return read_file(
                    self.workspace,
                    args,
                )

            if name == "create_file":
                return self.create_file(
                    args
                )

            if name == "edit_file":
                return self.edit_file(
                    args
                )

            if name == "overwrite_file":
                return self.overwrite_file(
                    args
                )

            if name == "run_python":
                return self.run_python(
                    args
                )

            raise ToolError(
                f"unknown tool: {name}"
            )

        except ToolError as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            }

    def create_file(self, args):
        relative = args["path"]
        content = args["content"]

        path = self.workspace.resolve(
            relative
        )

        if path.exists():
            raise ToolError(
                "path already exists"
            )

        preview = "\n".join(
            content.splitlines()[:40]
        )

        if not self.permissions.authorize(
            "write",
            "CREATE FILE",
            (
                f"File: {relative}\n\n"
                f"{preview}"
            ),
        ):
            return {
                "ok": False,
                "denied": True,
            }

        atomic_write(
            path,
            content,
        )

        return {
            "ok": True,
            "path": relative,
        }

    def edit_file(self, args):
        relative = args["path"]
        old = args["old_text"]
        new = args["new_text"]

        if not old:
            raise ToolError(
                "old_text cannot be empty"
            )

        path = self.workspace.resolve(
            relative,
            must_exist=True,
        )

        current = path.read_text(
            encoding="utf-8"
        )

        count = current.count(old)

        if count == 0:
            raise ToolError(
                "old_text not found"
            )

        if count != 1:
            raise ToolError(
                f"old_text matched {count} times"
            )

        proposed = current.replace(
            old,
            new,
            1,
        )

        preview = diff_text(
            current,
            proposed,
            relative,
        )

        if not self.permissions.authorize(
            "edit",
            "EDIT FILE",
            preview,
        ):
            return {
                "ok": False,
                "denied": True,
            }

        atomic_write(
            path,
            proposed,
        )

        return {
            "ok": True,
            "path": relative,
        }

    def overwrite_file(self, args):
        relative = args["path"]
        content = args["content"]

        path = self.workspace.resolve(
            relative,
            must_exist=True,
        )

        current = path.read_text(
            encoding="utf-8"
        )

        preview = diff_text(
            current,
            content,
            relative,
        )

        if not self.permissions.authorize(
            "overwrite",
            "OVERWRITE FILE",
            preview,
        ):
            return {
                "ok": False,
                "denied": True,
            }

        atomic_write(
            path,
            content,
        )

        return {
            "ok": True,
            "path": relative,
        }

    def run_python(self, args):
        relative = args["path"]

        argv = args.get(
            "args",
            [],
        )

        if not (
            isinstance(argv, list)
            and all(
                isinstance(x, str)
                for x in argv
            )
        ):
            raise ToolError(
                "args must be strings"
            )

        path = self.workspace.resolve(
            relative,
            must_exist=True,
        )

        if (
            not path.is_file()
            or path.suffix.lower()
            != ".py"
        ):
            raise ToolError(
                "run_python requires a .py file"
            )

        source = path.read_text(
            encoding="utf-8"
        )

        issues = scan_python(
            source,
            relative,
        )

        if issues:
            return {
                "ok": False,
                "blocked": True,
                "error": (
                    "static execution "
                    "preflight failed"
                ),
                "issues": issues,
            }

        digest = hashlib.sha256(
            source.encode("utf-8")
        ).hexdigest()

        details = (
            f"File: {relative}\n"
            f"Arguments: {argv}\n"
            f"SHA-256: {digest}\n\n"
            "Static scan found no known "
            "termination constructs.\n\n"
            "WARNING: This remains unsandboxed "
            "in-process execution."
        )

        if not self.permissions.authorize(
            "run_python",
            "RUN PYTHON",
            details,
        ):
            return {
                "ok": False,
                "denied": True,
            }

        result = execute_python(
            self.workspace,
            path,
            argv,
        )

        result["path"] = relative
        result["sha256"] = digest

        return result


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------

class CompatibleChatProvider:
    """
    A small client for OpenAI-compatible Chat Completions servers.

    Nothing here requires the server to actually be OpenAI.
    """

    def __init__(
        self,
        config,
        api_key,
    ):
        self.config = config
        self.api_key = api_key
        self.effort = None

    @property
    def endpoint(self):
        return (
            self.config[
                "base_url"
            ].rstrip("/")
            + "/"
            + self.config[
                "chat_path"
            ].lstrip("/")
        )

    def complete(
        self,
        messages,
    ):
        headers = {
            "Content-Type":
                "application/json"
        }

        headers.update(
            self.config.get(
                "extra_headers",
                {},
            )
        )

        if self.api_key:
            header = self.config.get(
                "auth_header",
                "Authorization",
            )

            prefix = self.config.get(
                "auth_prefix",
                "Bearer ",
            )

            headers[header] = (
                prefix
                + self.api_key
            )

        body = {
            "model":
                self.config["model"],
            "messages":
                messages,
            "tools":
                TOOLS,
        }

        if self.effort is not None:
            body[
                "reasoning_effort"
            ] = self.effort

        # Lets us add provider-specific flags
        # without modifying the client.
        body.update(
            self.config.get(
                "extra_body",
                {},
            )
        )

        try:
            response = requests.post(
                self.endpoint,
                headers=headers,
                json=body,
                timeout=self.config.get(
                    "timeout",
                    120,
                ),
            )

        except Exception as exc:
            raise ProviderError(
                f"request failed: {exc}"
            )

        if not (
            200
            <= response.status_code
            < 300
        ):
            raise ProviderError(
                f"HTTP "
                f"{response.status_code}\n"
                f"{trunc(response.text, 4000)}"
            )

        try:
            payload = response.json()

        except Exception:
            raise ProviderError(
                "provider returned non-JSON"
            )

        choices = payload.get(
            "choices"
        )

        if not choices:
            raise ProviderError(
                "response contains no choices"
            )

        message = choices[
            0
        ].get("message")

        if not isinstance(
            message,
            dict,
        ):
            raise ProviderError(
                "choices[0].message missing"
            )

        return message


# ----------------------------------------------------------------------
# Agent loop
# ----------------------------------------------------------------------

def parse_arguments(raw):
    if raw in (
        None,
        "",
    ):
        return {}

    if isinstance(
        raw,
        dict,
    ):
        return raw

    try:
        value = json.loads(raw)

    except Exception as exc:
        raise ToolError(
            f"invalid tool JSON: {exc}"
        )

    if not isinstance(
        value,
        dict,
    ):
        raise ToolError(
            "tool arguments must "
            "decode to an object"
        )

    return value


def tool_result_message(result):
    return trunc(
        json.dumps(
            result,
            ensure_ascii=False,
        ),
        MAX_TOOL_RESULT_CHARS,
    )


def run_turn(
    provider,
    dispatcher,
    messages,
):
    for _ in range(
        MAX_AGENT_STEPS
    ):
        try:
            message = provider.complete(
                messages
            )

        except ProviderError as exc:
            print()
            print("Provider error:")
            print(exc)
            return

        tool_calls = (
            message.get(
                "tool_calls"
            )
            or []
        )

        reasoning = (
            message.get(
                "reasoning_content"
            )
            or message.get(
                "reasoning"
            )
        )

        if reasoning:
            print()
            print("Reasoning:")
            print(reasoning)

        content = message.get(
            "content"
        )

        if content:
            print()
            print("Assistant:")
            print(content)

        assistant_message = {
            "role": "assistant",
            "content": content,
        }

        if tool_calls:
            assistant_message[
                "tool_calls"
            ] = tool_calls

        messages.append(
            assistant_message
        )

        if not tool_calls:
            return

        for call in tool_calls:
            call_id = call.get(
                "id"
            )

            function = call.get(
                "function",
                {},
            )

            name = function.get(
                "name"
            )

            print()
            print(
                f"Tool request: {name}"
            )

            try:
                args = parse_arguments(
                    function.get(
                        "arguments"
                    )
                )

                result = (
                    dispatcher.dispatch(
                        name,
                        args,
                    )
                )

            except Exception as exc:
                result = {
                    "ok": False,
                    "error": str(exc),
                }

            if result.get("ok"):
                print("Tool result: ok")

            elif result.get("denied"):
                print(
                    "Tool result: denied"
                )

            elif result.get("blocked"):
                print(
                    "Tool result: blocked"
                )

            else:
                print(
                    "Tool result: error"
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id":
                        call_id,
                    "content":
                        tool_result_message(
                            result
                        ),
                }
            )

    print()
    print(
        "Per-turn agent step "
        "limit reached."
    )


# ----------------------------------------------------------------------
# Console
# ----------------------------------------------------------------------

def print_help():
    print(
        """
Commands

:help
    Show commands.

:reset
    Reset conversation context.

:perms
    Show permission state.

:clear-perms
    Clear session and persistent permissions.

:config
    Print provider configuration. The API key is not stored here.

:workspace
    Print the project workspace.

:effort [low|medium|high|xhigh|max]
    Set reasoning effort level sent as reasoning_effort.
    No argument prints the current level.

:quit
    Stop MiniAgent normally. This does not invoke exit().
""".strip()
    )


def main():
    WORKSPACE.mkdir(
        parents=True,
        exist_ok=True,
    )

    config = load_config()

    api_key = load_api_key(
        config
    )

    workspace = Workspace(
        WORKSPACE
    )

    permissions = PermissionManager(
        PERMISSIONS_PATH,
        WORKSPACE,
    )

    dispatcher = Dispatcher(
        workspace,
        permissions,
    )

    provider = CompatibleChatProvider(
        config,
        api_key,
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        }
    ]

    print()
    print("MiniAgent ready")
    print(
        f"Workspace: {WORKSPACE}"
    )
    print(
        f"Endpoint: {provider.endpoint}"
    )
    print(
        f"Model: {config['model']}"
    )
    print()

    while True:
        try:
            command = safe_input(
                "You> "
            ).strip()

        except KeyboardInterrupt:
            print()
            print(
                "Use :quit to stop "
                "MiniAgent cleanly."
            )
            continue

        if not command:
            continue

        if command == ":quit":
            print(
                "MiniAgent stopped."
            )
            break

        if command == ":help":
            print_help()
            continue

        if command == ":reset":
            messages = [
                {
                    "role": "system",
                    "content":
                        SYSTEM_PROMPT,
                }
            ]

            print(
                "Conversation reset."
            )
            continue

        if command == ":perms":
            print(
                json.dumps(
                    permissions.summary(),
                    indent=2,
                )
            )
            continue

        if command == ":clear-perms":
            confirm = safe_input(
                "Clear permissions? [y/N]: "
            ).strip().lower()

            if confirm == "y":
                permissions.clear()

                print(
                    "Permissions cleared."
                )

            continue

        if command == ":config":
            print(
                json.dumps(
                    config,
                    indent=2,
                )
            )

            continue

        if command == ":workspace":
            print(WORKSPACE)
            continue

        if command.startswith(":effort"):
            parts = command.split(
                None, 1
            )
            valid = {
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            }

            if len(parts) == 1:
                print(
                    "Effort: "
                    + (
                        provider.effort
                        or "not set"
                    )
                )

            elif parts[1] in valid:
                provider.effort = (
                    parts[1]
                )
                print(
                    f"Effort: {parts[1]}"
                )

            else:
                print(
                    "Valid levels: "
                    "low medium high "
                    "xhigh max"
                )

            continue

        messages.append(
            {
                "role": "user",
                "content": command,
            }
        )

        run_turn(
            provider,
            dispatcher,
            messages,
        )


if __name__ == "__main__":
    main()
