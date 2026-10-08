"""Provider/model/key configuration and the front-end renderer switch.

``:config``, ``:key``, ``:rotate-key``/``:rekey``, ``:provider``,
``:model``/``:models`` and ``:effort`` all touch the same live wiring — ``ctx.provider`` and
``ctx.config`` — so ``_apply_selection`` (the one place a provider switch
actually happens) lives here too, alongside ``:verbose`` which swaps the
renderer the same way.
"""

from __future__ import annotations

from ...keys import (
    _delete_api_key,
    _ensure_api_key,
    _load_api_key,
    _secure_input,
    _store_api_key,
    load_brave_key,
    store_brave_key,
)
from ...provider import Provider, ProviderError, list_models
from ...ui import get_renderer
from ..registry import command

_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@command("config")
def config_command(ctx, arg):
    """Print provider configuration. The API key is not stored here.

    :config set KEY VALUE
        Set a configuration value and save it.
    """
    rest = arg.strip()
    if rest.startswith("set ") or rest == "set":
        _config_set(ctx, rest[len("set"):].strip())
        return
    if rest:
        print(f"Unknown :config usage: {arg!r}. Use ':config' or ':config set KEY VALUE'.")
        return
    print(f"Configuration ({ctx.config.path}):")
    print(ctx.config.describe())
    problem = ctx.provider.endpoint_problem()
    print(f"  live endpoint = {ctx.provider._url()!r}")
    if problem:
        print(f"  PROBLEM: {problem}")


def _config_set(ctx, rest):
    parts = rest.strip().split(None, 1)
    if len(parts) != 2:
        print("Usage: :config set <key> <value>")
        return
    key, value = parts
    previous = ctx.config.provider
    try:
        ctx.config.set(key, value)
    except (KeyError, ValueError) as exc:
        print(f"  error: {exc}")
        return
    if key in ("provider", "model"):
        _apply_selection(ctx, previous, ctx.config.provider, ctx.config.model)
    elif key not in ("vision_model", "search_model"):
        # Endpoint/auth settings are snapshotted by Provider at
        # construction: rebuild it so the change applies without a restart.
        fresh = Provider(ctx.config, ctx.provider.api_key)
        fresh.effort = ctx.provider.effort
        ctx.provider = fresh
        ctx.agent.provider = fresh
    print(f"  {key} updated and saved.")


def _apply_selection(ctx, previous_name, provider_name, model_name):
    """Point the live session at *provider_name* / *model_name*.

    Switching to a different provider builds a fresh Provider (settings are
    snapshotted at construction), obtaining the new provider's API key and
    carrying the current reasoning-effort level across.  The config change
    is in memory only; callers that want it persisted use ``config.set``
    (which saves) instead of ``config.select``. Mutates ``ctx.provider`` (and
    ``ctx.agent.provider``) in place rather than returning a replacement —
    this is the wart §5.6 calls out: a ``Provider`` snapshots config at
    construction, so switching used to mean threading a new instance back
    through a local variable at every call site.
    """
    ctx.config.select(provider_name, model_name)
    if provider_name != previous_name:
        api_key = _ensure_api_key(ctx.config)
        new_provider = Provider(ctx.config, api_key)
        new_provider.effort = ctx.provider.effort
        ctx.provider = new_provider
        ctx.agent.provider = new_provider
    else:
        ctx.provider.model = model_name


@command("key")
def key_command(ctx, arg):
    """Show which providers have an API key stored, and the Brave Search key.

    :key set VALUE
        Store VALUE as the current provider's key and start using it
        immediately (same as ':rotate-key VALUE').
    :key set brave VALUE
        Store the Brave Search API key used by the web_search tool.
    :key clear
        Forget the current provider's stored key.
    """
    rest = arg.strip()
    if rest.startswith("set ") or rest == "set":
        value = rest[len("set"):].strip()
        if value == "brave" or value.startswith("brave "):
            _brave_key_set(value[len("brave"):].strip())
            return
        if not value:
            print("Usage: :key set <value>")
            return
        _install_key(ctx, ctx.config.provider, value)
        return
    if rest == "clear":
        name = ctx.config.provider
        removed = _delete_api_key(ctx.config, name)
        ctx.provider.api_key = ""
        print(f"API key for provider '{name}' "
              f"{'removed' if removed else 'was not stored'}; this session "
              "is now unauthenticated. Run :rotate-key to enter a new one.")
        return
    if rest:
        print(f"Unknown :key usage: {arg!r}. Use ':key', ':key set VALUE', "
              "':key set brave VALUE' or ':key clear'.")
        return
    current = ctx.config.provider
    stored = bool(_load_api_key(ctx.config))
    print(f"API key stored for provider '{current}': {stored}")
    live = bool(getattr(ctx.provider, "api_key", ""))
    if ctx.config.auth_enabled and not live:
        print("  This session has no key loaded; run :rotate-key to enter one.")
    elif live and not stored:
        print("  A key is active in this session but is not saved; "
              "run :rotate-key to store it.")
    for name, settings in ctx.config.providers.items():
        if name == current:
            continue
        if not settings.get("auth_enabled", True):
            state = "auth disabled"
        else:
            state = "stored" if _load_api_key(ctx.config, name) else "MISSING"
        print(f"  {name}: {state}")
    print(f"Brave Search API key stored: {bool(load_brave_key())}")


