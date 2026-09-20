"""Provider-neutral, OpenAI-compatible Chat Completions client.

Uses raw ``requests`` only.  No OpenAI SDK.  Designed to talk to any server
that implements the common OpenAI-compatible tool-calling wire format.
"""

from __future__ import annotations

import requests


class ProviderError(Exception):
    """Raised when a provider request fails."""


# Valid reasoning-effort levels understood by some OpenAI-compatible servers.
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _join_nonempty(parts) -> str:
    """Join text parts with newlines, dropping empty pieces."""
    return "\n".join(part for part in parts if part)


def _text_from_parts(value) -> str:
    """Extract text from a value that is a string or a list of text parts."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return _join_nonempty(parts)
    return ""


class Provider:
    """Thin HTTP client for an OpenAI-compatible chat completions endpoint."""

    def __init__(self, config, api_key: str = ""):
        self.base_url = getattr(config, "base_url", "")
        self.chat_path = getattr(config, "chat_path", "chat/completions")
        self.model = getattr(config, "model", "")
        self.auth_enabled = getattr(config, "auth_enabled", True)
        self.auth_header = getattr(config, "auth_header", "Authorization")
        self.auth_prefix = getattr(config, "auth_prefix", "Bearer ")
        self.extra_headers = getattr(config, "extra_headers", {}) or {}
        self.extra_body = getattr(config, "extra_body", {}) or {}
        self.timeout = getattr(config, "timeout", 60)
        self.api_key = api_key or ""
        # reasoning_effort is not persisted; callers set it at runtime.
        self.effort = None

    # -- request building ---------------------------------------------------

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        path = self.chat_path.lstrip("/")
        return f"{base}/{path}"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_enabled and self.api_key:
            prefix = self.auth_prefix or ""
            if prefix:
                headers[self.auth_header] = f"{prefix}{self.api_key}"
            else:
                headers[self.auth_header] = self.api_key
        for k, v in self.extra_headers.items():
            headers[k] = v
        return headers

    def _body(self, messages: list, tools=None, tool_choice=None) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if self.effort is not None:
            body["reasoning_effort"] = self.effort
        # Lets us add provider-specific flags without modifying the client.
        body.update(self.extra_body)
        return body

    # -- public -------------------------------------------------------------

    def chat(self, messages: list, tools=None, tool_choice=None) -> dict:
        url = self._url()
        headers = self._headers()
        body = self._body(messages, tools, tool_choice)
        try:
            resp = requests.post(
                url, json=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Request failed: {exc}") from exc

        if resp.status_code >= 400:
            snippet = resp.text[:4000]
            raise ProviderError(
                f"HTTP {resp.status_code} from {url}:\n{snippet}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"Invalid JSON response: {exc}") from exc

    @staticmethod
    def extract_message(response: dict) -> dict:
        """Pull the assistant message dict out of a chat response."""
        try:
            return response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected response shape: {response}") from exc

    @staticmethod
    def split_content(content) -> tuple[str, str]:
        """Return ``(text, thinking)`` extracted from message content.

        Most OpenAI-compatible servers return ``content`` as a plain string,
        but some return a list of typed blocks instead, e.g.::

            [{"type": "thinking",
              "thinking": [{"type": "text", "text": "Hmm..."}],
              "closed": true},
             {"type": "text", "text": "Hello!"}]

        Text blocks are joined into ``text``; thinking/reasoning blocks are
        extracted into ``thinking`` so a caller can display them as
        reasoning instead of dumping raw JSON.  A thinking block's payload
        may itself be a plain string or a list of text parts.  Unknown
        block types (images, tool use, ...) are ignored.
        """
        if content is None:
            return "", ""
        if isinstance(content, str):
            return content, ""
        if not isinstance(content, list):
            return "", ""

        texts = []
        thoughts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in ("thinking", "reasoning"):
                payload = block.get("thinking", block.get("reasoning"))
                thoughts.append(_text_from_parts(payload))
            elif "text" in block:
                texts.append(_text_from_parts(block["text"]))
        return _join_nonempty(texts), _join_nonempty(thoughts)

    # -- reasoning effort ---------------------------------------------------

    def set_effort(self, level) -> bool:
        """Set reasoning effort.  None clears it.  Returns True if valid."""
        if level is None:
            self.effort = None
            return True
        if level in _VALID_EFFORTS:
            self.effort = level
            return True
        return False
