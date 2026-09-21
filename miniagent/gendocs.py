"""Generate ``docs/reference.md`` from the live tool/command registries and
every module's own docstring (§5.7 of ``docs/rearchitecture.md``).

The package used to carry three independent, hand-written maps of the same
information — a tool table in ``README.md``, a "what lives where" table in
``docs/self_editing.md``, and a module diagram in ``docs/architecture.md`` —
and they drifted apart because nothing forced them to agree.  This module
replaces all three tables (the diagrams and prose in ``architecture.md``
stay hand-written; they explain *why*, which does not generate) with one
file that is produced *from the code itself*:

* the **tools table** comes from :mod:`miniagent.tools.registry` — the same
  registry that builds ``TOOL_SCHEMAS``, so the model's own tool list and
  this table can never disagree;
* the **commands table** comes from :mod:`miniagent.console.registry` — the
  same registry ``:help`` and the startup banner are generated from;
* the **module layout table** comes from walking every ``.py`` file in the
  package and reading the first sentence of its module docstring.  This is
  the part that "maintains itself": every module already documents its own
  purpose at the top for a human reading the source, so nothing new has to
  be written or kept in sync — a module that gets a better docstring gets a
  better reference table for free, and a module that gets no docstring shows
  up as missing one, which is itself useful signal.

Run it with the agent's own ``run_python`` tool, exactly as an on-device
agent would::

    run_python miniagent/gendocs.py

It is safe to run standalone with a plain ``python3`` too.  Either way it is
**standard library only** — no third-party imports, no network, no shell —
and it never starts the console loop: importing this module or calling
:func:`main` only reads source files and (re)writes
``miniagent/docs/reference.md``.

``miniagent/tests/test_reference.py`` regenerates the same content in memory
and asserts it matches the checked-in file byte for byte.  That test, not
this generator, is what actually prevents the drift from coming back: this
module only makes it *possible* to regenerate; the test makes it impossible
to forget to.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# This file lives at miniagent/gendocs.py, so its own parent is the package
# root and *that* directory's parent is where `import miniagent` resolves
# from (the "site-packages" directory in the terminology the rest of the
# package's docs use — see docs/self_editing.md).  Inserted defensively so
# this module works whether it is run as a bare script (`python3
# miniagent/gendocs.py`, or via run_python, which does the equivalent of
# this itself) or imported normally as `miniagent.gendocs`.
_PACKAGE_ROOT = Path(__file__).resolve().parent
_IMPORT_ROOT = _PACKAGE_ROOT.parent
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

# Directories/files excluded from the module-layout table: the test suite
# (it is documented in docs/testing.md, not here) and the template launcher
# script (it is not part of the importable package — see its own docstring).
_EXCLUDED_DIRS = {"tests"}
_EXCLUDED_FILES = {"_jeb.py"}

_HEADER = """\
<!--
    GENERATED FILE — DO NOT EDIT BY HAND.

    This file is produced by miniagent/gendocs.py from the tool registry,
    the command registry, and every module's own docstring.  To regenerate
    it after changing a tool, a command, or a module docstring, run:

        run_python miniagent/gendocs.py

    miniagent/tests/test_reference.py fails if this file is out of date;
    that is the check that actually enforces "regenerate it", not this
    comment.  See docs/rearchitecture.md §5.7 for why this file exists.
-->

# MiniAgent reference

Generated tables for the tool registry, the command registry, and the
package's module layout. Prose about *why* the system is built this way
lives in [`architecture.md`](architecture.md); this file only lists *what
currently exists*, read straight from the code.

"""

_SENTENCE_END_RE = re.compile(r"(.*?[.!?])(?:\s|$)")


def _first_sentence(paragraph: str) -> str:
    """Return the first sentence of *paragraph*, whitespace collapsed.

    *paragraph* may span multiple lines (e.g. one paragraph of a
    docstring); newlines and repeated whitespace are collapsed to single
    spaces first, matching how ``miniagent.tools.registry._clean_doc``
    treats a tool's own docstring, so both come out reading like normal
    prose.  A paragraph with no ``.``/``!``/``?`` is returned whole.
    """
    text = " ".join(paragraph.split())
    if not text:
        return ""
    match = _SENTENCE_END_RE.match(text)
    return match.group(1) if match else text


def _module_purpose(path: Path) -> str:
    """The first sentence of *path*'s module docstring, or a note if absent."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        doc = ast.get_docstring(tree, clean=True)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return f"(could not read docstring: {exc})"
    if not doc:
        return "(no module docstring)"
    first_paragraph = doc.split("\n\n", 1)[0]
    return _first_sentence(first_paragraph)


