"""This install's Pythonista reference material, for the knowledge tool.

Two read-only resources behind one tool:

- The **knowledge base** — a small, user-maintained library shipped inside
  the package (``miniagent/knowledge/``): environment maps as markdown plus
  maintenance scripts the *user* runs by hand (``probe.py`` records fresh
  runtime facts; ``lookup_docs.py`` is the standalone ancestor of the docs
  action below).  Because it lives inside the package, every project's agent
  can read it through the tool, while the file tools of unrelated project
  workspaces cannot touch it.
- **Pythonista's bundled official documentation** — ``Documentation.inv``
  (a TSV symbol index: name, kind, doc path) and ``Documentation.zip``
  (the HTML pages) inside the app bundle.  :class:`PythonDocs` is a port of
  ``lookup_docs.py`` that searches them offline, so the agent consults the
  canonical docs — not the stale online copies — without network access and
  without executing anything.

The ``knowledge`` tool in ``tools.py`` exposes both.  ``list`` / ``read`` /
``search`` operate on the knowledge files through a
:class:`~miniagent.workspace.Workspace` rooted at the knowledge directory,
so they get exactly the confinement, formatting, and limits of the project
file tools: a knowledge path can never escape the knowledge root.  ``docs``
searches the bundled documentation.  ``docs`` does not touch the knowledge
directory, so it works even when that directory is missing.

Everything is strictly read-only: no action writes a file and no action
executes code, which is why the tool needs no permission gating at all.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .workspace import Workspace


class KnowledgeError(Exception):
    """Raised when a knowledge operation cannot be completed."""


# The knowledge base ships inside the package, next to this module.
KNOWLEDGE_DIRNAME = "knowledge"

# Docs lookups never return more than this many symbol matches.
_DOCS_MATCH_LIMIT = 40
# The readable text of a doc page is capped here (matches lookup_docs.py).
_DOCS_PAGE_CHARS = 8000
_IOS_DOC_PREFIX = "py3/ios/"
_DOC_SUFFIX = ".html"


def default_knowledge_dir() -> Path:
    """Return the knowledge directory shipped inside the package."""
    return Path(__file__).resolve().parent / KNOWLEDGE_DIRNAME


def _app_bundle_path():
    """Return the Pythonista app bundle directory, or ``None``."""
    try:
        from objc_util import NSBundle  # Pythonista built-in
        return str(NSBundle.mainBundle().bundlePath())
    except Exception:
        return None


def _strip_tags(html: str) -> str:
    """Reduce an HTML doc page to readable text (port of lookup_docs.py)."""
    body = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"&[a-zA-Z#0-9]+;", " ", body)
    return re.sub(r"[ \t]+", " ", body)


class Knowledge:
    """Read-only access to the knowledge base and the bundled docs.

    When *root* is omitted, the knowledge directory shipped inside the
    package is used.  When it does not exist (or an explicit *root* is not a
    directory), the instance is *unavailable*: ``available`` is False and the
    file operations raise :class:`KnowledgeError` with the location that was
    tried.  Construction never raises.  The ``docs`` method is independent of
    the knowledge directory and works regardless.
    """

    def __init__(self, root=None):
        wanted = Path(root) if root is not None else default_knowledge_dir()
        self.requested_root = str(wanted)
        if wanted.is_dir():
            self.root = wanted.resolve()
            # Workspace is generic over its root, so the knowledge files get
            # the identical confinement, formatting, and limits the project
            # file tools have — confined to the knowledge directory.
            self.workspace = Workspace(self.root)
        else:
            self.root = None
            self.workspace = None
        self._docs = None

    @property
    def available(self) -> bool:
        """True when the knowledge base directory exists."""
        return self.workspace is not None

    def _require(self) -> Workspace:
        if self.workspace is None:
            raise KnowledgeError(
                f"Knowledge base not available at {self.requested_root}. "
                "The miniagent package ships its knowledge/ directory next "
                "to its modules."
            )
        return self.workspace

    # -- knowledge files ------------------------------------------------

    def list(self, path: str = "."):
        """List knowledge files beneath *path* (always recursive)."""
        return self._require().list_files(path, recursive=True)

    def read(self, path: str, start_line=None, end_line=None):
        """Return *path*'s line-numbered contents, with optional range."""
        return self._require().read_file(
            path, start_line=start_line, end_line=end_line
        )

    def search(self, pattern, path=".", case_sensitive=False, regex=False,
               include=None, max_results=None):
        """Search knowledge file contents for *pattern*.

        Same matcher semantics and result shape as the ``search_files`` tool.
        """
        return self._require().search_files(
            pattern,
            path=path,
            case_sensitive=case_sensitive,
            regex=regex,
            include=include,
            max_results=max_results,
        )

    # -- bundled documentation ------------------------------------------

    def docs(self, query=None, page=None):
        """Search or read Pythonista's bundled official documentation.

        - *query*: ranked symbol matches from the documentation index
          (case-insensitive substring match; names starting with the query
          come first).
        - *page*: the readable text of one documentation page, e.g.
          ``"ui"`` or ``"py3/ios/appex.html"``.
        - neither: the list of Pythonista module documentation pages.
        - both: an error.

        Raises :class:`KnowledgeError` when the bundled documentation
        cannot be located (outside Pythonista, or a non-standard install).
        """
        if query and page:
            raise KnowledgeError("docs accepts 'query' or 'page', not both")
        if self._docs is None:
            self._docs = PythonDocs()
        if page:
            return self._docs.show(page)
        if query:
            return self._docs.search(query)
        return {
            "pages": self._docs.module_pages(),
            "hint": (
                "Pass query=<symbol> for ranked index matches, or "
                "page=<name> (e.g. 'ui' or 'py3/ios/appex.html') to read "
                "a doc page's text."
            ),
        }