def _install_key(ctx, name, value):
    """Store *value* for provider *name*; hot-swap it in if *name* is live.

    Provider snapshots api_key at construction, so storing alone would
    leave the running session on the old (or missing) key until restart.
    """
    stored = _store_api_key(ctx.config, value, name)
    where = ("stored in keychain" if stored
             else "kept in memory for this run only (keychain unavailable)")
    if name == ctx.config.provider:
        ctx.provider.api_key = value
        print(f"API key rotated: {where} for provider '{name}' "
              "and active in this session.")
    else:
        print(f"API key rotated: {where} for provider '{name}'.")


@command("rotate-key", aliases=("rekey",))
def rotate_key_command(ctx, arg):
    """Replace or re-enter an API key without restarting.

    Prompts for the key with hidden input, stores it in the keychain for
    the current provider and swaps the live session over to it, so the
    next request uses it. Use this when a key has expired, or when the
    keychain entry is missing and requests are failing with 401.

    :rotate-key for PROVIDER
        Prompt for another configured provider's key (e.g. the vision
        provider) instead of the current one.
    :rotate-key VALUE
        Store VALUE directly instead of prompting (the value is visible
        as you type it, so prefer the prompted form).
    """
    value = arg.strip()
    name = ctx.config.provider
    if value == "for" or value.startswith("for "):
        name = value[len("for"):].strip()
        value = ""
        if name not in ctx.config.providers:
            known = ", ".join(ctx.config.providers) or "(none)"
            print(f"Unknown provider: {name or '(none given)'}. "
                  f"Configured: {known}")
            return
    if not name:
        print("No provider is configured yet; add one with "
              "':provider add NAME BASE_URL MODEL'.")
        return
    if not value:
        value = _secure_input(
            f"New API key for provider '{name}' "
            "(input hidden, blank to cancel): "
        ).strip()
    if not value:
        print("Key rotation cancelled; nothing changed.")
        return
    _install_key(ctx, name, value)


def _provider_models(ctx, name):
    settings = ctx.config.providers.get(name)
    if settings is None:
        known = ", ".join(ctx.config.providers) or "(none)"
        print(f"Unknown provider: {name or '(none)'}. Configured: {known}")
        return
    key = ""
    if settings.get("auth_enabled", True):
        if name == ctx.config.provider:
            key = getattr(ctx.provider, "api_key", "")
        key = key or _load_api_key(ctx.config, name)
        if not key:
            print(f"No API key for '{name}'; trying without one "
                  f"(set it with ':rotate-key for {name}').")
    try:
        ids = list_models(settings, key)
    except ProviderError as exc:
        print(f"  error: {exc}")
        return
    if not ids:
        print(f"'{name}' returned no models.")
        return
    configured = set(settings.get("models", []))
    print(f"Models offered by '{name}' ({len(ids)}; * = in config.json):")
    for mid in ids:
        print(f"  {'*' if mid in configured else ' '} {mid}")
    url = settings.get("base_url", "")
    print(f"Add one with: :provider add {name} {url} MODEL")


