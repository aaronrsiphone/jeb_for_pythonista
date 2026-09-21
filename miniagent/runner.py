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

Interactive input is additionally **disabled for the duration of a run**:
``builtins.input`` and ``sys.stdin`` are replaced with objects that raise
:class:`InteractiveInputBlocked` instead of reading.  Run output is captured
into buffers, so a real prompt would be invisible, and a blocking read on the
real stdin would hang Pythonista with no in-process way to interrupt it —
prevention is the only strategy.  A script may rebind ``builtins.input``
itself to inject scripted answers; only the default interactive path is
blocked.  See ``docs/testing.md`` for the testing patterns around this.
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

        Returns a dict with ``stdout``, ``stderr`` and ``ok`` keys.  The
        script's stdout/stderr are captured into buffers, and interactive
        input (``input()`` / ``sys.stdin``) is disabled for the duration of
        the run: it fails fast with ``InteractiveInputBlocked`` instead of
        blocking forever on the invisible prompt.
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

    def static_scan(self, source: str, name: str = "<script>") -> list[str]:
        """AST-scan *source* and return human-readable findings.

        This is the static preflight as a pure function: it never raises for
        content findings.  ``_preflight`` enforces it (first finding wins),
        and the ``run_python`` permission preview in ``tools.py`` reuses it
        so the user sees the exact scan the runner will enforce.  A parse
        error is returned as a finding.
        """
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as exc:
            return [f"Cannot parse {name}: {exc}"]

        findings: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                dotted = _dotted_name(node.func)
                if dotted and dotted in _BLOCKED_CALLS:
                    findings.append(
                        f"Blocked termination call '{dotted}' in {name}"
                    )
            elif isinstance(node, ast.Raise):
                if node.exc is not None and isinstance(node.exc, ast.Call):
                    dotted = _dotted_name(node.exc.func)
                    if dotted == "SystemExit" or dotted == "BaseException":
                        findings.append(
                            f"Blocked 'raise SystemExit()' in {name}"
                        )
                elif isinstance(node.exc, ast.Name) and node.exc.id in (
                    "SystemExit",
                    "BaseException",
                ):
                    findings.append(f"Blocked 'raise SystemExit' in {name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {alias.name for alias in node.names}
                blocked = _BLOCKED_IMPORTS.get(module)
                if blocked:
                    hits = names & blocked
                    if hits:
                        label = (
                            "termination builtin import"
                            if module == "builtins"
                            else "direct sys.exit import"
                            if module == "sys"
                            else "process-control os import"
                        )
                        findings.append(
                            f"Blocked {label} in {name}: "
                            f"{', '.join(sorted(hits))}"
                        )
        return findings

    def _preflight(self, source: str, full: Path):
        """Enforce the static scan before execution (first finding wins)."""
        findings = self.static_scan(source, full.name)
        if findings:
            raise RunnerError(findings[0])

    # -- execution ----------------------------------------------------------

    def _execute(self, code_obj, full: Path, args: list) -> dict:
        root = str(self.workspace.root)

        old_argv = sys.argv
        old_cwd = os.getcwd()
        old_path = list(sys.path)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        old_stdin = sys.stdin
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
        # Interactive input is impossible under run_python: stdout is
        # captured, so a prompt would be invisible, and a blocking read on
        # the real stdin would hang Pythonista with no in-process way to
        # interrupt it.  Reads fail fast with a clear error instead.  A
        # script that wants scripted answers rebinds builtins.input itself
        # (see docs/testing.md); only the default interactive path is
        # blocked.
        sys.stdin = _BlockedStdin()
        builtins.input = _BlockedInput()

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
            sys.stdin = old_stdin
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


class InteractiveInputBlocked(RuntimeError):
    """Raised when a run script tries to read interactive input.

    Under run_python the script's stdout is captured, so a prompt would be
    invisible, and a blocking read on the real stdin would hang Pythonista
    with no in-process way to interrupt it.  The runner therefore replaces
    ``builtins.input`` and ``sys.stdin`` with objects that raise this error
    immediately, so a script that trips it fails in milliseconds with a
    clear traceback in the run result instead of hanging the app.  Tests
    that exercise prompting code should inject a scripted prompt — see
    ``docs/testing.md``.
    """


_INPUT_BLOCKED_MESSAGE = (
    "Interactive input is disabled under run_python: the script's output is "
    "captured, so a prompt would be invisible and the read would block "
    "forever, hanging Pythonista. If this script needs to test prompting "
    "code, inject a scripted prompt or rebind builtins.input yourself (see "
    "docs/testing.md in the miniagent package)."
)


class _BlockedInput:
    """Replaces ``builtins.input`` during a run; raises instead of blocking."""

    def __call__(self, *args, **kwargs):
        raise InteractiveInputBlocked(_INPUT_BLOCKED_MESSAGE)

    def __repr__(self):
        return "<input() disabled under run_python>"


class _BlockedStdin:
    """Replaces ``sys.stdin`` during a run; reads raise, nothing blocks.

    Read-like entry points raise :class:`InteractiveInputBlocked`
    immediately (including iteration and ``sys.stdin.buffer.read(...)``).
    Harmless metadata queries answer like a non-interactive stream so code
    that merely *asks* about stdin can proceed without reading it.
    """

    encoding = "utf-8"
    errors = "replace"
    closed = False

    def _blocked(self, *args, **kwargs):
        raise InteractiveInputBlocked(_INPUT_BLOCKED_MESSAGE)

    # Every read-like entry point fails fast instead of blocking.
    read = _blocked
    readline = _blocked
    readlines = _blocked
    read1 = _blocked
    __next__ = _blocked
    __iter__ = _blocked

    @property
    def buffer(self):
        # sys.stdin.buffer.read(...) hits the same loud failure.
        return self

    def close(self):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return False

    def seekable(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("stdin is disabled under run_python")

    def __repr__(self):
        return "<stdin disabled under run_python>"


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
