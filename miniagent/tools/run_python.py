"""``run_python``: execute a workspace .py file in-process (gated, RUN_PYTHON)."""

import hashlib

from .. import permissions as _perm
from .registry import tool


@tool(capability=_perm.RUN_PYTHON)
def run_python(ctx, path: str, args: list = None) -> dict:
    """Execute a .py file in-process inside the project workspace. NOT
    sandboxed. Requires the run_python permission. The script's
    stdout/stderr are captured and returned; interactive input (input() or
    sys.stdin) is disabled and fails fast with an error instead of
    prompting. Never write validation scripts that read stdin; script any
    answers the code under test would prompt for."""
    return ctx.runner.run(path, args or [])


@run_python.preview
def _(ctx, path: str = "", args: list = None, **_kw):
    argv = args or []
    source = None
    scan_name = path
    try:
        full = ctx.workspace.resolve(path)
        source = full.read_text(encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        scan_name = full.name
    except Exception:
        digest = "(unavailable)"

    if source is None:
        scan_line = "Static scan: unavailable (file could not be read)."
    else:
        findings = ctx.runner.static_scan(source, scan_name)
        if findings:
            scan_line = "Static scan findings (run will be refused): " + "; ".join(findings)
        else:
            scan_line = "Static scan found no known termination constructs."

    details = (
        f"File: {path}\n"
        f"Arguments: {argv}\n"
        f"SHA-256: {digest}\n\n"
        f"{scan_line}\n"
        "Interactive input: disabled at runtime — input() and\n"
        "sys.stdin reads raise an error instead of hanging.\n\n"
        "WARNING: This remains unsandboxed in-process execution."
    )
    return "RUN PYTHON", details
