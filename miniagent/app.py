"""High-level construction and the interactive console loop.

``run(project_root)`` is the single public entry point.  Importing this module
does not start a session; all construction happens inside ``run()``.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from pathlib import Path

from .agent import Agent, build_system_prompt
from .config import Config, default_state_dir
from .permissions import Permissions
from .provider import Provider
from .runner import Runner
from .sessions import SessionLogger, SessionError
from .tools import Tools
from .vision import Vision
from .workspace import Workspace, WorkspaceError

KEYCHAIN_ACCOUNT = "api_key"

_BANNER = """
=================================================
 MiniAgent — coding agent for: {root}
 Endpoint: {endpoint}
 Model: {model}
 Commands:
   :help  :config  :key  :model
   :perms  :reset  :clear-perms
   :effort  :files  :context  :resume  :quit
=================================================
"""

_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _get_keychain():
    try:
        import keychain  # Pythonista built-in
        return keychain
    except ImportError:
        return None


def _keychain_service(base_url: str) -> str:
    """Legacy keychain service name, keyed by a hash of the API base URL.

    Kept so that keys stored by older MiniAgent versions (and the original
    jeb.py) can be found again and migrated to the per-provider scheme.
    """
    digest = hashlib.sha256((base_url or "").encode("utf-8")).hexdigest()[:16]
    return f"MiniAgent:{digest}"


def _provider_service(name: str) -> str:
    """Keychain service name for one provider's API key."""
    return f"MiniAgent:provider:{name}"


def _load_api_key(config: Config, provider: str | None = None) -> str:
    """Return the API key for *provider* (default: the selected provider).

    Keys are stored per provider.  When a provider has no key yet but a
    legacy entry (keyed by base-URL hash) exists for its endpoint, that
    entry is copied over to the per-provider service.  *provider* may name
    any configured provider, not just the selected one — the vision tool
    uses this to load the key of its own provider.
    """
    name = provider if provider else config.provider
    kc = _get_keychain()
    if kc is None:
        return os.environ.get("MINIAGENT_API_KEY", "")
    service = _provider_service(name)
    try:
        key = kc.get_password(service, KEYCHAIN_ACCOUNT) or ""
    except Exception:
        key = ""
    if key:
        return key
    # The provider's own endpoint decides which legacy entry to migrate.
    settings = (getattr(config, "providers", {}) or {}).get(name) or {}
    base_url = settings.get("base_url") or getattr(config, "base_url", "") or ""
    legacy_service = _keychain_service(base_url)
    try:
        legacy_key = kc.get_password(legacy_service, KEYCHAIN_ACCOUNT) or ""
    except Exception:
        legacy_key = ""
    if legacy_key:
        try:
            kc.set_password(service, KEYCHAIN_ACCOUNT, legacy_key)
        except Exception:
            pass
    return legacy_key


def _store_api_key(config: Config, value: str):
    kc = _get_keychain()
    if kc is None:
        os.environ["MINIAGENT_API_KEY"] = value
        return False
    service = _provider_service(config.provider)
    try:
        kc.set_password(service, KEYCHAIN_ACCOUNT, value)
        return True
    except Exception:
        return False


def _secure_input(prompt: str) -> str:
    """Secure input that hides what is typed (for API keys)."""
    try:
        import console
        if hasattr(console, "secure_input"):
            return console.secure_input(prompt)
    except ImportError:
        pass
    try:
        import getpass
        return getpass.getpass(prompt)
    except Exception:
        try:
            return input(prompt)
        except EOFError:
            return ""


def _ensure_api_key(config: Config) -> str:
    """Return the stored API key, prompting interactively if none exists.

    Mirrors the original jeb.py behaviour: if auth is enabled and no key is
    found in the keychain, the user is prompted once and the key is stored.
    """
    if not config.auth_enabled:
        return ""

    key = _load_api_key(config)
    if key:
        return key

    print(f"No API key found in the keychain for provider '{config.provider}'.")
    value = _secure_input(
        "API key (will be stored in Pythonista keychain): "
    ).strip()

    if not value:
        print("WARNING: no API key provided; requests will be unauthenticated.")
        return ""

    stored = _store_api_key(config, value)
    if stored:
        print("API key stored in keychain.")
    else:
        print("API key stored in environment (keychain unavailable).")
    return value


# ---------------------------------------------------
# JEB.md discovery
# ---------------------------------------------------

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


