"""Workspace confinement and low-level file operations.

Everything in this module operates strictly beneath a project root that is
supplied by the caller.  Paths that resolve outside that root are rejected.
"""

from __future__ import annotations

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

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise WorkspaceError(f"Project root does not exist: {self.root}")
        if not self.root.is_dir():
            raise WorkspaceError(f"Project root is not a directory: {self.root}")

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
        self._atomic_write(full, content)
        return {"ok": True, "path": self.rel(full), "bytes": len(content.encode("utf-8"))}

    def edit_file(self, path: str, old_text: str, new_text: str):
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        text = full.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise WorkspaceError("old_text not found in file")
        if occurrences > 1:
            raise WorkspaceError(
                f"old_text occurs {occurrences} times; edit requires a unique match"
            )
        updated = text.replace(old_text, new_text, 1)
        self._atomic_write(full, updated)
        return {
            "ok": True,
            "path": self.rel(full),
            "diff": _unified_diff(text, updated, self.rel(full)),
        }

    def overwrite_file(self, path: str, content: str):
        full = self.resolve(path)
        if not full.exists():
            raise WorkspaceError(f"File does not exist: {path}")
        if not full.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        old = full.read_text(encoding="utf-8")
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
        os.replace(str(full), str(target))
        return {"ok": True, "path": self.rel(full), "moved_to": self.rel(target)}

    # -- preview helpers (no mutation) -------------

    def preview_edit(self, path: str, old_text: str, new_text: str):
        full = self.resolve(path)
        if not full.exists() or not full.is_file():
            return f"(cannot preview: {path})"
        text = full.read_text(encoding="utf-8")
        if text.count(old_text) != 1:
            return f"(old_text occurs {text.count(old_text)} time(s); no unique match)"
        updated = text.replace(old_text, new_text, 1)
        return _unified_diff(text, updated, self.rel(full))

    def preview_overwrite(self, path: str, content: str):
        full = self.resolve(path)
        if not full.exists() or not full.is_file():
            return f"(cannot preview: {path})"
        old = full.read_text(encoding="utf-8")
        return _unified_diff(old, content, self.rel(full))

    # -- internals ---------------------------------

    @staticmethod
    def _atomic_write(path: Path, content: str):
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
