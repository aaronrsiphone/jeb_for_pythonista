"""Tests for the on-device provider (local_provider.py).

Safe to run via run_python: no network, no model, no console loop.  The real
thing loads a multi-gigabyte model through tiny_gpt; here a temporary folder
holding a ten-line fake ``hf_run.py`` stands in for tiny_gpt, so what is
tested is everything on this side of that boundary:

* a ``tinygpt://`` base_url selects ``LocalProvider`` everywhere a provider
  is built (``make_provider``), and anything else still gets the HTTP one;
* config accepts the scheme, never asks such a provider for an API key, and
  ``:provider models`` answers from the configured list;
* the tiny_gpt folder is found from the base_url, and a missing one is
  reported with the places that were tried;
* ``extra_body`` options reach the engine, the model is loaded once and
  reloaded only when a load setting changes;
* every failure (model not downloaded, an old tiny_gpt, an error while
  generating) becomes a ``ProviderError``, so a turn ends in ``TurnFailed``
  instead of a traceback;
* a whole agent turn — model asks for a tool, gets the result, answers —
  works through the real ``Agent`` loop.

Lives permanently in miniagent/tests/; run it directly or through
run_all.py.
"""

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these new modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")
             or n == "hf_run"]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent import local_provider as lp  # noqa: E402
from miniagent.agent import Agent  # noqa: E402
from miniagent.config import Config, _provider_name_from_url  # noqa: E402
from miniagent.events import (  # noqa: E402
    AssistantText,
    ReasoningChunk,
    TurnEnded,
    TurnFailed,
)
from miniagent.provider import (  # noqa: E402
    Provider,
    ProviderError,
    list_models,
    make_provider,
)
from miniagent.ui import drive  # noqa: E402
from miniagent.ui.headless import Headless  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}\n  got:  {got!r}\n  want: {want!r}")
        failures.append(name)


def check_in(name, needle, haystack):
    check(name, needle in haystack, True)
    if needle not in haystack:
        print(f"  text was: {haystack!r}")


def raises(fn):
    try:
        fn()
    except ProviderError as exc:
        return str(exc)
    return None


# A stand-in for tiny_gpt's hf_run.py.  It records how it was called in
# module globals the tests read back, and replays REPLIES.
FAKE_HF_RUN = '''
class RunError(Exception):
    pass

LOADS = []
CALLS = []
REPLIES = []
FAIL_LOAD = None

class Engine(object):
    def __init__(self):
        self.last = {"new_tokens": 3, "prefill_s": 1.0, "decode_s": 1.0,
                     "tokens_per_s": 3.0}
        self.model = self
        self.closed = False

    def close(self):
        self.closed = True

    def chat(self, messages, tools=None, on_prompt=None, **options):
        CALLS.append({"messages": list(messages), "tools": tools,
                      "options": options})
        if on_prompt:
            on_prompt(2600, 0)
        reply = REPLIES.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"index": 0, "message": reply}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}}

def open_chat_engine(model_id=None, **kw):
    if FAIL_LOAD:
        raise RunError(FAIL_LOAD)
    LOADS.append((model_id, kw))
    return Engine()
'''


