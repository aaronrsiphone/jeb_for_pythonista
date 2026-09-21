"""JEB.md discovery and the global/local merge engine.

``JEB.md`` is MiniAgent's standing-instructions file (the harness's answer to
a ``CLAUDE.md``): whatever it contains is folded into the system prompt.  Up
to three files can contribute — two candidate *global* locations plus one
*local*, project-scoped file — so this module also carries a small markdown
merge engine that combines the two global files when both exist, resolving
conflicts in favour of the more discoverable one.

Split out of ``app.py`` (§5.6) as its own module because it is the largest
single piece of self-contained logic that module used to carry, and because
splitting it out is what finally makes it testable in isolation
(``test_jebmd.py``) instead of only indirectly through ``:context``.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

JEB_MD_NAME = "JEB.md"
_MAX_JEB_MD_CHARS = 32_000

# When merging the two global JEB.md files, two units of text (list items
# or blocks) whose normalised text is at least this similar are treated as
# conflicting versions of the same instruction.  The Documents version wins.
_CONFLICT_SIMILARITY = 0.85

_HEADING_RE = re.compile(r"^#{1,6}\s")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Global JEB.md candidate directories, most preferred first.  The Documents
# version is preferred (it is user-visible in the Files app on iOS); the
# original location is kept for backward compatibility and merged in when
# present.
_DOCS_GLOBAL_DIR = ("Documents", "miniagent")
_LEGACY_GLOBAL_DIR = ("miniagent",)


def _read_jeb_md(path: Path) -> str:
    """Return the trimmed contents of *path*, or an empty string."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = text.strip()
    if len(text) > _MAX_JEB_MD_CHARS:
        removed = len(text) - _MAX_JEB_MD_CHARS
        text = text[:_MAX_JEB_MD_CHARS] + f"\n\n... [{removed} chars truncated]"
    return text


def _global_jeb_md_paths() -> list[Path]:
    """Return the candidate global ``JEB.md`` paths, most preferred first.

    1. The **Documents version** — ``~/Documents/miniagent/JEB.md``.
    2. The **original** — ``~/miniagent/JEB.md``.

    When both exist they are merged into a single global section with
    conflicts resolved in favour of the Documents version (see
    ``_merge_global_jeb_md_texts``).
    """
    home = Path.home()
    return [
        home.joinpath(*_DOCS_GLOBAL_DIR, JEB_MD_NAME),
        home.joinpath(*_LEGACY_GLOBAL_DIR, JEB_MD_NAME),
    ]


def _unit_key(unit_text: str) -> str:
    """Return the comparison key for a unit of JEB.md text."""
    return " ".join(unit_text.split()).casefold()


def _heading_key(heading: str) -> str:
    """Return the comparison key for a markdown heading line."""
    return _unit_key(heading.lstrip("#").strip())


def _is_hr_unit(unit_text: str) -> bool:
    """Return True if *unit_text* is a markdown horizontal rule."""
    stripped = unit_text.strip()
    return len(stripped) >= 3 and set(stripped) == {stripped[0]} and stripped[0] in "-*_"


def _split_jeb_md_sections(text: str) -> list:
    """Split *text* into heading-keyed sections of mergeable units.

    Returns a list of ``[heading, units]`` items in document order.
    *heading* is the ATX heading line that starts the section (``""`` for
    any preamble before the first heading).  *units* is a list of
    ``(is_list_item, unit_text)`` tuples, where a unit is either a single
    list item (together with its continuation lines) or a run of
    consecutive non-list, non-heading lines.
    """
    sections: list = []
    heading = ""
    units: list = []
    current: list = []
    current_is_item = False

    def flush_unit():
        nonlocal current, current_is_item
        if current:
            units.append((current_is_item, "\n".join(current)))
            current = []
            current_is_item = False

    def commit_section():
        nonlocal heading, units
        if units or heading:
            sections.append([heading, units])
        heading = ""
        units = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if _HEADING_RE.match(line):
            flush_unit()
            commit_section()
            heading = line
        elif not line.strip():
            flush_unit()
        elif _LIST_ITEM_RE.match(line):
            flush_unit()
            current = [line]
            current_is_item = True
        elif not current:
            current = [line]
            current_is_item = False
        else:
            current.append(line)

    flush_unit()
    commit_section()
    return sections