@command("provider", aliases=("providers",))
def provider_command(ctx, arg):
    """List configured providers and their endpoints.

    :provider add NAME BASE_URL [MODEL,MODEL...]
        Add a provider (or update an existing one's URL / add models) and
        save. Pick it afterwards with :model; its key is asked for then.
    :provider remove NAME
        Remove a provider that is not the active one.
    :provider models [NAME]
        Ask the provider's API (GET <base_url>/models) which models it
        offers; NAME defaults to the active provider. Models already in
        config.json are marked with *.
    """
    config = ctx.config
    parts = arg.split()
    if not parts:
        if not config.providers:
            print("No providers configured. "
                  "Add one: :provider add NAME BASE_URL MODEL")
            return
        print(f"Providers ({config.path}):")
        for name, settings in config.providers.items():
            tag = "  <- current" if name == config.provider else ""
            models = ", ".join(settings.get("models", [])) or "(no models)"
            print(f"  {name}: {settings.get('base_url') or '(no base_url)'}{tag}")
            print(f"      models: {models}")
        return
    action = parts[0]
    if action == "models" and len(parts) <= 2:
        _provider_models(ctx, parts[1] if len(parts) == 2 else config.provider)
        return
    try:
        if action == "add" and len(parts) >= 3:
            models = [m for chunk in parts[3:] for m in chunk.split(",") if m]
            existed = parts[1] in config.providers
            config.add_provider(parts[1], parts[2], models)
            if parts[1] == config.provider:
                # Live provider edited (or first one ever): rebuild it.
                fresh = Provider(config, ctx.provider.api_key
                                 if existed else _ensure_api_key(config))
                fresh.effort = ctx.provider.effort
                ctx.provider = fresh
                ctx.agent.provider = fresh
            print(f"Provider '{parts[1]}' {'updated' if existed else 'added'} "
                  "and saved. Use :model to select one of its models.")
            if not config.providers[parts[1]]["models"]:
                print("  It has no models yet: "
                      f":provider add {parts[1]} {parts[2]} MODEL")
        elif action == "remove" and len(parts) == 2:
            config.remove_provider(parts[1])
            print(f"Provider '{parts[1]}' removed and saved. "
                  "(Its stored API key was left in the keychain.)")
        else:
            print("Usage: :provider | :provider add NAME BASE_URL "
                  "[MODEL,...] | :provider remove NAME | :provider models [NAME]")
    except (KeyError, ValueError) as exc:
        print(f"  error: {exc.args[0] if exc.args else exc}")


def _brave_key_set(value):
    if not value:
        print("Usage: :key set brave <value>")
        return
    if store_brave_key(value):
        print("Brave Search API key stored in keychain.")
    else:
        print("Brave Search API key stored in environment for this session "
              "(keychain unavailable).")


@command("model", aliases=("models",))
def model_command(ctx, arg):
    """List every configured model as <provider>/<model-name>, numbered, and
    select one to use for the current session (selection is not persisted).
    With no argument you are prompted for the number; with a number the
    selection is made directly. :models is an alias.
    """
    config = ctx.config
    entries = config.model_entries()
    if not entries:
        print("No models configured.")
        print("Add one with ':provider add NAME BASE_URL MODEL[,MODEL...]'.")
        return

    current = (config.provider, config.model)
    print("Available models:")
    for i, (p, m) in enumerate(entries, start=1):
        marker = "   <- current" if (p, m) == current else ""
        print(f"  {i}. {p}/{m}{marker}")

    choice = arg.strip()
    if not choice:
        try:
            choice = input("Select model number (blank to cancel): ").strip()
        except EOFError:
            choice = ""
    if not choice:
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(entries)):
        print(f"Enter a number between 1 and {len(entries)}.")
        return

    provider_name, model_name = entries[int(choice) - 1]
    if (provider_name, model_name) == current:
        print(f"{provider_name}/{model_name} is already active.")
        return

    previous = config.provider
    _apply_selection(ctx, previous, provider_name, model_name)
    print(f"Model set to {provider_name}/{model_name} for this session.")


@command("effort")
def effort_command(ctx, arg):
    """Set reasoning effort level sent as reasoning_effort.
    No argument prints the current level.
    """
    arg = arg.strip()
    if not arg:
        print(f"Effort: {ctx.provider.effort or 'not set'}")
        return
    if ctx.provider.set_effort(arg):
        print(f"Effort: {arg}")
    else:
        print(f"Valid levels: {' '.join(_VALID_EFFORTS)}")


@command("verbose")
def verbose_command(ctx, arg):
    """Switch the front end between the compact console renderer (the
    default: collapsed reasoning, one line per tool call, the permission
    legend shown once) and the verbose one (full reasoning, separate tool
    request/result lines, the legend on every prompt). No argument prints
    the current renderer.
    """
    arg = arg.strip()
    if not arg:
        print(f"Renderer: {ctx.renderer_name}")
        return
    wanted = {"on": "verbose", "off": "console"}.get(arg.lower())
    if wanted is None:
        print("Usage: :verbose [on|off]")
        return
    if wanted == ctx.renderer_name:
        print(f"Renderer already {ctx.renderer_name}.")
        return
    print(f"Renderer: {wanted}")
    ctx.renderer = get_renderer(wanted)
    ctx.renderer_name = wanted
