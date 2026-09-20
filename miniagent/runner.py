"""In-process Python execution for the project workspace.

This runner performs in-process execution of project Python files.  It is
**not sandboxed** — Pythonista has no usable subprocess boundary for this
application.  Static preflight and defensive runtime replacement are used to
block obvious process/application termination behaviour, but a malicious or
buggy script may still destabilise Pythonista before Python can recover.

The execution environment — stdout/stderr, argv, cwd, ``sys.path``, and the
console-critical builtins ``input`` and ``print`` — is captured before each
run and restored afterwards, so a script that rebinds them cannot poison the
interactive console loop once the run ends.
"""

from __future__ import annotations

import ast
import builtins
import io
import os
import sys
import traceback
from pathlib import Path

from .workspace import Workspace, WorkspaceError

# ---------------------------------------------------------------------------
# Static preflight
# ---------------------------------------------------------------------------

# Dotted call targets that are blocked by the static preflight.
_BLOCKED_CALLS = {
    "exit",
    "quit",
    "sys.exit",
    "os._exit",
    "os.abort",
    "os.kill",
    "os.fork",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.execl",
    "os.execlp",
    "os.execle",
    "os.execlpe",
}

# Names that, when imported from their parent module, constitute a termination
# hazard.  This mirrors the import-blocking in the original jeb.py.
_BLOCKED_IMPORTS = {
    "sys": {"exit"},
    "builtins": {"exit", "quit", "SystemExit"},
    "os": {
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
    },
}

_OUTPUT_LIMIT = 20000


class RunnerError(Exception):
    """Raised when a runner preflight or compilation check fails."""


