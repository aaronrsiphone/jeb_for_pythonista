"""Collaborator behind the web_search tool: the search agent's model and Brave client.

Plays the role for ``web_search`` that ``vision.py`` plays for
``ask_image``: ``run()`` constructs it with the config, a per-provider key
loader and a ``BraveSearch`` client, and passes it to ``Tools``.  The tool
asks it for a ready-to-use ``Provider`` each time it runs.

The search agent's model comes from config.json's ``search_model`` key —
``<provider>/<model-name>``, or a bare model name for the selected
provider — so the inner agent can run on a cheaper or faster model than
the main chat.  When ``search_model`` is unset it falls back to the
currently selected chat model, so web search works without extra setup.
Resolution happens on every call rather than at construction, so
``:model`` and ``:config set search_model`` take effect immediately.
"""

from __future__ import annotations

from types import SimpleNamespace

from .brave import SearchError
from .provider import Provider
from .vision import split_target

# The search agent makes several quick calls; a provider whose configured
# timeout is the 120 s chat default should fail faster here, so one slow
# request cannot hold the whole tool call for minutes.
INNER_TIMEOUT = 60


class WebSearch:
    """Resolves the search model and holds the Brave client for web_search."""

    def __init__(self, config, load_key, brave):
        self._config = config
        self._load_key = load_key
        self.brave = brave

    def target(self) -> tuple:
        """Return ``(provider name, model, provider settings)`` for the search agent."""
        config = self._config
        providers = getattr(config, "providers", {}) or {}
        spec = str(getattr(config, "search_model", "") or "").strip()
        if spec:
            name, model = split_target(spec)
            name = name or str(getattr(config, "provider", "") or "")
        else:
            name = str(getattr(config, "provider", "") or "")
            model = str(getattr(config, "model", "") or "")
        settings = providers.get(name) if name else None
        if not isinstance(settings, dict):
            raise SearchError(
                f"search model provider {name!r} is not configured; set "
                '"search_model": "<provider>/<model-name>" in config.json or '
                "run ':config set search_model <provider>/<model-name>'"
            )
        model = model or str(settings.get("model") or "")
        if not model:
            raise SearchError(f"no model is configured for provider {name!r}")
        return name, model, settings

    def target_label(self) -> str:
        """Short description of the search model, for the permission preview."""
        try:
            name, model, _settings = self.target()
        except SearchError as exc:
            return f"(unavailable: {exc})"
        return f"{name}/{model}"

    def make_provider(self) -> Provider:
        """Build a ``Provider`` for the search model, with its own API key."""
        name, model, settings = self.target()
        ns = SimpleNamespace(**settings)
        ns.model = model
        try:
            configured = int(settings.get("timeout") or INNER_TIMEOUT)
        except (TypeError, ValueError):
            configured = INNER_TIMEOUT
        ns.timeout = min(configured, INNER_TIMEOUT)
        key = ""
        if settings.get("auth_enabled", True):
            key = self._load_key(name) or ""
            if not key:
                raise SearchError(
                    f"no API key is stored for provider {name!r}, which "
                    "search_model uses"
                )
        return Provider(ns, key)