def settings(base_url, **extra_body):
    return SimpleNamespace(
        provider="lfm", base_url=base_url, model="LiquidAI/LFM3-1.7B",
        extra_body=extra_body, path="/state/config.json")


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    fake = tmp / "tiny_gpt"
    fake.mkdir()
    (fake / "hf_run.py").write_text(FAKE_HF_RUN)
    url = f"tinygpt://{fake}"

    # --- which provider is built ------------------------------------------

    check("tinygpt:// is local", lp.is_local("TinyGPT://x"), True)
    check("https:// is not local", lp.is_local("https://api.x/v1"), False)
    check("make_provider: local", type(make_provider(settings(url))).__name__,
          "LocalProvider")
    check("make_provider: http",
          type(make_provider(settings("https://api.x/v1"), "k")), Provider)

    # --- config -----------------------------------------------------------

    state = tmp / "state"
    state.mkdir()
    config = Config(str(state), {})
    config.add_provider("lfm", "tinygpt://", ["LiquidAI/LFM3-1.7B"])
    check("add_provider keeps the scheme", config.base_url, "tinygpt://")
    check("add_provider turns auth off", config.auth_enabled, False)
    check("the model is selected", config.model, "LiquidAI/LFM3-1.7B")
    check("config -> LocalProvider", type(make_provider(config)).__name__,
          "LocalProvider")
    reloaded = Config(str(state), {"provider": "p", "providers": {
        "p": {"base_url": url, "models": ["m"], "auth_enabled": True}}})
    check("a hand-written entry never wants a key", reloaded.auth_enabled, False)
    check("default provider name", _provider_name_from_url("tinygpt://"), "local")
    try:
        config.add_provider("bad", "ftp://x", [])
        check("other schemes are still refused", "accepted", "ValueError")
    except ValueError as exc:
        check_in("the refusal mentions the local scheme", "tinygpt://", str(exc))
    check("list_models answers from the config",
          list_models({"base_url": url, "models": ["b", "a"]}), ["a", "b"])

    # --- finding tiny_gpt -------------------------------------------------

    check("folder from the base_url", lp.find_tinygpt(url), str(fake))
    missing = raises(lambda: lp.find_tinygpt(f"tinygpt://{tmp / 'nowhere'}"))
    check_in("a missing folder is named", str(tmp / "nowhere"), missing or "")
    check_in("... with what to do", "base_url", missing or "")
    problem = make_provider(settings(f"tinygpt://{tmp / 'nowhere'}")).endpoint_problem()
    check_in("endpoint_problem reports it", "cannot find tiny_gpt", problem)
    check("endpoint_problem is quiet when fine",
          make_provider(settings(url)).endpoint_problem(), "")

    # --- chat -------------------------------------------------------------

    provider = make_provider(settings(url, context=3072, temperature=0,
                                      enable_thinking=False))
    hf_run = lp._import_hf_run(str(fake))  # the fake

    def chat(p, *replies):
        hf_run.REPLIES[:] = replies
        out = io.StringIO()
        with redirect_stdout(out):
            result = p.chat([{"role": "user", "content": "hi"}],
                            tools=[{"type": "function"}])
        return result, out.getvalue()

    result, printed = chat(provider, {"role": "assistant", "content": "hello"})
    check("the engine's response is returned as is",
          Provider.extract_message(result)["content"], "hello")
    check("load settings reach open_chat_engine", hf_run.LOADS, [
        ("LiquidAI/LFM3-1.7B", {"ctx": 3072, "weights": "auto",
                                "max_new": 1024})])
    call = hf_run.CALLS[-1]
    check("messages and tools are passed through",
          (call["messages"], call["tools"]),
          ([{"role": "user", "content": "hi"}], [{"type": "function"}]))
    check("sampling options come from extra_body",
          (call["options"]["temperature"], call["options"]["enable_thinking"],
           call["options"]["top_k"]), (0, False, None))
    check_in("loading is announced", "loading LiquidAI/LFM3-1.7B", printed)
    check_in("a long prompt is announced", "reading 2600 tokens", printed)
    check_in("the speed is reported", "3 tokens in 2s (3.0/s)", printed)

    again = make_provider(settings(url, context=3072))
    chat(again, {"role": "assistant", "content": "x"})
    check("a second provider reuses the loaded model", len(hf_run.LOADS), 1)
    first_engine = lp._LOADED["engine"]
    bigger = make_provider(settings(url, context=8192, progress=False))
    _, printed = chat(bigger, {"role": "assistant", "content": "x"})
    check("a different context reloads", [kw["ctx"] for _, kw in hf_run.LOADS],
          [3072, 8192])
    check("... after closing the old model", first_engine.closed, True)
    check("progress: false prints nothing", printed, "")

    # --- failures ---------------------------------------------------------

    message = raises(lambda: chat(bigger, ValueError("conversation is too long")))
    check_in("an engine error becomes a ProviderError",
             "local model: conversation is too long", message or "")
    hf_run.FAIL_LOAD = "LiquidAI/LFM3-1.7B is not downloaded yet."
    lp._LOADED.clear()
    message = raises(lambda: chat(bigger, {"role": "assistant", "content": "x"}))
    check_in("a load error becomes a ProviderError", "not downloaded yet",
             message or "")
    check_in("... naming the tiny_gpt folder", str(fake), message or "")
    hf_run.FAIL_LOAD = None

    old = tmp / "old_tiny_gpt"
    old.mkdir()
    (old / "hf_run.py").write_text("MODE = 'chat'\n")
    message = raises(lambda: make_provider(
        settings(f"tinygpt://{old}", progress=False)).chat([]))
    check_in("a tiny_gpt without open_chat_engine is reported", "too old",
             message or "")

    # --- a whole turn through the agent -----------------------------------

    lp._LOADED.clear()
    provider = make_provider(settings(url, progress=False))
    hf_run = lp._import_hf_run(str(fake))  # again: the old copy replaced it

    class OneTool:
        schemas = [{"type": "function", "function": {"name": "read_file"}}]

        def dispatch(self, call):
            return json.dumps({"content": "x = 4"})
            yield  # pragma: no cover  (makes this a generator)

    hf_run.REPLIES[:] = [
        {"role": "assistant", "content": "", "reasoning_content": "look",
         "tool_calls": [{"id": "call_1", "type": "function", "function": {
             "name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]},
        {"role": "assistant", "content": "x is 4"},
        RuntimeError("boom"),
    ]
    agent = Agent(provider, OneTool(), "SYSTEM")
    renderer = Headless()
    out = io.StringIO()
    with redirect_stdout(out):
        text = drive(agent, "what is x?", renderer)
    check("the turn's answer", text, "x is 4")
    check("event types", [type(e).__name__ for e in renderer.events],
          [ReasoningChunk.__name__, AssistantText.__name__, TurnEnded.__name__])
    second = hf_run.CALLS[-1]["messages"]
    check("the tool result went back to the model",
          [m["role"] for m in second], ["system", "user", "assistant", "tool"])
    check("usage is summed across the turn",
          renderer.events[-1].usage.get("prompt_tokens"), 20)
    renderer = Headless()
    with redirect_stdout(out):
        drive(agent, "again", renderer)
    check("a failing model ends the turn in TurnFailed",
          type(renderer.events[-1]), TurnFailed)
    check("nothing printed with progress off", out.getvalue(), "")

    lp._LOADED.clear()
    for name in [n for n in list(sys.modules) if n == "hf_run"]:
        del sys.modules[name]
    for folder in (str(fake), str(old)):
        while folder in sys.path:
            sys.path.remove(folder)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} local-provider check(s) failed")
print("All checks passed.")