def _render_jeb_md_sections(sections: list) -> str:
    """Reassemble sections from ``_split_jeb_md_sections`` into markdown."""
    rendered: list = []
    for heading, units in sections:
        chunks: list = []
        prev_was_item = False
        for is_item, unit_text in units:
            if not chunks:
                chunks.append(unit_text)
            elif is_item and prev_was_item:
                chunks.append("\n" + unit_text)
            else:
                chunks.append("\n\n" + unit_text)
            prev_was_item = is_item
        body = "".join(chunks).strip()
        if heading and body:
            rendered.append(heading + "\n\n" + body)
        elif heading:
            rendered.append(heading)
        elif body:
            rendered.append(body)
    return "\n\n".join(rendered).strip()


def _merge_global_jeb_md_texts(docs_text: str, legacy_text: str):
    """Merge the two global ``JEB.md`` texts, resolving conflicts.

    *docs_text* (the Documents version) takes precedence over
    *legacy_text* (the original in ``~/miniagent/``).  Both are split
    into heading-keyed sections and merged unit by unit (a unit is a list
    item or a block of text):

    * a unit present in both files (ignoring case and whitespace) is kept
      once;
    * a legacy unit closely resembling a kept unit (similarity of at least
      ``_CONFLICT_SIMILARITY``) is treated as a *conflicting* version of
      the same instruction — the Documents version wins and the legacy
      unit is dropped;
    * everything else from the legacy file is kept: sections unique to it
      are appended after the Documents sections, and units unique to it
      are appended within their own section (before any trailing
      horizontal rule, so section separators stay at the end).

    Returns ``(merged_text, conflicts)`` where *conflicts* is a list of
    ``(legacy_unit, kept_unit)`` text pairs, one per conflict resolved in
    favour of the Documents version.
    """
    docs_sections = _split_jeb_md_sections(docs_text)
    legacy_sections = _split_jeb_md_sections(legacy_text)

    docs_index: dict = {}
    kept: list = []  # (key, text) of every kept unit, Documents first
    for position, (heading, units) in enumerate(docs_sections):
        docs_index.setdefault(_heading_key(heading), position)
        for _is_item, unit_text in units:
            kept.append((_unit_key(unit_text), unit_text))

    conflicts: list = []

    def keep_legacy_unit(unit_text: str) -> bool:
        """Return True when *unit_text* adds something new; record conflicts."""
        key = _unit_key(unit_text)
        for kept_key, kept_text in kept:
            if key == kept_key:
                return False  # exact duplicate of kept content
            similarity = difflib.SequenceMatcher(None, key, kept_key).ratio()
            if similarity >= _CONFLICT_SIMILARITY:
                conflicts.append((unit_text, kept_text))
                return False  # conflicting instruction: Documents version wins
        kept.append((key, unit_text))
        return True

    for heading, units in legacy_sections:
        kept_units = [u for u in units if keep_legacy_unit(u[1])]
        target = docs_index.get(_heading_key(heading))
        if target is not None:
            # Merge into the matching Documents section, before any
            # trailing horizontal rule so separators stay in place.
            insert_at = len(docs_sections[target][1])
            while (insert_at > 0
                   and _is_hr_unit(docs_sections[target][1][insert_at - 1][1])):
                insert_at -= 1
            docs_sections[target][1][insert_at:insert_at] = kept_units
        elif kept_units:
            docs_sections.append([heading, kept_units])

    return _render_jeb_md_sections(docs_sections), conflicts


