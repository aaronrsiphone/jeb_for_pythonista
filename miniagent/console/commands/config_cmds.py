"""Provider/model/key configuration and the front-end renderer switch.

``:config``, ``:key``, ``:model``/``:models`` and ``:effort`` all touch the
same live wiring — ``ctx.provider`` and ``ctx.config`` — so
``_apply_selection`` (the one place a provider switch actually happens)
lives here too, alongside ``:verbose`` which swaps the renderer the same way.
"""

from __future__ import annotations

from ...keys import _ensure_api_key, _load_api_key, _store_api_key
from ...provider import Provider
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
    print("Configuration:")
    print(ctx.config.describe())


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
    """Show whether an API key is stored.

    :key set VALUE
        Store the API key in the keychain for the current provider.
    """
    rest = arg.strip()
    if rest.startswith("set ") or rest == "set":
        _key_set(ctx, rest[len("set"):].strip())
        return
    if rest:
        print(f"Unknown :key usage: {arg!r}. Use ':key' or ':key set VALUE'.")
        return
    stored = bool(_load_api_key(ctx.config))
    print(f"API key stored for provider '{ctx.config.provider}': {stored}")


def _key_set(ctx, value):
    if not value:
        print("Usage: :key set <value>")
        return
    stored = _store_api_key(ctx.config, value)
    if stored:
        print("API key stored in keychain.")
    else:
        print("API key stored in environment (keychain unavailable).")


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
        print("Add providers and their models to the 'providers' section")
        print(f"of {config.path}, then restart MiniAgent.")
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