def load_jeb_md_context(project_root: Path) -> str:
    """Discover and concatenate ``JEB.md`` context for *project_root*.

    Three locations are checked, in order:

    1. **Global, Documents version** — a ``JEB.md`` in
       ``~/Documents/miniagent/``.  Its content is included first.
    2. **Global, original** — a ``JEB.md`` in ``~/miniagent/``.  When both
       global files exist they are merged into a single section, with
       conflicts resolved in favour of the Documents version
       (``_merge_global_jeb_md_texts``); otherwise whichever file exists
       is used on its own.
    3. **Local** — a ``JEB.md`` in the project workspace root.  Its content
       is included after the global content.

    Any of them may be absent.  The sections are separated by a labelled
    divider so the model can tell them apart.  An empty string is returned
    when no file exists.
    """
    parts: list[str] = []

    global_text, global_header, _conflicts = _load_global_jeb_md()
    if global_text:
        parts.append(global_header + "\n\n" + global_text)

    local_path = Path(project_root).resolve() / JEB_MD_NAME
    local_text = _read_jeb_md(local_path)
    if local_text:
        parts.append("# Project JEB.md (workspace)\n\n" + local_text)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


def run(project_root):
    """Launch the interactive MiniAgent console for *project_root*."""
    root = Path(project_root).resolve()
    if not root.exists():
        print(f"Project root does not exist: {root}")
        return
    if not root.is_dir():
        print(f"Project root is not a directory: {root}")
        return

    state_dir = default_state_dir()
    config = Config.load(state_dir)

    # First-run setup: import legacy config or prompt interactively.
    if not config.exists():
        config.first_run_setup(legacy_dir=str(root))

    api_key = _ensure_api_key(config)

    try:
        workspace = Workspace(root)
    except WorkspaceError as exc:
        print(exc)
        return

    permissions = Permissions(state_dir, str(root))
    runner = Runner(workspace)
    # Vision collaborator for the ask_image tool: it resolves its own
    # provider/model from config.json's "vision_model" key and loads that
    # provider's API key through the same keychain scheme as the main chat.
    vision = Vision(config, load_key=lambda name: _load_api_key(config, name))
    tools = Tools(workspace, permissions, runner, vision=vision)
    provider = Provider(config, api_key)
    session_logger = SessionLogger(
        root,
        state_dir=state_dir,
        provider=config.provider,
        model=config.model,
    )
    jeb_context = load_jeb_md_context(root)
    agent = Agent(provider, tools, build_system_prompt(str(root), jeb_context),
                  recorder=session_logger)

    _console_loop(agent, config, permissions, workspace, root, provider,
                  session_logger)


# ---------------------------------------------------
# Console loop
# ---------------------------------------------------