def _load_global_jeb_md():
    """Read and merge the global ``JEB.md`` files.

    The Documents version (``~/Documents/miniagent/JEB.md``) is preferred.
    When the original (``~/miniagent/JEB.md``) also exists the two texts
    are merged into one, with conflicts resolved in favour of the
    Documents version.

    Returns ``(text, header, conflicts)``: the merged global text (empty
    when neither file exists), the section header that labels it in the
    system prompt, and the list of resolved conflicts.
    """
    docs_path, legacy_path = _global_jeb_md_paths()
    docs_text = _read_jeb_md(docs_path)
    legacy_text = _read_jeb_md(legacy_path)

    if docs_text and legacy_text:
        text, conflicts = _merge_global_jeb_md_texts(docs_text, legacy_text)
        header = "# Global JEB.md (~/Documents/miniagent merged with ~/miniagent)"
    elif docs_text:
        text, conflicts = docs_text, []
        header = "# Global JEB.md (~/Documents/miniagent)"
    elif legacy_text:
        text, conflicts = legacy_text, []
        header = "# Global JEB.md (~/miniagent)"
    else:
        text, header, conflicts = "", "", []
    return text, header, conflicts


def package_jeb_md_path() -> Path:
    """Return the path of the running package's own ``JEB.md``."""
    return Path(__file__).resolve().parent / JEB_MD_NAME


def is_self_edit(project_root) -> bool:
    """Return True when the running package lives inside *project_root*.

    This is the self-editing case: the workspace the agent has been pointed
    at contains the very package that is running it — which is what happens
    when the workspace is Pythonista's ``site-packages`` and ``miniagent/``
    sits inside it.  Detecting it is what lets the package's own standing
    instructions reach the agent (see :func:`load_jeb_md_context`); without
    the check, a shipped ``miniagent/JEB.md`` would only ever be found when
    the workspace happened to be the package directory itself.
    """
    package_dir = package_jeb_md_path().parent
    root = Path(project_root).resolve()
    if package_dir == root:
        return False  # the ordinary local lookup already covers this
    try:
        package_dir.relative_to(root)
    except ValueError:
        return False
    return True


def load_jeb_md_context(project_root: Path) -> str:
    """Discover and concatenate ``JEB.md`` context for *project_root*.

    Four locations are checked, in order:

    1. **Global, Documents version** — a ``JEB.md`` in
       ``~/Documents/miniagent/``.  Its content is included first.
    2. **Global, original** — a ``JEB.md`` in ``~/miniagent/``.  When both
       global files exist they are merged into a single section, with
       conflicts resolved in favour of the Documents version
       (``_merge_global_jeb_md_texts``); otherwise whichever file exists
       is used on its own.
    3. **Package** — the running package's own ``JEB.md``, included only
       when the package lives *inside* the workspace (:func:`is_self_edit`),
       i.e. the agent has been pointed at a directory containing the harness
       that is running it.  These are the self-editing rules, and this is
       what gets them into the system prompt automatically instead of
       relying on the agent thinking to read ``docs/self_editing.md``.
       Skipped when the workspace *is* the package directory, because the
       local lookup below already finds the same file.
    4. **Local** — a ``JEB.md`` in the project workspace root.  Most
       specific, so it comes last.

    Any of them may be absent.  The sections are separated by a labelled
    divider so the model can tell them apart.  An empty string is returned
    when no file exists.
    """
    parts: list[str] = []

    global_text, global_header, _conflicts = _load_global_jeb_md()
    if global_text:
        parts.append(global_header + "\n\n" + global_text)

    if is_self_edit(project_root):
        package_text = _read_jeb_md(package_jeb_md_path())
        if package_text:
            parts.append(
                "# Package JEB.md (self-editing: the harness is inside this "
                "workspace)\n\n" + package_text
            )

    local_path = Path(project_root).resolve() / JEB_MD_NAME
    local_text = _read_jeb_md(local_path)
    if local_text:
        parts.append("# Project JEB.md (workspace)\n\n" + local_text)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)
