"""MiniAgent application configuration.

Configuration lives in a single JSON file inside the application-state
directory (outside any user project).  No credentials are stored here; API
keys live in the Pythonista keychain, one entry per provider.

The file has this shape::

    {
      "provider": "mistral",          # currently selected provider
      "model": "zai-glm-5-3",         # currently selected model
      "vision_model": "mistral/pixtral-12b-2409",  # used by ask_image
      "providers": {
        "mistral": {
          "base_url": "https://api.mistral.ai/v1",
          "chat_path": "chat/completions",
          "models": ["zai-glm-5-3", "mistral-medium-latest"],
          "auth_enabled": true,
          "auth_header": "Authorization",
          "auth_prefix": "Bearer ",
          "extra_headers": {},
          "extra_body": {},
          "timeout": 120
        }
      }
    }

``vision_model`` selects the provider/model pair the ``ask_image`` tool
sends images to; the format is ``<provider>/<model-name>``, or a bare model
name to use the currently selected provider.  It is optional: when absent,
``ask_image`` reports that no vision model is configured.  Unknown top-level
keys are preserved on load/save.

Older flat configs (``base_url`` / ``model`` at the top level) are migrated
automatically the first time they are loaded: the single provider is named
after its API hostname and the old model becomes its one ``models`` entry.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

# Defaults for each provider entry.  Unspecified keys in a provider's
# settings fall back to these values.
PROVIDER_DEFAULTS = {
    "base_url": "",
    "chat_path": "chat/completions",
    "models": [],          # model ids available from this provider
    "model": "",           # last model used with this provider
    "auth_enabled": True,
    "auth_header": "Authorization",
    "auth_prefix": "Bearer ",
    "extra_headers": {},
    "extra_body": {},
    "timeout": 120,
}

# Keys whose values should be JSON-typed (not plain strings).
_JSON_KEYS = {"extra_headers", "extra_body", "models"}

# Provider-scoped keys settable through ``:config set``.  The per-provider
# "model" memory is managed internally via ``select()`` / ``set("model")``.
_SETTABLE_PROVIDER_KEYS = {
    "base_url", "chat_path", "models", "auth_enabled", "auth_header",
    "auth_prefix", "extra_headers", "extra_body", "timeout",
}


def _safe_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def default_state_dir() -> str:
    """Return a writable directory for MiniAgent application state.

    Prefers Pythonista's user-visible Documents folder, then a dot-directory
    in the home folder, then a temp directory.  No iOS container UUID or
    device-specific path is hard-coded.
    """
    candidates = [
        os.path.expanduser("~/Documents/miniagent"),
        os.path.expanduser("~/.miniagent"),
    ]
    for c in candidates:
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".writeprobe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return c
        except OSError:
            continue
    import tempfile

    fallback = os.path.join(tempfile.gettempdir(), "miniagent")
    os.makedirs(fallback, exist_ok=True)
    return fallback


# Legacy config file names written by the original jeb.py.  The ``.py``
# variant is accepted because some setups saved the JSON payload under that
# name; both are plain JSON on disk.
_LEGACY_CONFIG_NAMES = ("miniagent_config.json", "miniagent_config.py")


def load_legacy_config(search_dir: str) -> dict | None:
    """Read a legacy jeb.py config file from *search_dir*, if present.

    Returns the parsed dict, or ``None`` when no legacy file exists or it
    cannot be parsed.
    """
    for name in _LEGACY_CONFIG_NAMES:
        path = os.path.join(search_dir, name)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
    return None


def _provider_name_from_url(base_url: str) -> str:
    """Derive a short provider name from an API base URL's hostname.

    ``https://api.mistral.ai/v1`` becomes ``mistral``: a leading ``api``
    label is dropped when the host has more than two labels.  Falls back to
    ``default`` when nothing usable remains.
    """
    host = ""
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return "default"
    labels = host.split(".")
    if len(labels) > 2 and labels[0] == "api":
        labels = labels[1:]
    name = "".join(ch for ch in labels[0] if ch.isalnum() or ch in "-_")
    return name or "default"


def _migrate_legacy(data) -> dict:
    """Convert a legacy flat config dict to the multi-provider shape.

    The single provider is named after the base URL hostname and the old
    model becomes its only ``models`` entry.  Returns an empty shape when
    there is nothing to migrate.
    """
    if not isinstance(data, dict) or not data.get("base_url"):
        return {"providers": {}, "provider": "", "model": ""}
    settings = {}
    for key in PROVIDER_DEFAULTS:
        if key in ("model", "models"):
            continue
        if key in data:
            settings[key] = data[key]
    model = str(data.get("model") or "")
    settings["models"] = [model] if model else []
    settings["model"] = model
    name = _provider_name_from_url(str(data.get("base_url")))
    return {"providers": {name: settings}, "provider": name, "model": model}


def _normalize_providers(raw) -> dict:
    """Return a validated providers mapping: name -> settings."""
    providers = {}
    if not isinstance(raw, dict):
        return providers
    for name, settings in raw.items():
        if not isinstance(name, str) or not name or not isinstance(settings, dict):
            continue
        merged = dict(PROVIDER_DEFAULTS)
        merged.update(settings)  # unknown keys are preserved
        models = merged.get("models")
        if not isinstance(models, list):
            models = []
        merged["models"] = [m for m in models if isinstance(m, str) and m]
        if not isinstance(merged.get("model"), str):
            merged["model"] = ""
        if not merged["model"] and merged["models"]:
            merged["model"] = merged["models"][0]
        if not isinstance(merged.get("extra_headers"), dict):
            merged["extra_headers"] = {}
        if not isinstance(merged.get("extra_body"), dict):
            merged["extra_body"] = {}
        try:
            merged["timeout"] = int(merged.get("timeout") or 120)
        except (TypeError, ValueError):
            merged["timeout"] = 120
        providers[name] = merged
    return providers


def _coerce(key: str, value):
    """Type-coerce a value being set for *key*."""
    if key in _JSON_KEYS:
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            raise ValueError(f"{key} must be a JSON value")
        if key == "models":
            if not isinstance(value, list) or not all(
                isinstance(m, str) for m in value
            ):
                raise ValueError("models must be a JSON list of strings")
        elif not isinstance(value, dict):
            raise ValueError(f"{key} must be a JSON object")
    elif key == "timeout":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("timeout must be an integer")
    elif key == "auth_enabled":
        value = _to_bool(value)
    else:
        value = str(value)
    return value


def _split_vision_model(value: str) -> tuple[str | None, str]:
    """Split a ``vision_model`` value into ``(provider, model)``.

    ``"mistral/pixtral-12b-2409"`` -> ``("mistral", "pixtral-12b-2409")``; a
    bare model name -> ``(None, name)`` (the selected provider is used).
    """
    text = str(value or "").strip()
    if "/" in text:
        provider, _, model = text.partition("/")
        return provider.strip() or None, model.strip()
    return None, text


class Config:
    """Provider configuration loaded from / saved to a JSON file."""

    def __init__(self, state_dir: str, data: dict | None = None):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "config.json")
        if data is None:
            data, migrated = self._load()
        else:
            migrated = False
        self._data, changed = self._normalize(data)
        if migrated or changed:
            self.save()

    @classmethod
    def load(cls, state_dir: str) -> "Config":
        return cls(state_dir)

    def _load(self):
        """Read config.json, migrating a legacy flat config on the fly."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return {}, False
            if isinstance(data, dict) and "providers" not in data:
                return _migrate_legacy(data), True
            return data, False
        return {}, False

    def _normalize(self, data) -> tuple[dict, bool]:
        """Validate *data*, returning ``(normalized dict, changed flag)``."""
        if not isinstance(data, dict):
            data = {}
        changed = False
        providers = _normalize_providers(data.get("providers"))

        provider = data.get("provider", "")
        if provider not in providers:
            provider = next(iter(providers), "")
            if provider != data.get("provider", ""):
                changed = True

        model = data.get("model", "")
        if provider:
            settings = providers[provider]
            models = settings["models"]
            if model and model not in models:
                # The selection wins: keep the model listed and usable.
                models.append(model)
                changed = True
            if not model:
                model = settings["model"]

        out = {
            k: v for k, v in data.items()
            if k not in ("providers", "provider", "model")
        }
        out["providers"] = providers
        out["provider"] = provider
        out["model"] = model
        return out, changed

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    # -- first-run interactive setup ----------------------------------------

    def first_run_setup(self, legacy_dir: str | None = None) -> bool:
        """Interactive first-run configuration.

        If a legacy config exists in *legacy_dir*, its values are imported
        silently.  Otherwise the user is prompted for a provider name, base
        URL, the models available, chat path and auth settings.  Returns
        True if a config was created or imported.
        """
        # Try legacy migration first.
        if legacy_dir:
            legacy = load_legacy_config(legacy_dir)
            if legacy:
                raw = legacy if "providers" in legacy else _migrate_legacy(legacy)
                self._data, _ = self._normalize(raw)
                self.save()
                print(f"Migrated legacy config from {legacy_dir} to {self.path}")
                return True

        print("First-run MiniAgent configuration")
        print()

        base_url = ""
        while not base_url:
            base_url = _safe_input(
                "API base URL, e.g. https://host.example/v1: "
            ).strip()

        default_name = _provider_name_from_url(base_url)
        name = _safe_input(f"Provider name [{default_name}]: ").strip()
        if not name:
            name = default_name

        models_raw = ""
        while not models_raw:
            models_raw = _safe_input(
                "Models available (comma-separated): "
            ).strip()
        models = [m.strip() for m in models_raw.split(",") if m.strip()]

        chat_path = _safe_input(
            "Chat completions path [/chat/completions]: "
        ).strip()
        if not chat_path:
            chat_path = "/chat/completions"

        use_auth = _safe_input(
            "Use API-key authentication? [Y/n]: "
        ).strip().lower()

        settings = dict(PROVIDER_DEFAULTS)
        settings.update({
            "base_url": base_url.rstrip("/"),
            "chat_path": chat_path,
            "models": models,
            "model": models[0],
            "auth_enabled": use_auth not in ("n", "no"),
        })
        self._data = {
            "providers": {name: settings},
            "provider": name,
            "model": models[0],
        }
        self.save()
        return True

    # -- accessors ----------------------------------------------------------

    def __getattr__(self, name):
        # Only called when normal attribute lookup fails.  The selected
        # provider's settings are exposed as attributes so Provider can be
        # built from a Config without knowing about the multi-provider shape.
        if name.startswith("_"):
            raise AttributeError(name)
        data = self._data
        if name in ("providers", "provider", "model"):
            return data[name]
        selected = data["providers"].get(data["provider"])
        if selected is not None and name in selected:
            return selected[name]
        if name in PROVIDER_DEFAULTS:
            return PROVIDER_DEFAULTS[name]
        try:
            return data[name]
        except KeyError:
            raise AttributeError(name)

    def as_dict(self) -> dict:
        return dict(self._data)

    # -- selection ----------------------------------------------------------

    def model_entries(self) -> list[tuple[str, str]]:
        """All ``(provider, model)`` pairs, in configuration order."""
        entries = []
        for name, settings in self._data["providers"].items():
            for model in settings.get("models", []):
                entries.append((name, model))
        return entries

    def select(self, provider: str, model: str):
        """Select *provider* / *model* in memory (not persisted)."""
        providers = self._data["providers"]
        if provider not in providers:
            raise KeyError(f"Unknown provider: {provider}")
        if model and model not in providers[provider]["models"]:
            providers[provider]["models"].append(model)
        providers[provider]["model"] = model
        self._data["provider"] = provider
        self._data["model"] = model

    def set(self, key: str, value):
        if key == "provider":
            if value not in self._data["providers"]:
                known = ", ".join(sorted(self._data["providers"])) or "(none)"
                raise KeyError(f"Unknown provider: {value}. Available: {known}")
            old = self._data["provider"]
            self._data["provider"] = value
            if value != old:
                # The model follows the new provider's remembered selection.
                self._data["model"] = self._data["providers"][value]["model"]
        elif key == "model":
            self._data["model"] = str(value)
            name = self._data["provider"]
            if name:
                settings = self._data["providers"][name]
                settings["model"] = self._data["model"]
                if self._data["model"] and self._data["model"] not in settings["models"]:
                    settings["models"].append(self._data["model"])
        elif key == "vision_model":
            value = str(value).strip()
            provider, model = _split_vision_model(value)
            if provider is not None and provider not in self._data["providers"]:
                known = ", ".join(sorted(self._data["providers"])) or "(none)"
                raise KeyError(
                    f"Unknown vision provider: {provider}. Available: {known}"
                )
            if not model:
                raise KeyError("vision_model needs a model name after the provider")
            self._data["vision_model"] = value
        elif key in _SETTABLE_PROVIDER_KEYS:
            name = self._data["provider"]
            if not name:
                raise KeyError("No provider configured")
            settings = self._data["providers"][name]
            settings[key] = _coerce(key, value)
            if key == "models" and self._data["model"]:
                # Keep the invariant "the selected model is always listed",
                # matching what _normalize does on load.
                if self._data["model"] not in settings["models"]:
                    settings["models"].append(self._data["model"])
        else:
            raise KeyError(f"Unknown config key: {key}")
        self.save()

    def describe(self) -> str:
        data = self._data
        lines = [
            f"  provider = {data['provider']!r}",
            f"  model = {data['model']!r}",
        ]
        vision_model = str(data.get("vision_model", "") or "")
        if vision_model:
            lines.append(f"  vision_model = {vision_model!r}")
        lines.append("  providers:")
        for name, settings in data["providers"].items():
            tag = "  (selected)" if name == data["provider"] else ""
            lines.append(f"    {name}{tag}:")
            for k in sorted(settings):
                if k == "model" and not settings.get(k):
                    continue
                lines.append(f"      {k} = {settings[k]!r}")
        if not data["providers"]:
            lines.append("    (none configured)")
        return "\n".join(lines)


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")
