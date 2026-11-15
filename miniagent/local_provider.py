"""A provider that runs the model on this device, in this process.

A provider whose ``base_url`` starts with ``tinygpt://`` is not an HTTP
endpoint: it is a model run by `tiny_gpt <https://github.com/aaronrsiphone/
tiny_gpt>`_'s NumPy/Accelerate inference code, loaded into the same
Pythonista process as the agent.  Nothing leaves the device and no API key
is involved.

    :provider add lfm tinygpt:// LiquidAI/LFM3-1.7B
    :model lfm/LiquidAI/LFM3-1.7B

The text after ``tinygpt://`` is the folder holding tiny_gpt's ``hf_run.py``
(``tinygpt://~/Documents/tiny_gpt``).  Left empty, the usual places are
searched.  The model must already be downloaded there (``hf_run.py`` with
``MODE = 'download'``); this module never touches the network.

Settings go in the provider's ``extra_body`` (``:config set extra_body
{...}``), which for a local model is read here instead of being sent
anywhere:

``context``
    Tokens of conversation the model can hold (default 4096).  The system
    prompt and the tool descriptions take about 2,600 of them.  LFM3-1.7B
    needs roughly 150 KB of memory per token.
``max_tokens``
    Longest single reply, thinking included (default 1024).
``enable_thinking``
    ``false`` skips the model's reasoning: faster, less reliable.
``weights``
    ``auto`` / ``lowmem`` / ``float32``, as in ``hf_run.py``.
``temperature`` / ``top_k`` / ``top_p`` / ``repetition_penalty``
    Sampling; the default is what the model's own configuration recommends.
``progress``
    ``false`` silences the one status line printed per model call.

All translation between the chat-completions format and the model (chat
template, tool-call parsing, context trimming, KV-cache reuse) happens in
tiny_gpt's ``tinygpt/hf/chat_api.py``; this class only finds it, loads the
model once, and maps its errors to :class:`ProviderError`.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from .config import LOCAL_SCHEME
from .provider import Provider, ProviderError

SCHEME = LOCAL_SCHEME

DEFAULT_CONTEXT = 4096
DEFAULT_MAX_TOKENS = 1024

# The one loaded model: {"key": (folder, model, context, weights),
# "engine": ChatEngine}.  A 1.7B-parameter model does not fit in a phone's
# memory twice, so switching models drops the previous one first.
_LOADED = {}


def is_local(base_url) -> bool:
    """True when *base_url* names an on-device model, not an HTTP endpoint."""
    return str(base_url or "").strip().lower().startswith(SCHEME)


def _candidates():
    here = Path(__file__).resolve()
    docs = Path(os.path.expanduser("~/Documents"))
    yield docs / "tiny_gpt"
    yield docs / "tiny_gpt-main"
    # A checkout sitting next to this one (site-packages or a repo folder).
    for parent in (here.parent.parent, here.parent.parent.parent):
        yield parent / "tiny_gpt"
        yield parent / "tiny_gpt-main"


def find_tinygpt(base_url: str = "") -> str:
    """Return the folder holding tiny_gpt's ``hf_run.py``.

    Raises :class:`ProviderError` naming every place that was tried.
    """
    text = str(base_url or "").strip()
    given = text[len(SCHEME):].strip() if is_local(text) else text
    tried = []
    places = [Path(os.path.expanduser(given))] if given else list(_candidates())
    for place in places:
        if (place / "hf_run.py").is_file():
            return str(place)
        tried.append(str(place))
    raise ProviderError(
        "cannot find tiny_gpt (its hf_run.py). Looked in:\n  "
        + "\n  ".join(tried)
        + "\nCopy the tiny_gpt folder there, or name its folder in the "
        "provider's base_url: ':config set base_url "
        "tinygpt://~/Documents/tiny_gpt'."
    )


def _same_folder(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def _import_hf_run(folder: str):
    loaded = sys.modules.get("hf_run")
    if loaded is not None and not _same_folder(
            os.path.dirname(getattr(loaded, "__file__", "") or ""), folder):
        # A different copy of tiny_gpt was imported earlier (the base_url
        # was changed mid-session): forget it rather than mix the two.
        for name in [n for n in list(sys.modules) if n == "hf_run"
                     or n == "tinygpt" or n.startswith("tinygpt.")]:
            del sys.modules[name]
    if folder in sys.path:
        sys.path.remove(folder)
    sys.path.insert(0, folder)
    try:
        module = importlib.import_module("hf_run")
    except Exception as exc:  # ImportError, or a broken copy of tiny_gpt
        raise ProviderError(f"cannot import tiny_gpt from {folder}: {exc}") from exc
    if not hasattr(module, "open_chat_engine"):
        raise ProviderError(
            f"the tiny_gpt in {folder} is too old to drive an agent (its "
            "hf_run.py has no open_chat_engine). Update tiny_gpt."
        )
    return module


class LocalProvider(Provider):
    """``Provider`` whose ``chat()`` runs a tiny_gpt model in-process."""

    def __init__(self, config, api_key: str = ""):
        super().__init__(config, "")
        self.auth_enabled = False

    def endpoint_problem(self) -> str:
        if not self.model:
            return (f"provider {self.name!r} has no model selected; add one "
                    f"with ':provider add {self.name or 'NAME'} {SCHEME} "
                    "LiquidAI/LFM3-1.7B'.")
        try:
            find_tinygpt(self.base_url)
        except ProviderError as exc:
            return str(exc)
        return ""

    # -- the model ---------------------------------

    def _option(self, name, default=None):
        value = self.extra_body.get(name)
        return default if value is None else value

    def _engine(self):
        folder = find_tinygpt(self.base_url)
        try:
            context = int(self._option("context", DEFAULT_CONTEXT))
            max_tokens = int(self._option("max_tokens", DEFAULT_MAX_TOKENS))
        except (TypeError, ValueError):
            raise ProviderError("extra_body context and max_tokens must be numbers")
        weights = str(self._option("weights", "auto"))
        key = (folder, str(self.model), context, weights)
        if _LOADED.get("key") == key:
            return _LOADED["engine"]
        old = _LOADED.pop("engine", None)
        _LOADED.clear()
        if old is not None:
            try:
                old.model.close()
            except Exception:
                pass
            del old
        hf_run = _import_hf_run(folder)
        if self._option("progress", True):
            print(f"· loading {self.model} on this device…")
        try:
            engine = hf_run.open_chat_engine(
                self.model, ctx=context, weights=weights, max_new=max_tokens)
        except hf_run.RunError as exc:
            raise ProviderError(f"{exc}\n(tiny_gpt folder: {folder})") from exc
        except MemoryError as exc:
            raise ProviderError(
                f"out of memory loading {self.model}. Lower the context: "
                "':config set extra_body {\"context\": 3072}'."
            ) from exc
        except Exception as exc:
            raise ProviderError(
                f"could not load {self.model}: {type(exc).__name__}: {exc}"
            ) from exc
        _LOADED["key"] = key
        _LOADED["engine"] = engine
        return engine

    # -- public ------------------------------------

    def chat(self, messages: list, tools=None, tool_choice=None) -> dict:
        engine = self._engine()
        progress = bool(self._option("progress", True))
        think = self._option("enable_thinking")
        try:
            response = engine.chat(
                messages,
                tools=tools,
                max_tokens=self._option("max_tokens"),
                temperature=self._option("temperature"),
                top_k=self._option("top_k"),
                top_p=self._option("top_p"),
                repetition_penalty=self._option("repetition_penalty"),
                enable_thinking=None if think is None else bool(think),
                on_prompt=_announce if progress else None,
            )
        except MemoryError as exc:
            raise ProviderError("out of memory while generating") from exc
        except Exception as exc:
            # ChatError (conversation too long, template refusal) and
            # anything unexpected from the numerics: report, do not crash
            # the session.  KeyboardInterrupt (the stop button) is not an
            # Exception and passes through as for any other provider.
            raise ProviderError(f"local model: {exc}") from exc
        if progress:
            last = engine.last
            print("· %d tokens in %.0fs (%.1f/s)" % (
                last.get("new_tokens", 0),
                last.get("prefill_s", 0) + last.get("decode_s", 0),
                last.get("tokens_per_s", 0)))
        return response


def _announce(new: int, cached: int):
    """Say so before a wait long enough to wonder about."""
    if new >= 200:
        print(f"· reading {new} tokens ({cached} cached)…")