class PythonDocs:
    """Offline search over Pythonista's bundled official documentation.

    A faithful port of the knowledge-base script ``lookup_docs.py``:
    ``Documentation.inv`` is a TSV symbol index (``name``, ``kind``,
    ``doc path``) and ``Documentation.zip`` holds the HTML pages.  The zip
    entry list and the parsed index are loaded lazily and cached on the
    instance.  All operations are read-only and raise :class:`KnowledgeError`
    with a clear message when the documentation assets are missing.
    """

    def __init__(self, zip_path=None, inv_path=None):
        if zip_path is None or inv_path is None:
            bundle = _app_bundle_path()
            if bundle is None:
                raise KnowledgeError(
                    "Cannot determine the Pythonista app bundle path "
                    "(objc_util missing) — bundled documentation unavailable."
                )
            base = Path(bundle)
            zip_path = zip_path or base / "Documentation.zip"
            inv_path = inv_path or base / "Documentation.inv"
        self.zip_path = Path(zip_path)
        self.inv_path = Path(inv_path)
        for missing in (self.zip_path, self.inv_path):
            if not missing.exists():
                raise KnowledgeError(
                    f"Bundled documentation not found: {missing}"
                )
        self._index = None
        self._zip_names = None

    # -- lazy assets -----------------------------------------------------

    def zip_names(self):
        """The documentation zip entry names, loaded once."""
        if self._zip_names is None:
            with zipfile.ZipFile(self.zip_path) as z:
                self._zip_names = frozenset(z.namelist())
        return self._zip_names

    def index(self):
        """The parsed symbol index as ``(name, kind, path)`` tuples."""
        if self._index is None:
            entries = []
            with open(self.inv_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 3:
                        entries.append((parts[0], parts[1], parts[2]))
            self._index = entries
        return self._index

    def resolve(self, path):
        """Find a real zip entry for a (possibly partial) doc path."""
        path = (path or "").split("#")[0]
        if not path:
            return None
        candidates = [path]
        if not path.endswith(_DOC_SUFFIX):
            candidates.append(path + _DOC_SUFFIX)
        for candidate in list(candidates):
            base = Path(candidate).name
            candidates.append(_IOS_DOC_PREFIX + base)
            candidates.append("py3/library/" + base)
        for candidate in candidates:
            if candidate in self.zip_names():
                return candidate
        return None

    # -- operations ------------------------------------------------------

    def search(self, query: str) -> dict:
        """Return ranked symbol matches for *query* from the index."""
        query = (query or "").strip()
        if not query:
            raise KnowledgeError("docs query must be a non-empty string")
        lowered = query.lower()
        matches = [
            entry for entry in self.index()
            if lowered in entry[0].lower()
        ]
        matches.sort(
            key=lambda entry: (not entry[0].lower().startswith(lowered),
                              entry[0].lower())
        )
        shown = matches[:_DOCS_MATCH_LIMIT]
        return {
            "query": query,
            "total": len(matches),
            "matches": [
                {
                    "name": name,
                    "kind": kind,
                    "path": self.resolve(doc_path) or doc_path,
                }
                for name, kind, doc_path in shown
            ],
            "truncated": len(matches) > len(shown),
        }

    def show(self, page: str) -> dict:
        """Return the readable text of the doc page at *page*."""
        target = self.resolve(page)
        if target is None:
            raise KnowledgeError(f"No doc page found for: {page!r}")
        with zipfile.ZipFile(self.zip_path) as z:
            html = z.read(target).decode("utf-8", "replace")
        return {"page": target, "content": _strip_tags(html)[:_DOCS_PAGE_CHARS]}

    def module_pages(self):
        """The Pythonista module doc pages (``py3/ios/*.html``) by name."""
        return sorted(
            name[len(_IOS_DOC_PREFIX):-len(_DOC_SUFFIX)]
            for name in self.zip_names()
            if name.startswith(_IOS_DOC_PREFIX) and name.endswith(_DOC_SUFFIX)
        )


__all__ = [
    "Knowledge",
    "KnowledgeError",
    "PythonDocs",
    "default_knowledge_dir",
]