class Runner:
    """Executes project Python files in-process with capture + restoration."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    # -- public interface ---------------------------------------------------

    def run(self, path: str, args=None) -> dict:
        """Execute *path* in-process and return captured output.

        Returns a dict with ``stdout``, ``stderr`` and ``ok`` keys.
        """
        args = list(args or [])
        full = self.workspace.resolve(path)
        if not full.exists() or not full.is_file():
            raise RunnerError(f"Not a runnable file: {path}")
        if full.suffix != ".py":
            raise RunnerError(f"Not a .py file: {path}")

        source = full.read_text(encoding="utf-8")

        self._preflight(source, full)

        try:
            code_obj = compile(source, str(full), "exec")
        except SyntaxError as exc:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"SyntaxError: {exc}",
            }

        return self._execute(code_obj, full, args)

    # -- preflight ----------------------------------------------------------

    def _preflight(self, source: str, full: Path):
        try:
            tree = ast.parse(source, filename=str(full))
        except SyntaxError as exc:
            raise RunnerError(f"Cannot parse {full.name}: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted_name(node.func)
                if name and name in _BLOCKED_CALLS:
                    raise RunnerError(
                        f"Blocked termination call '{name}' in {full.name}"
                    )
            elif isinstance(node, ast.Raise):
                if node.exc is not None and isinstance(node.exc, ast.Call):
                    name = _dotted_name(node.exc.func)
                    if name == "SystemExit" or name == "BaseException":
                        raise RunnerError(
                            f"Blocked 'raise SystemExit()' in {full.name}"
                        )
                elif isinstance(node.exc, ast.Name) and node.exc.id in (
                    "SystemExit",
                    "BaseException",
                ):
                    raise RunnerError(
                        f"Blocked 'raise SystemExit' in {full.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {alias.name for alias in node.names}
                blocked = _BLOCKED_IMPORTS.get(module)
                if blocked:
                    hits = names & blocked
                    if hits:
                        label = "termination builtin import" if module == "builtins" else (
                            "direct sys.exit import" if module == "sys" else
                            "process-control os import"
                        )
                        raise RunnerError(
                            f"Blocked {label} in {full.name}: {', '.join(sorted(hits))}"
                        )

    # -- execution ----------------------------------------------------------

    def _execute(self, code_obj, full: Path, args: list) -> dict:
        root = str(self.workspace.root)

        old_argv = sys.argv
        old_cwd = os.getcwd()
        old_path = list(sys.path)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        old_modules = set(sys.modules)
        # Console-critical builtins: a run script that rebinds input()/print()
        # without restoring them would poison the interactive console loop
        # after the run ends (e.g. an input stub returning a fixed string
        # forever), so they are always restored in the finally block below.
        old_input = builtins.input
        old_print = builtins.print

        sys.argv = [str(full)] + args
        os.chdir(root)
        if root not in sys.path:
            sys.path.insert(0, root)

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        sys.stdout = out_buf
        sys.stderr = err_buf

        replacements = _install_runtime_blocks()

        namespace = {
            "__name__": "__main__",
            "__file__": str(full),
            "__builtins__": builtins.__dict__,
        }
        ok = True
        try:
            exec(code_obj, namespace)
        except SystemExit:
            err_buf.write("[blocked: SystemExit intercepted]\n")
            ok = False
        except BaseException:
            err_buf.write(traceback.format_exc())
            ok = False
        finally:
            _remove_runtime_blocks(replacements)
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            builtins.input = old_input
            builtins.print = old_print
            sys.argv = old_argv
            os.chdir(old_cwd)
            sys.path[:] = old_path
            self._purge_stale_imports(old_modules)

        return {
            "ok": ok,
            "stdout": _truncate(out_buf.getvalue()),
            "stderr": _truncate(err_buf.getvalue()),
        }

    def _purge_stale_imports(self, previous: set):
        """Remove modules imported during execution from the workspace."""
        root = os.path.abspath(str(self.workspace.root))
        for name in list(sys.modules):
            if name in previous:
                continue
            mod = sys.modules.get(name)
            if mod is None:
                continue
            f = getattr(mod, "__file__", None)
            if f and os.path.abspath(f).startswith(root + os.sep):
                del sys.modules[name]


# -- helpers ------------------------------------------------------------------


def _dotted_name(node):
    """Return the dotted call target of *node*, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _truncate(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[output truncated at {limit} characters]...\n"


class _BlockedTermination:
    """Callable that raises RuntimeError instead of terminating the process."""

    _label = "termination"

    def __init__(self, label="termination"):
        self._label = label

    def __call__(self, *args, **kwargs):
        raise RuntimeError(
            f"{self._label} blocked by the MiniAgent runner"
        )


def _install_runtime_blocks() -> dict:
    """Defensively replace termination builtins for the duration of a run.

    Mirrors the original jeb.py: patches ``builtins.exit``, ``builtins.quit``,
    ``sys.exit``, ``os._exit``, ``os.abort`` and ``os.kill``.
    """
    import sys as _sys

    saved = {}
    saved["builtins.exit"] = getattr(builtins, "exit", None)
    saved["builtins.quit"] = getattr(builtins, "quit", None)
    saved["sys.exit"] = getattr(_sys, "exit", None)
    saved["os._exit"] = getattr(os, "_exit", None)
    saved["os.abort"] = getattr(os, "abort", None)
    saved["os.kill"] = getattr(os, "kill", None)

    builtins.exit = _BlockedTermination("exit/quit")
    builtins.quit = _BlockedTermination("exit/quit")
    _sys.exit = _BlockedTermination("sys.exit")
    os._exit = _BlockedTermination("os._exit")
    os.abort = _BlockedTermination("os.abort")
    os.kill = _BlockedTermination("os.kill")
    return saved


def _remove_runtime_blocks(saved: dict):
    import sys as _sys

    if saved.get("builtins.exit") is not None:
        builtins.exit = saved["builtins.exit"]
    if saved.get("builtins.quit") is not None:
        builtins.quit = saved["builtins.quit"]
    if saved.get("sys.exit") is not None:
        _sys.exit = saved["sys.exit"]
    if saved.get("os._exit") is not None:
        os._exit = saved["os._exit"]
    if saved.get("os.abort") is not None:
        os.abort = saved["os.abort"]
    if saved.get("os.kill") is not None:
        os.kill = saved["os.kill"]
