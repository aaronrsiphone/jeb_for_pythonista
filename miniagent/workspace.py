"""Workspace confinement and low-level file operations.

Everything in this module operates strictly beneath a project root that is
supplied by the caller.  Paths that resolve outside that root are rejected.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import tempfile
from pathlib import Path

# Limits for content search so a single tool call cannot balloon.
_SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024
_SEARCH_LINE_CLIP = 300
_SEARCH_DEFAULT_MAX_RESULTS = 200

# Name of the recoverable trash folder that clean_up moves files into.
TRASH_DIRNAME = "to_delete"


class WorkspaceError(Exception):
    """Raised when a workspace operation cannot be completed."""


class Workspace:
    """A confined directory tree exposed to the coding tools.

    The workspace root is canonicalised once at construction time.  Every
    subsequent path argument is resolved relative to that root and checked to
    ensure it does not escape it.
    """

    def __init__(self, root, checkpoints=None):
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise WorkspaceError(f"Project root does not exist: {self.root}")
        if not self.root.is_dir():
            raise WorkspaceError(f"Project root is not a directory: {self.root}")
        # Optional collaborator (§5.2): when set, every mutation below
        # snapshots the file's prior state before touching it, so a turn can
        # be undone.  Left None by default so callers that must never
        # checkpoint (knowledge.py's Workspace, rooted at the knowledge
        # directory, and every existing caller/test that predates this)
        # keep working unchanged.
        self.checkpoints = checkpoints

    # -- path handling -----------------------------

    def resolve(self, path: str) -> Path:
        """Resolve *path* against the workspace root and confine it.

        Absolute paths are accepted only when they already live inside the
        root.  Relative paths are anchored at the root.  Symlinks are resolved
        before the confinement check so that ``../`` style escapes are caught
        even when masked by symlinks.
        """
        p = Path(path)
        if p.is_absolute():
            candidate = p
        else:
            candidate = self.root / path
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise WorkspaceError(
                f"Path '{path}' escapes workspace root {self.root}"
            )
        return resolved

    def rel(self, path: Path) -> str:
        """Return *path* expressed relative to the workspace root."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # -- file operations ---------------------------

    def list_files(self, path: str = ".", recursive: bool = False, max_depth=None):
        root = self.resolve(path)
        if not root.exists():
            raise WorkspaceError(f"Path does not exist: {path}")
        if not root.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")

        depth = None
        if recursive:
            depth = max_depth if isinstance(max_depth, int) else 1000

        entries = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root)
                cur_depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth is not None and cur_depth > depth:
                    dirnames[:] = []
                    continue
                for name in sorted(dirnames):
                    entries.append(self._entry(os.path.join(dirpath, name)))
                for name in sorted(filenames):
                    entries.append(self._entry(os.path.join(dirpath, name)))
        else:
            for name in sorted(os.listdir(root)):
                entries.append(self._entry(os.path.join(root, name)))

        entries.sort(key=lambda e: (not e["is_dir"], e["path"].lower()))
        return entries

    def _entry(self, full):
        rel = self.rel(Path(full))
        return {
            "path": rel,
            "is_dir": os.path.isdir(full),
            "size": os.path.getsize(full) if os.path.isfile(full) else 0,
        }

    def read_file(self, path: str, start_line=None, end_line=None):
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        text = full.read_text(encoding="utf-8")
        lines = text.splitlines()
        total = len(lines)
        s = 1
        e = total
        if isinstance(start_line, int):
            s = max(1, start_line)
        if isinstance(end_line, int):
            e = min(total, end_line)
        if s > 1 or e < total:
            sel = lines[s - 1 : e]
        else:
            sel = lines
        numbered = "\n".join(
            f"{i + s:6d} | {line}" for i, line in enumerate(sel)
        )
        header = f"Path: {self.rel(full)}  Lines: {s}-{e} of {total}"
        body = numbered if numbered else "(empty file)"
        return header + "\n" + body

    def search_files(self, pattern, path=".", case_sensitive=False,
                     regex=False, include=None, max_results=200):
        """Search file contents beneath *path* for *pattern*.

        Returns a dict with ``matches`` (a list of ``path`` / ``line`` /
        ``text`` entries), ``files_searched``, ``files_skipped`` and a
        ``truncated`` flag.  Matching is a case-insensitive literal substring
        search unless *case_sensitive* or *regex* is requested.  Files that
        are not valid UTF-8, that are very large, or whose resolved path
        escapes the workspace root are skipped and counted, never followed.
        """
        if not isinstance(pattern, str) or not pattern:
            raise WorkspaceError("Search pattern must be a non-empty string")

        root = self.resolve(path)
        if not root.exists():
            raise WorkspaceError(f"Path does not exist: {path}")
        if not root.is_dir():
            raise WorkspaceError(f"Not a directory: {path}")

        matcher = _compile_search_matcher(pattern, case_sensitive, regex)

        if (isinstance(max_results, bool) or not isinstance(max_results, int)
                or max_results <= 0):
            max_results = _SEARCH_DEFAULT_MAX_RESULTS
        include = include if isinstance(include, str) and include else None

        matches = []
        files_searched = 0
        files_skipped = 0
        truncated = False

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if include and not fnmatch.fnmatch(name, include):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    confined = self.resolve(full)
                except WorkspaceError:
                    files_skipped += 1
                    continue
                try:
                    if os.path.getsize(confined) > _SEARCH_MAX_FILE_BYTES:
                        files_skipped += 1
                        continue
                    with open(confined, "r", encoding="utf-8") as f:
                        text = f.read()
                except (OSError, UnicodeDecodeError):
                    files_skipped += 1
                    continue
                files_searched += 1
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if matcher(line):
                        matches.append({
                            "path": self.rel(confined),
                            "line": lineno,
                            "text": _clip_line(line),
                        })
                        if len(matches) >= max_results:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        return {
            "matches": matches,
            "files_searched": files_searched,
            "files_skipped": files_skipped,
            "truncated": truncated,
        }

    def create_file(self, path: str, content: str):
        full = self.resolve(path)
        if full.exists():
            raise WorkspaceError(f"File already exists: {path}")
        full.parent.mkdir(parents=True, exist_ok=True)
        self._snapshot(full)
        self._atomic_write(full, content)
        return {"ok": True, "path": self.rel(full), "bytes": len(content.encode("utf-8"))}

    def edit_file(self, path: str, old_text: str, new_text: str,
                  replace_all: bool = False, occurrence=None):
        """Replace text in an existing file.

        By default ``old_text`` must occur exactly once (the historical
        behaviour).  ``replace_all=True`` replaces every occurrence;
        ``occurrence=N`` (1-based) replaces only the Nth.  The two are
        mutually exclusive.  On a non-unique match with neither given, the
        error names every occurrence's line number so the model can pick one
        instead of guessing again.  On no match at all, the error shows the
        closest-matching region of the file and how it differs, since most
        failures are a whitespace or line-ending mismatch that is obvious
        once shown.
        """
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        text = full.read_text(encoding="utf-8")
        updated, count = _apply_edit(
            text, old_text, new_text, self.rel(full),
            replace_all=replace_all, occurrence=occurrence,
        )
        self._snapshot(full)
        self._atomic_write(full, updated)
        return {
            "ok": True,
            "path": self.rel(full),
            "replacements": count,
            "diff": _unified_diff(text, updated, self.rel(full)),
        }

    def multi_edit(self, path: str, edits):
        """Apply a list of ``{old_text, new_text}`` edits to one file atomically.

        Edits are applied in order against the file's running content, each
        one requiring a unique match at the point it is applied (the default
        ``edit_file`` semantics — no ``replace_all``/``occurrence`` here).
        Either every edit applies and the file is written once, or the first
        failing edit aborts the whole call and the file is left byte-for-byte
        untouched: nothing is written until every edit has already been
        validated against the in-memory text.
        """
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        if not isinstance(edits, list) or not edits:
            raise WorkspaceError("multi_edit requires a non-empty 'edits' list")

        original = full.read_text(encoding="utf-8")
        current = original
        total = 0
        for i, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                raise WorkspaceError(f"edit #{i} is not an object")
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise WorkspaceError(f"edit #{i} is missing a non-empty 'old_text'")
            if not isinstance(new_text, str):
                raise WorkspaceError(f"edit #{i} is missing 'new_text'")
            try:
                current, count = _apply_edit(current, old_text, new_text, self.rel(full))
            except WorkspaceError as exc:
                raise WorkspaceError(f"edit #{i} failed: {exc}")
            total += count

        # Nothing above has touched disk: every edit is validated against
        # `current` in memory first, so a mid-list failure never reaches here.
        self._snapshot(full)
        self._atomic_write(full, current)
        return {
            "ok": True,
            "path": self.rel(full),
            "edits_applied": len(edits),
            "replacements": total,
            "diff": _unified_diff(original, current, self.rel(full)),
        }

    def overwrite_file(self, path: str, content: str):
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        old = full.read_text(encoding="utf-8")
        self._snapshot(full)
        self._atomic_write(full, content)
        return {
            "ok": True,
            "path": self.rel(full),
            "diff": _unified_diff(old, content, self.rel(full)),
        }

    # -- clean-up (recoverable trash) --------------

    def trash_dir(self) -> Path:
        """The workspace folder where ``clean_up`` parks removed files."""
        return self.root / TRASH_DIRNAME

    def clean_up(self, path: str):
        """Move the file at *path* into the workspace ``to_delete`` folder.

        Nothing is deleted: the file is moved under a collision-free name
        (``name-1.ext``, ``name-2.ext``, ...) so it can be inspected or
        restored before the folder is emptied.  Paths inside ``to_delete``
        itself are refused.
        """
        full = self.resolve(path)
        trash = self.trash_dir()
        if full == trash or trash in full.parents:
            raise WorkspaceError("Refusing to clean up: path is inside to_delete")
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        trash.mkdir(parents=True, exist_ok=True)
        target = _unique_path(trash / full.name)
        # clean_up moves the file rather than editing it, but it is still a
        # mutation of *full*'s original location: undoing the turn should put
        # the file back where it was, not just leave the trash copy behind.
        self._snapshot(full)
        os.replace(str(full), str(target))
        return {"ok": True, "path": self.rel(full), "moved_to": self.rel(target)}

    # -- preview helpers (no mutation) -------------

    def preview_edit(self, path: str, old_text: str, new_text: str,
                      replace_all: bool = False, occurrence=None):
        full = self.resolve(path)
        if not full.exists() or not full.is_file():
            return f"(cannot preview: {path})"
        text = full.read_text(encoding="utf-8")
        try:
            updated, count = _apply_edit(
                text, old_text, new_text, self.rel(full),
                replace_all=replace_all, occurrence=occurrence,
            )
        except WorkspaceError as exc:
            return f"(cannot preview: {exc})"
        preview = _diff_preview(text, updated, self.rel(full))
        if count != 1:
            preview = f"{count} replacement(s)\n\n{preview}"
        return preview

    def preview_multi_edit(self, path: str, edits):
        full = self.resolve(path)
        if not full.exists() or not full.is_file():
            return f"(cannot preview: {path})"
        if not isinstance(edits, list) or not edits:
            return "(cannot preview: 'edits' must be a non-empty list)"
        text = full.read_text(encoding="utf-8")
        current = text
        for i, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                return f"(cannot preview: edit #{i} is not an object)"
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                return f"(cannot preview: edit #{i} is missing 'old_text')"
            if not isinstance(new_text, str):
                return f"(cannot preview: edit #{i} is missing 'new_text')"
            try:
                current, _count = _apply_edit(current, old_text, new_text, self.rel(full))
            except WorkspaceError as exc:
                return f"(cannot preview: edit #{i} would fail: {exc})"
        header = f"{len(edits)} edit(s), combined diff:"
        return f"{header}\n\n{_diff_preview(text, current, self.rel(full))}"

    def preview_overwrite(self, path: str, content: str):
        full = self.resolve(path)
        if not full.exists() or not full.is_file():
            return f"(cannot preview: {path})"
        old = full.read_text(encoding="utf-8")
        return _diff_preview(old, content, self.rel(full))

    # -- checkpoint hook ----------------------------

    def _snapshot(self, full: Path):
        """Ask the checkpoints collaborator to record *full*'s prior state.

        A no-op when no ``Checkpoints`` is attached.  ``Checkpoints.snapshot``
        already never raises (it disables itself on failure instead), but the
        call is still wrapped here so a workspace mutation can never be
        broken by its safety net misbehaving.
        """
        if self.checkpoints is None:
            return
        try:
            self.checkpoints.snapshot(full)
        except Exception:
            pass

    # -- internals ---------------------------------

    @staticmethod
    def _atomic_write(path: Path, content: str):
        # Compile gate (§5.2): a write to a *.py path is compiled before the
        # atomic rename that would make it live.  This is about the cheapest
        # possible defence against the worst self-edit outcome — a harness
        # that will not start — and it hands the model a file/line/problem it
        # can act on immediately instead of discovering the breakage on the
        # next launch. Checked before anything on disk is touched (no temp
        # file is even created), so a rejected write leaves nothing to clean
        # up and the original file exactly as it was.
        if path.suffix == ".py":
            try:
                compile(content, str(path), "exec")
            except SyntaxError as exc:
                lineno = exc.lineno if exc.lineno is not None else "?"
                problem = exc.msg or str(exc)
                raise WorkspaceError(
                    f"Refusing to write {path}: syntax error at line "
                    f"{lineno}: {problem}"
                )

        d = str(path.parent)
        tmp_dir = d if os.path.isdir(d) else None
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".tmp", prefix=".mw_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def _find_all(text: str, sub: str) -> list:
    """Return the start offsets of every non-overlapping occurrence of *sub*.

    Matches the semantics of ``str.count``/``str.replace`` (non-overlapping,
    left to right), so the positions returned line up with what ``count``
    reports and what ``replace`` would touch.
    """
    positions = []
    start = 0
    step = max(len(sub), 1)
    while True:
        idx = text.find(sub, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + step
    return positions


def _line_of(text: str, offset: int) -> int:
    """Return the 1-based line number containing character *offset*."""
    return text.count("\n", 0, offset) + 1


def _apply_edit(text: str, old_text: str, new_text: str, label: str,
                 replace_all: bool = False, occurrence=None):
    """Apply one ``old_text`` -> ``new_text`` replacement to *text*.

    Returns ``(updated_text, replacement_count)``.  With neither
    ``replace_all`` nor ``occurrence``, ``old_text`` must occur exactly once
    (the historical behaviour) — a non-unique match reports every occurrence's
    line number instead of just refusing, and no match at all reports the
    closest-matching region of the file with what differs.  *label* names the
    file in that error, e.g. ``self.rel(full)``.
    """
    if replace_all and occurrence is not None:
        raise WorkspaceError("replace_all and occurrence are mutually exclusive")
    if occurrence is not None and (
        isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1
    ):
        raise WorkspaceError("occurrence must be a positive integer (1-based)")

    positions = _find_all(text, old_text)
    count = len(positions)

    if count == 0:
        raise WorkspaceError(_no_match_message(text, old_text, label))

    if replace_all:
        return text.replace(old_text, new_text), count

    if occurrence is not None:
        if occurrence > count:
            raise WorkspaceError(
                f"occurrence {occurrence} requested but old_text occurs only "
                f"{count} time(s) in {label}"
            )
        idx = positions[occurrence - 1]
        updated = text[:idx] + new_text + text[idx + len(old_text):]
        return updated, 1

    if count > 1:
        lines = ", ".join(str(_line_of(text, pos)) for pos in positions)
        raise WorkspaceError(
            f"old_text occurs {count} times in {label} (lines {lines}); "
            "pass replace_all=true to replace every occurrence, or "
            "occurrence=<1-based index> to pick one"
        )

    return text.replace(old_text, new_text, 1), 1


# Cap on how many line-window candidates the near-miss search compares
# against, so a single failed edit on a very large file cannot make the
# error message itself expensive to produce.
_CLOSEST_MATCH_MAX_LINES = 4000


def _no_match_message(text: str, old_text: str, label: str) -> str:
    """Build the "old_text not found" error, naming the closest region.

    Most real failures are a whitespace or line-ending mismatch, and the
    model can fix those in one step if it is shown what differs — so this
    looks for the file region that most resembles ``old_text`` (by
    character-level similarity over a few candidate line-window sizes) and
    includes a diff against it, rather than just saying "not found".
    """
    file_lines = text.splitlines()
    if not file_lines:
        return f"old_text not found in {label} (file is empty)"

    old_lines = old_text.splitlines() or [old_text]
    window = max(1, len(old_lines))
    search_lines = file_lines[:_CLOSEST_MATCH_MAX_LINES]

    best_ratio = -1.0
    best_start = 0
    best_size = min(window, len(search_lines))
    for size in sorted({max(1, window - 1), window, window + 1, len(search_lines)}):
        if size <= 0 or size > len(search_lines):
            continue
        for start in range(0, len(search_lines) - size + 1):
            candidate = "\n".join(search_lines[start:start + size])
            ratio = difflib.SequenceMatcher(None, candidate, old_text).ratio()
            if ratio > best_ratio:
                best_ratio, best_start, best_size = ratio, start, size

    snippet = "\n".join(search_lines[best_start:best_start + best_size])
    region = f"{label}:{best_start + 1}-{best_start + best_size}"
    diff = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(), snippet.splitlines(),
            fromfile="old_text", tofile=region, lineterm="",
        )
    )
    return (
        f"old_text not found in {label}. Closest match is {region} "
        f"({best_ratio:.0%} similar):\n{diff}"
    )