def _console_loop(agent, config, permissions, workspace, root, provider,
                  session_logger):
    endpoint = provider.base_url.rstrip("/") + "/" + provider.chat_path.lstrip("/")
    model_label = (
        f"{config.provider}/{config.model}" if config.provider else config.model
    )

    print(_BANNER.format(
        root=root,
        endpoint=endpoint,
        model=model_label,
    ))

    while True:
        try:
            line = input("You> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            print("Use :quit to stop MiniAgent cleanly.")
            continue
        if not line:
            continue

        if line in (":quit", ":q", ":exit"):
            print("MiniAgent stopped.")
            break
        if line == ":help":
            _print_help()
            continue
        if line == ":reset":
            agent.reset()
            print("Conversation reset.")
            continue
        if line == ":resume":
            _resume_command(agent, session_logger)
            continue
        if line == ":config":
            print("Configuration:")
            print(config.describe())
            continue
        if line.startswith(":config set "):
            provider = _config_set(config, agent, provider, line[len(":config set "):])
            continue
        if line.startswith(":key set "):
            _key_set(config, line[len(":key set "):])
            continue
        if line == ":key":
            stored = bool(_load_api_key(config))
            print(f"API key stored for provider '{config.provider}': {stored}")
            continue
        if line == ":perms":
            print(permissions.summary_json())
            continue
        if line == ":clear-perms":
            _clear_perms(permissions)
            continue
        if line == ":files":
            try:
                entries = workspace.list_files(".")
                for e in entries:
                    tag = "/" if e["is_dir"] else ""
                    print(f"  {e['path']}{tag}")
            except WorkspaceError as exc:
                print(exc)
            continue
        if line == ":effort" or line.startswith(":effort "):
            _effort(provider, line[len(":effort"):].strip())
            continue
        if line == ":workspace":
            print(root)
            continue
        if line == ":context":
            _print_jeb_context(root)
            continue
        if (line in (":model", ":models")
                or line.startswith(":model ") or line.startswith(":models ")):
            if line.startswith(":models"):
                arg = line[len(":models"):].strip()
            else:
                arg = line[len(":model"):].strip()
            provider = _model_command(agent, config, provider, arg)
            continue

        # Anything else is a prompt for the agent.
        try:
            agent.turn(line)
        except KeyboardInterrupt:
            print("\n[interrupted]")
            continue
        print()


def _print_help():
    print(
        """
Commands

:help
    Show commands.

:reset
    Reset conversation context.

:resume
    List recorded sessions for this workspace, 4 per page, and reinstate
    the chosen session's message history so the conversation picks up
    where it left off. Pick with 1-4, turn pages with 0 (previous) and
    5 (next), or press Enter to cancel. New messages are appended to the
    resumed session's log.

:perms
    Show permission state.

:clear-perms
    Clear session and persistent permissions.

:config
    Print provider configuration. The API key is not stored here.

:config set KEY VALUE
    Set a configuration value and save it.

:key
    Show whether an API key is stored.

:key set VALUE
    Store the API key in the keychain for the current provider.

:model [NUMBER]
    List every configured model as <provider>/<model-name>, numbered, and
    select one to use for the current session (selection is not persisted).
    With no argument you are prompted for the number; with a number the
    selection is made directly. :models is an alias.

:effort [low|medium|high|xhigh|max]
    Set reasoning effort level sent as reasoning_effort.
    No argument prints the current level.

:files
    List top-level project files.

:workspace
    Print the project workspace path.

:context
    Show JEB.md files discovered (global + local) and the combined
    context that was added to the system prompt.

:quit
    Stop MiniAgent normally. This does not invoke exit().
""".strip()
    )


def _config_set(config, agent, provider, rest):
    parts = rest.strip().split(None, 1)
    if len(parts) != 2:
        print("Usage: :config set <key> <value>")
        return provider
    key, value = parts
    previous = config.provider
    try:
        config.set(key, value)
    except (KeyError, ValueError) as exc:
        print(f"  error: {exc}")
        return provider
    if key in ("provider", "model"):
        provider = _apply_selection(
            agent, config, provider, previous, config.provider, config.model
        )
    print(f"  {key} updated and saved.")
    return provider


def _apply_selection(agent, config, live_provider, previous_name,
                     provider_name, model_name):
    """Point the live session at *provider_name* / *model_name*.

    Switching to a different provider builds a fresh Provider (settings are
    snapshotted at construction), obtaining the new provider's API key and
    carrying the current reasoning-effort level across.  The config change
    is in memory only; callers that want it persisted use ``config.set``
    (which saves) instead of ``config.select``.
    """
    config.select(provider_name, model_name)
    if provider_name != previous_name:
        api_key = _ensure_api_key(config)
        new_provider = Provider(config, api_key)
        new_provider.effort = live_provider.effort
        agent.provider = new_provider
        return new_provider
    live_provider.model = model_name
    return live_provider


def _model_command(agent, config, provider, arg):
    """Handle ``:model`` — list models as provider/model and select one.

    The selection applies to the current session only; it is not written to
    config.json.
    """
    entries = config.model_entries()
    if not entries:
        print("No models configured.")
        print("Add providers and their models to the 'providers' section")
        print(f"of {config.path}, then restart MiniAgent.")
        return provider

    current = (config.provider, config.model)
    print("Available models:")
    for i, (p, m) in enumerate(entries, start=1):
        marker = "   <- current" if (p, m) == current else ""
        print(f"  {i}. {p}/{m}{marker}")

    choice = arg
    if not choice:
        try:
            choice = input("Select model number (blank to cancel): ").strip()
        except EOFError:
            choice = ""
    if not choice:
        return provider
    if not choice.isdigit() or not (1 <= int(choice) <= len(entries)):
        print(f"Enter a number between 1 and {len(entries)}.")
        return provider

    provider_name, model_name = entries[int(choice) - 1]
    if (provider_name, model_name) == current:
        print(f"{provider_name}/{model_name} is already active.")
        return provider

    previous = config.provider
    provider = _apply_selection(
        agent, config, provider, previous, provider_name, model_name
    )
    print(f"Model set to {provider_name}/{model_name} for this session.")
    return provider


# ---------------------------------------------------
# Session resume (:resume)
# ---------------------------------------------------

_RESUME_PAGE_SIZE = 4


def _resume_command(agent, session_logger):
    """Handle ``:resume`` — pick a recorded session and reinstate its history.

    Sessions are listed four per page.  Choosing 1-4 resumes the session at
    that position on the current page; 0 and 5 turn to the previous and next
    page; a blank answer cancels.
    """
    sessions = session_logger.list_sessions()
    if not sessions:
        print("No recorded sessions for this workspace yet.")
        return

    page = 0
    total_pages = (len(sessions) + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE
    while True:
        start = page * _RESUME_PAGE_SIZE
        entries = sessions[start:start + _RESUME_PAGE_SIZE]
        print()
        print(f"Recorded sessions (page {page + 1} of {total_pages}):")
        for i, info in enumerate(entries, start=1):
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(info.last_activity))
            print(f"  {i}. {info.id}  ({info.message_count} messages, "
                  f"last {when})")
            if info.preview:
                print(f"      {info.preview}")
        nav_left = "0. previous page" if page > 0 else "0. (first page)"
        nav_right = "5. next page" if page + 1 < total_pages else "5. (last page)"
        print(f"  {nav_left}    {nav_right}    Enter to cancel")

        try:
            choice = input("Resume> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit():
            print(f"Enter 0-{_RESUME_PAGE_SIZE}: 1-4 picks a session, "
                  "0/5 turn pages.")
            continue

        picked = int(choice)
        if picked == 0:
            page = max(0, page - 1)
            continue
        if picked == _RESUME_PAGE_SIZE + 1:
            if page + 1 < total_pages:
                page += 1
            else:
                print("Already on the last page.")
            continue
        if 1 <= picked <= len(entries):
            _resume_session(agent, session_logger, entries[picked - 1])
            return
        print(f"This page lists {len(entries)} session(s); "
              f"enter 1-{len(entries)}.")


def _resume_session(agent, session_logger, info):
    """Load session *info*, attach the logger to it, and restore the history."""
    try:
        history = session_logger.load(info.id)
        session_logger.attach(info.id)
    except SessionError as exc:
        print(f"Cannot resume session {info.id}: {exc}")
        return
    agent.restore(history)
    print(f"Resumed session {info.id}: {len(history)} messages restored.")
    print("The conversation continues from where it left off; new messages")
    print("are appended to this session's log.")


def _key_set(config, rest):
    value = rest.strip()
    if not value:
        print("Usage: :key set <value>")
        return
    stored = _store_api_key(config, value)
    if stored:
        print("API key stored in keychain.")
    else:
        print("API key stored in environment (keychain unavailable).")


def _clear_perms(permissions):
    try:
        confirm = input("Clear permissions? [y/N]: ").strip().lower()
    except EOFError:
        confirm = ""
    if confirm == "y":
        permissions.clear()
        print("Permissions cleared.")


def _effort(provider, arg):
    if not arg:
        print(f"Effort: {provider.effort or 'not set'}")
        return
    if provider.set_effort(arg):
        print(f"Effort: {arg}")
    else:
        print(f"Valid levels: {' '.join(_VALID_EFFORTS)}")


def _excerpt(text: str, width: int = 72) -> str:
    """Return the first line of *text*, truncated to *width* characters."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    if len(line) > width:
        line = line[: width - 3] + "..."
    return line


def _print_jeb_context(root: Path):
    """Show which JEB.md files were discovered and a content preview."""
    found = False
    for path in _global_jeb_md_paths():
        status = "found" if path.exists() else "missing"
        print(f"Global  : {path} ({status})")
        found = found or path.exists()

    local_path = Path(root).resolve() / JEB_MD_NAME
    status = "found" if local_path.exists() else "missing"
    print(f"Local   : {local_path} ({status})")
    found = found or local_path.exists()

    if not found:
        print("No JEB.md files found (global or local).")
        return

    _text, _header, conflicts = _load_global_jeb_md()
    if conflicts:
        docs_path, legacy_path = _global_jeb_md_paths()
        print()
        print(f"Conflicts between the global files were resolved in favour of {docs_path}:")
        for legacy_unit, kept_unit in conflicts:
            print(f"  dropped from {legacy_path}: {_excerpt(legacy_unit)}")
            print(f"  kept instead: {_excerpt(kept_unit)}")

    print()
    print("Combined context sent to the system prompt:")
    print("-" * 68)
    context = load_jeb_md_context(root)
    if context:
        print(context)
    else:
        print("(empty)")
    print("-" * 68)
