"""``:files``, ``:workspace`` and ``:context`` (the JEB.md inspector)."""

from __future__ import annotations

from pathlib import Path

from ... import jebmd
from ...workspace import WorkspaceError
from ..registry import command


@command("files")
def files_command(ctx, arg):
    """List top-level project files."""
    try:
        entries = ctx.workspace.list_files(".")
        for e in entries:
            tag = "/" if e["is_dir"] else ""
            print(f"  {e['path']}{tag}")
    except WorkspaceError as exc:
        print(exc)


@command("workspace")
def workspace_command(ctx, arg):
    """Print the project workspace path."""
    print(ctx.root)


@command("context")
def context_command(ctx, arg):
    """Show JEB.md files discovered (global + package + local) and the combined
    context that was added to the system prompt.
    """
    _print_jeb_context(ctx.root)


def _excerpt(text: str, width: int = 72) -> str:
    """Return the first line of *text*, truncated to *width* characters."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    if len(line) > width:
        line = line[: width - 3] + "..."
    return line


def _print_jeb_context(root: Path):
    """Show which JEB.md files were discovered and a content preview."""
    found = False
    for path in jebmd._global_jeb_md_paths():
        status = "found" if path.exists() else "missing"
        print(f"Global  : {path} ({status})")
        found = found or path.exists()

    if jebmd.is_self_edit(root):
        package_path = jebmd.package_jeb_md_path()
        status = "found" if package_path.exists() else "missing"
        print(f"Package : {package_path} ({status}, self-editing)")
        found = found or package_path.exists()

    local_path = Path(root).resolve() / jebmd.JEB_MD_NAME
    status = "found" if local_path.exists() else "missing"
    print(f"Local   : {local_path} ({status})")
    found = found or local_path.exists()

    if not found:
        print("No JEB.md files found (global, package or local).")
        return

    _text, _header, conflicts = jebmd._load_global_jeb_md()
    if conflicts:
        docs_path, legacy_path = jebmd._global_jeb_md_paths()
        print()
        print(f"Conflicts between the global files were resolved in favour of {docs_path}:")
        for legacy_unit, kept_unit in conflicts:
            print(f"  dropped from {legacy_path}: {_excerpt(legacy_unit)}")
            print(f"  kept instead: {_excerpt(kept_unit)}")

    print()
    print("Combined context sent to the system prompt:")
    print("-" * 68)
    context = jebmd.load_jeb_md_context(root)
    if context:
        print(context)
    else:
        print("(empty)")
    print("-" * 68)