def _iter_package_modules(package_root: Path):
    """Yield every ``.py`` file under *package_root*, excluding tests/ and
    ``_jeb.py``, as paths relative to *package_root* with ``/`` separators.
    """
    paths = []
    for path in package_root.rglob("*.py"):
        rel = path.relative_to(package_root)
        if rel.parts[0] in _EXCLUDED_DIRS:
            continue
        if rel.name in _EXCLUDED_FILES:
            continue
        paths.append(rel)
    return sorted(paths, key=lambda p: p.as_posix())


def _md_escape(text: str) -> str:
    """Escape the handful of characters that would break a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _render_tools_table(tool_registry) -> str:
    lines = ["| Tool | Capability | Description |", "|---|---|---|"]
    for spec in tool_registry.all_specs():
        capability = spec.capability if spec.capability else "none"
        lines.append(
            f"| `{spec.name}` | {capability} | {_md_escape(spec.description)} |"
        )
    return "\n".join(lines)


def _render_commands_table(command_registry) -> str:
    lines = ["| Command | Aliases | Summary |", "|---|---|---|"]
    for spec in command_registry.all_specs():
        aliases = ", ".join(f"`:{a}`" for a in spec.aliases) if spec.aliases else "—"
        first_paragraph = spec.help.split("\n\n", 1)[0]
        summary = _first_sentence(first_paragraph)
        lines.append(f"| `:{spec.name}` | {aliases} | {_md_escape(summary)} |")
    return "\n".join(lines)


def _render_layout_table(package_root: Path) -> str:
    lines = ["| Module | Purpose |", "|---|---|"]
    for rel in _iter_package_modules(package_root):
        purpose = _module_purpose(package_root / rel)
        lines.append(f"| `{rel.as_posix()}` | {_md_escape(purpose)} |")
    return "\n".join(lines)


def _reimport_fresh():
    """Drop every ``miniagent.*`` module from ``sys.modules`` but this one.

    Run via ``run_python`` inside a *running* MiniAgent session, the
    interactive harness has already imported ``miniagent.tools`` and
    ``miniagent.console`` once — possibly before the very edits the agent
    is now trying to document.  Re-importing fresh (the same convention
    every file in ``miniagent/tests/`` follows, for the same reason) makes
    sure the registries reflect what is on disk right now, not what was
    loaded at session start.
    """
    self_name = __name__  # "miniagent.gendocs" when imported, "__main__" as a script
    for name in [
        n for n in list(sys.modules)
        if (n == "miniagent" or n.startswith("miniagent.")) and n != self_name
    ]:
        del sys.modules[name]


def render_reference() -> str:
    """Build the full contents of ``docs/reference.md`` as a string."""
    _reimport_fresh()

    import miniagent.console as _console_pkg  # noqa: F401  (registers every command)
    import miniagent.tools as _tools_pkg  # noqa: F401  (registers every tool)
    from miniagent.console import registry as command_registry
    from miniagent.tools import registry as tool_registry

    tool_count = len(tool_registry.all_specs())
    command_count = len(command_registry.all_specs())
    module_count = len(_iter_package_modules(_PACKAGE_ROOT))

    sections = [
        _HEADER.rstrip("\n"),
        f"## Tools ({tool_count})\n\n" + _render_tools_table(tool_registry),
        f"## Commands ({command_count})\n\n" + _render_commands_table(command_registry),
        f"## Module layout ({module_count} files)\n\n"
        + _render_layout_table(_PACKAGE_ROOT),
    ]
    return "\n\n".join(sections) + "\n"


def main():
    """Regenerate ``docs/reference.md`` in place and return its new text."""
    text = render_reference()
    out_path = _PACKAGE_ROOT / "docs" / "reference.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {out_path} ({len(text)} bytes)")
    return text


if __name__ == "__main__":
    main()
