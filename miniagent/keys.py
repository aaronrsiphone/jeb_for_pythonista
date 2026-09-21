"""Keychain / API-key loading, storage and legacy migration.

Split out of ``app.py`` (§5.6): this is pure key-management, with no
dependency on the console loop or the agent.  ``Vision`` uses
``_load_api_key`` directly (via ``run()``'s ``load_key`` callback) to load
whichever provider it is configured for, independent of the main chat
provider.
"""

from __future__ import annotations

import hashlib
import os

from .config import Config

KEYCHAIN_ACCOUNT = "api_key"


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