def _unique_path(target: Path) -> Path:
    """Return *target*, or the first free ``name-1``, ``name-2``, ... variant."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(1, 1000):
        candidate = target.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise WorkspaceError(f"Cannot find a free name in {TRASH_DIRNAME}")


def _unified_diff(old: str, new: str, label: str, max_lines: int = 400) -> str:
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=label,
        tofile=label,
    )
    out = []
    n = 0
    for line in diff:
        out.append(line if line.endswith("\n") else line + "\n")
        n += 1
        if n >= max_lines:
            out.append("...(diff truncated)...\n")
            break
    return "".join(out) or "(no changes)"


def _diff_stat(old: str, new: str) -> str:
    """Return a short summary like ``"+6 -2"`` for the change from *old* to *new*."""
    import difflib

    added = 0
    removed = 0
    for line in difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
    ):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"+{added} -{removed}"


def _diff_preview(old: str, new: str, label: str, max_lines: int = 60) -> str:
    """Return a human-facing preview: a stat-line header, then a capped diff.

    Used only by ``preview_edit``/``preview_overwrite`` for the permission
    prompt shown to the human; never sent to the model.
    """
    diff_text = _unified_diff(old, new, label, max_lines=max_lines)
    stat = _diff_stat(old, new)
    hunks = sum(1 for line in diff_text.splitlines() if line.startswith("@@"))
    header = f"{label}   {stat}   ({hunks} hunk(s))"
    return f"{header}\n\n{diff_text}"


def _compile_search_matcher(pattern, case_sensitive, regex):
    """Return a predicate over lines that matches *pattern*.

    Literal search is a substring test; regex search compiles the pattern
    once.  Case-insensitivity is the default for both modes.
    """
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise WorkspaceError(f"Invalid regular expression: {exc}")
        return compiled.search
    if case_sensitive:
        return lambda line: pattern in line
    lowered = pattern.lower()
    return lambda line: lowered in line.lower()


def _clip_line(line: str, limit: int = _SEARCH_LINE_CLIP) -> str:
    """Trim an over-long matched line so one match cannot flood the result."""
    if len(line) <= limit:
        return line
    return line[:limit] + "...[line truncated]"
