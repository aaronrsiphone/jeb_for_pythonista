"""High-level construction and the interactive console loop.

``run(project_root)`` is the single public entry point.  Importing this module
does not start a session; all construction happens inside ``run()``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .agent import Agent, build_system_prompt
from .config import Config, default_state_dir
from .permissions import Permissions
from .provider import Provider
from .runner import Runner
from .tools import Tools
from .workspace import Workspace, WorkspaceError

KEYCHAIN_ACCOUNT = "api_key"

_BANNER = """
============================================================
 MiniAgent — coding agent for: {root}
 Endpoint: {endpoint}
 Model: {model}
 Commands: :help  :config  :key  :model  :perms  :reset  :clear-perms
           :effort  :files  :context  :quit
============================================================
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


def _load_api_key(config: Config) -> str:
    """Return the API key for the currently selected provider.

    Keys are stored per provider.  When a provider has no key yet but a
    legacy entry (keyed by base-URL hash) exists for its endpoint, that
    entry is copied over to the per-provider service.
    """
    kc = _get_keychain()
    if kc is None:
        return os.environ.get("MINIAGENT_API_KEY", "")
    service = _provider_service(config.provider)
    try:
        key = kc.get_password(service, KEYCHAIN_ACCOUNT) or ""
    except Exception:
        key = ""
    if key:
        return key
    legacy_service = _keychain_service(getattr(config, "base_url", "") or "")
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


# ---------------------------------------------------------------------------
# JEB.md discovery
# ---------------------------------------------------------------------------

JEB_MD_NAME = "JEB.md"
_MAX_JEB_MD_CHARS = 32_000


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


def _global_jeb_md_path() -> Path:
    """Return the path to the global ``JEB.md`` in ``~/miniagent/``."""
    return Path.home() / "miniagent" / JEB_MD_NAME


def load_jeb_md_context(project_root: Path) -> str:
    """Discover and concatenate ``JEB.md`` context for *project_root*.

    Two locations are checked, in order:

    1. **Global** — a ``JEB.md`` in ``~/miniagent/``.  Its content is
       included first.
    2. **Local** — a ``JEB.md`` in the project workspace root.  Its content
       is included second.

    Either or both may be absent.  The two sections are separated by a
    labelled divider so the model can tell them apart.  An empty string is
    returned when neither file exists.
    """
    parts: list[str] = []

    global_path = _global_jeb_md_path()
    global_text = _read_jeb_md(global_path)
    if global_text:
        parts.append("# Global JEB.md (~/miniagent)\n\n" + global_text)

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
    tools = Tools(workspace, permissions, runner)
    provider = Provider(config, api_key)
    jeb_context = load_jeb_md_context(root)
    agent = Agent(provider, tools, build_system_prompt(str(root), jeb_context))

    _console_loop(agent, config, permissions, workspace, root, provider)


# ---------------------------------------------------------------------------
# Console loop
# ---------------------------------------------------------------------------


def _console_loop(agent, config, permissions, workspace, root, provider):
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


def _print_jeb_context(root: Path):
    """Show which JEB.md files were discovered and a content preview."""
    global_path = _global_jeb_md_path()
    local_path = Path(root).resolve() / JEB_MD_NAME

    found = False

    if global_path.exists():
        found = True
        print(f"Global  : {global_path}")
    if local_path.exists():
        found = True
        print(f"Local   : {local_path}")

    if not found:
        print("No JEB.md files found (global or local).")
        return

    print()
    print("Combined context sent to the system prompt:")
    print("-" * 68)
    context = load_jeb_md_context(root)
    if context:
        print(context)
    else:
        print("(empty)")
    print("-" * 68)
