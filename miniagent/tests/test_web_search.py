"""Tests for the web_search tool, its Brave client and its search model wiring.

Safe to run via run_python: no network (every Brave request goes through a
fake ``send``, every model call through a scripted provider), no console
loop, no interactive prompts, and no writes outside the system temp
directory.  Covers:

* ``brave.py`` — query and freshness validation, request shape, the
  2/s throttle, 429 retry, error mapping, the persisted monthly quota and
  its month rollover, and rendering of LLM Context responses;
* ``tools/web_search.py`` — the event stream (exactly one start/complete
  pair, progress in between), the search budget and the empty-schema
  trick that forces an answer, failure paths, the answer cap, and the
  point of the whole design: the caller's conversation receives the
  answer and never the extracted page text;
* ``websearch.py`` — search model resolution and its provider;
* ``config.py`` ``search_model``, ``:key set brave``, and the console
  renderer's progress line.
"""

import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

import miniagent.console  # noqa: E402,F401  (registers the commands)
from miniagent import brave as brave_mod  # noqa: E402
from miniagent.agent import Agent  # noqa: E402
from miniagent.brave import (  # noqa: E402
    BraveSearch,
    DEFAULT_PARAMS,
    ENDPOINT,
    MONTHLY_LIMIT,
    SearchError,
    render,
    validate_freshness,
    validate_query,
)
from miniagent.config import Config  # noqa: E402
from miniagent.console.registry import get_spec as get_command  # noqa: E402
from miniagent.events import (  # noqa: E402
    PermissionNeeded,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
)
from miniagent.keys import BRAVE_ENV, load_brave_key  # noqa: E402
from miniagent.permissions import WEB_SEARCH, Permissions  # noqa: E402
from miniagent.provider import ProviderError  # noqa: E402
from miniagent.runner import Runner  # noqa: E402
from miniagent.tools import Tools  # noqa: E402
# Not `from miniagent.tools import web_search`: the package re-exports the
# tool under that name, which shadows the submodule with its ToolSpec.
ws = importlib.import_module("miniagent.tools.web_search")  # noqa: E402
from miniagent.tools.registry import all_specs  # noqa: E402
from miniagent.ui import drive  # noqa: E402
from miniagent.ui.console import Console  # noqa: E402
from miniagent.ui.headless import Headless  # noqa: E402
from miniagent.websearch import INNER_TIMEOUT, WebSearch  # noqa: E402
from miniagent.workspace import Workspace  # noqa: E402

failures = []
DISPATCH_BUG = "Unexpected error in"


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


def raises(fn, exc_type=SearchError):
    """Return the exception message if *fn* raises *exc_type*, else None."""
    try:
        fn()
    except exc_type as exc:
        return str(exc)
    return None


# --- fakes -----------------------------------------------------------------

ADAM_URL = "https://arxiv.org/abs/1412.6980"
FILLER = "filler-passage " * 400  # ~6 KB of extracted page text


def sample_response(extra_snippet=""):
    snippets = ["m_t = beta_1 m_{t-1} + (1 - beta_1) g_t",
                '{"table": [["beta_1", "0.9"], ["beta_2", "0.999"]]}']
    if extra_snippet:
        snippets.append(extra_snippet)
    return {
        "grounding": {"generic": [{"url": ADAM_URL,
                                   "title": "Adam: A Method for Stochastic Optimization",
                                   "snippets": snippets}],
                      "map": []},
        "sources": {ADAM_URL: {"title": "Adam", "hostname": "arxiv.org",
                               "age": ["Monday, December 22, 2014", "2014-12-22",
                                       "11 years ago", "2014-12-22T00:00:00Z"]}},
    }


class FakeSend:
    """Records requests; replies from a script of (status, payload) pairs."""

    def __init__(self, replies=None, default=(200, None)):
        self.replies = list(replies or [])
        self.default = default
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers),
                           "body": dict(body), "timeout": timeout})
        status, payload = self.replies.pop(0) if self.replies else self.default
        return status, (sample_response() if payload is None else payload)


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class ScriptedProvider:
    """A chat provider replaying scripted assistant messages."""

    def __init__(self, script, default=None):
        self.script = list(script)
        self.default = default if default is not None else {"content": "done"}
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        step = self.script.pop(0) if self.script else self.default
        if callable(step):
            step = step(messages, tools)
        if isinstance(step, Exception):
            raise step
        return {"choices": [{"message": {"role": "assistant", **step}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


def search_call(query, call_id="c1", **extra):
    return {"content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "brave_search",
                     "arguments": json.dumps({"query": query, **extra})}}]}


class FakeWeb:
    def __init__(self, brave, provider):
        self.brave = brave
        self._provider = provider

    def make_provider(self):
        return self._provider

    def target_label(self):
        return "fake/search-model"


def make_brave(tmp, send=None, key="brave-key", month="2026-10"):
    t = FakeTime()
    client = BraveSearch(load_key=lambda: key, state_dir=tmp,
                         send=send or FakeSend(), clock=t.clock,
                         sleep=t.sleep, month=lambda: month)
    return client, t


def make_tools(tmp, web):
    root = Path(tmp) / "ws"
    root.mkdir(exist_ok=True)
    state = Path(tmp) / "state"
    state.mkdir(exist_ok=True)
    workspace = Workspace(str(root))
    return Tools(workspace, Permissions(str(state), str(root)),
                 Runner(workspace), web=web)


class Run:
    def __init__(self, events, result):
        self.events = events
        self.result = result
        self.payload = json.loads(result)

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]

    @property
    def kinds(self):
        return [type(e).__name__ for e in self.events]


def dispatch(tools, prompt, answers=("y",)):
    answers = list(answers)
    call = {"id": "w1", "function": {"name": "web_search",
                                     "arguments": json.dumps({"prompt": prompt})}}
    gen = tools.dispatch(call)
    events, to_send = [], None
    while True:
        try:
            event = gen.send(to_send)
        except StopIteration as stop:
            return Run(events, stop.value)
        events.append(event)
        to_send = None
        if isinstance(event, PermissionNeeded):
            to_send = Permissions.parse_answer(answers.pop(0) if answers else "")


tmp_roots = []


def tmpdir():
    d = tempfile.mkdtemp(prefix="miniagent_websearch_")
    tmp_roots.append(d)
    return d


saved_brave_env = os.environ.pop(BRAVE_ENV, None)

try:
    # --- validation ---------------------------------------------------------

    check("query is stripped", validate_query("  adam update rule "), "adam update rule")
    check("empty query refused", bool(raises(lambda: validate_query("  "))), True)
    check("601-char query refused",
          "600" in (raises(lambda: validate_query("a" * 601)) or ""), True)
    check("76-word query refused, naming the limit",
          "75" in (raises(lambda: validate_query("w " * 76)) or ""), True)
    check("75-word query accepted", len(validate_query("w " * 75).split()), 75)
    for value in ("pd", "pw", "pm", "py", "2022-04-01to2022-07-30"):
        check(f"freshness {value} accepted", validate_freshness(value), value)
    check("blank freshness means none", validate_freshness(""), None)
    check("bad freshness refused", bool(raises(lambda: validate_freshness("week"))), True)

    # --- request shape --------------------------------------------------------

    tmp = tmpdir()
    send = FakeSend()
    client, clock = make_brave(tmp, send)
    client.search("adam optimizer", freshness="py", goggles="$boost,site=arxiv.org")
    req = send.calls[0]
    check("POSTs to the LLM Context endpoint", req["url"], ENDPOINT)
    check("endpoint is llm/context", ENDPOINT.endswith("/res/v1/llm/context"), True)
    check("auth header carries the key", req["headers"].get("X-Subscription-Token"), "brave-key")
    check("asks for JSON", req["headers"].get("Accept"), "application/json")
    check("no Api-Version header unless pinned", "Api-Version" in req["headers"], False)
    check("query in body", req["body"]["q"], "adam optimizer")
    check("freshness in body", req["body"]["freshness"], "py")
    check("goggle in body", req["body"]["goggles"], "$boost,site=arxiv.org")
    check("strict relevance threshold", req["body"]["context_threshold_mode"], "strict")
    for k, v in DEFAULT_PARAMS.items():
        check(f"default param {k}", req["body"][k], v)
    check("30 s timeout", req["timeout"], 30)

    client.search("second query")
    check("no freshness key when not given", "freshness" in send.calls[1]["body"], False)
    check("2/s throttle: back-to-back request waits 0.5 s", clock.sleeps, [0.5])

    # --- 429 retry and error mapping ----------------------------------------

    tmp = tmpdir()
    send = FakeSend([(429, {"error": "slow down"}), (200, None)])
    client, clock = make_brave(tmp, send)
    result = client.search("adam")
    check("429 then 200: succeeds", isinstance(result, dict), True)
    check("429 then 200: one retry", len(send.calls), 2)
    check("429 then 200: paused 1 s before retrying", 1.0 in clock.sleeps, True)
    check("429 then 200: counted once (429 not counted)", client.usage()[1], 1)

    tmp = tmpdir()
    send = FakeSend([(429, "x"), (429, "x")])
    client, _ = make_brave(tmp, send)
    msg = raises(lambda: client.search("adam")) or ""
    check("two 429s: clean rate-limit error", "429" in msg, True)
    check("two 429s: not counted", client.usage()[1], 0)

    for status, payload, needle in ((401, "nope", "API key"),
                                    (500, "boom", "HTTP 500"),
                                    (200, "not json", "unreadable")):
        client, _ = make_brave(tmpdir(), FakeSend([(status, payload)]))
        msg = raises(lambda: client.search("adam")) or ""
        check(f"HTTP {status} maps to a clean error", needle in msg, True)

    send = FakeSend()
    client, _ = make_brave(tmpdir(), send, key="")
    msg = raises(lambda: client.search("adam")) or ""
    check("missing key: tells the user how to set it", ":key set brave" in msg, True)
    check("missing key: nothing sent", send.calls, [])

    # --- monthly quota ------------------------------------------------------

    tmp = tmpdir()
    send = FakeSend()
    client, _ = make_brave(tmp, send)
    client.search("one")
    client.search("two")
    usage_file = Path(tmp) / brave_mod.USAGE_FILENAME
    check("usage persisted in the state dir",
          json.loads(usage_file.read_text(encoding="utf-8")), {"2026-10": 2})
    check("usage() reads it back", client.usage(), ("2026-10", 2))
    check("remaining() counts down", client.remaining(), MONTHLY_LIMIT - 2)

    usage_file.write_text(json.dumps({"2026-10": MONTHLY_LIMIT}), encoding="utf-8")
    before = len(send.calls)
    msg = raises(lambda: client.search("three")) or ""
    check("at the monthly limit: refused", "monthly limit" in msg, True)
    check("at the monthly limit: nothing sent", len(send.calls), before)

    next_month, _ = make_brave(tmp, send, month="2026-11")
    next_month.search("new month")
    check("a new month starts a fresh count", next_month.usage(), ("2026-11", 1))

    # --- rendering ----------------------------------------------------------

    text = render(sample_response())
    lines = text.splitlines()
    check("render: numbered title with host and date",
          lines[0], "[1] Adam: A Method for Stochastic Optimization (arxiv.org, 2014-12-22)")
    check("render: URL on its own line", lines[1], ADAM_URL)
    check("render: JSON snippet passed through unescaped",
          '{"table": [["beta_1", "0.9"]' in text, True)
    check("render: no doubled escaping", '\\"' in text, False)
    check("render: empty grounding is explicit",
          render({"grounding": {"generic": []}, "sources": {}}), "No relevant results.")
    check("render: non-dict is explicit", render(None), "No relevant results.")
    no_date = sample_response()
    no_date["sources"][ADAM_URL]["age"] = []
    check("render: unknown date omitted",
          render(no_date).splitlines()[0].endswith("(arxiv.org)"), True)
    capped = render(sample_response(extra_snippet=FILLER), cap=500)
    check("render: capped with a marker",
          (len(capped) <= 520, capped.endswith("[... truncated]")), (True, True))

    # --- registry and goggle ------------------------------------------------

    check("web_search is the only delegating tool",
          sorted(s.name for s in all_specs() if s.delegates), ["web_search"])
    goggle_lines = [ln for ln in ws.GOGGLE.splitlines()
                    if ln.strip() and not ln.startswith("!")]
    check("goggle: every instruction within Brave's 500-char limit",
          [ln for ln in goggle_lines if len(ln) > 500], [])
    check("goggle: at most two * and two ^ per instruction",
          [ln for ln in goggle_lines if ln.count("*") > 2 or ln.count("^") > 2], [])
    check("goggle: every instruction has an action or option",
          [ln for ln in goggle_lines if "$" not in ln], [])
    check("goggle: not a whitelist by default (no bare $discard)",
          "$discard" in [ln.strip() for ln in goggle_lines], False)
    check("goggle: arXiv boosted", "$boost=4,site=arxiv.org" in goggle_lines, True)
    check("goggle: news discarded", "$discard,site=reuters.com" in goggle_lines, True)
    check("system prompt placeholders all substituted",
          ("{today}" in ws._system_prompt(), "{max_searches}" in ws._system_prompt()),
          (False, False))

    # --- the tool: happy path and the event stream ---------------------------

    tmp = tmpdir()
    send = FakeSend()
    client, _ = make_brave(tmp, send)
    inner = ScriptedProvider([search_call("adam optimizer update rule"),
                              {"content": "Adam: $m_t = \\beta_1 m_{t-1}$\n" + ADAM_URL}])
    tools = make_tools(tmp, FakeWeb(client, inner))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        run = dispatch(tools, "What is the Adam update rule? Context: comparing optimizers.")
    check("engine printed nothing", out.getvalue(), "")
    check("event stream: start, prompt, progress, complete",
          run.kinds, ["ToolStarted", "PermissionNeeded", "ToolProgress", "ToolCompleted"])
    check("exactly one start/complete pair, both web_search",
          [e.name for e in run.of(ToolStarted) + run.of(ToolCompleted)],
          ["web_search", "web_search"])
    check("progress line names the query",
          run.of(ToolProgress)[0].text, "search: adam optimizer update rule")
    prompt_event = run.of(PermissionNeeded)[0]
    check("gated as web_search", prompt_event.capability, WEB_SEARCH)
    check("preview shows the outgoing prompt",
          "What is the Adam update rule?" in prompt_event.details, True)
    check("preview names the search model",
          "fake/search-model" in prompt_event.details, True)
    check("preview shows the monthly count",
          f"0/{MONTHLY_LIMIT} used in 2026-10" in prompt_event.details, True)
    check("status ok", run.of(ToolCompleted)[0].status, "ok")
    check("answer returned", run.payload.get("answer", "").startswith("Adam:"), True)
    check("search count reported", run.payload.get("searches"), 1)
    check("monthly count reported", run.payload.get("brave_used_this_month"), 1)
    check("goggle sent with every search", send.calls[0]["body"].get("goggles"), ws.GOGGLE)
    check("inner agent received rendered passages, not JSON",
          any(m.get("role") == "tool" and m["content"].startswith("[1] Adam")
              for m in inner.calls[1]["messages"]), True)
    check("inner agent ran on its own system prompt",
          "research search agent" in inner.calls[0]["messages"][0]["content"], True)

    # --- budget: the empty-schema trick ------------------------------------

    tmp = tmpdir()
    send = FakeSend()
    client, _ = make_brave(tmp, send)
    counter = {"n": 0}

    def greedy(messages, tools_offered):
        if not tools_offered:
            return {"content": "Best answer from what was found."}
        counter["n"] += 1
        return search_call(f"query {counter['n']}", call_id=f"g{counter['n']}")

    inner = ScriptedProvider([], default=greedy)
    run = dispatch(make_tools(tmp, FakeWeb(client, inner)), "keep searching")
    check("budget: exactly MAX_SEARCHES requests sent", len(send.calls), ws.MAX_SEARCHES)
    check("budget: tools withdrawn once spent", inner.calls[-1]["tools"], [])
    check("budget: model then answered", run.payload.get("ok"), True)
    check("budget: one progress line per search",
          len(run.of(ToolProgress)), ws.MAX_SEARCHES)

    # --- an invalid query costs nothing and is reported --------------------

    tmp = tmpdir()
    send = FakeSend()
    client, _ = make_brave(tmp, send)
    inner = ScriptedProvider([search_call("w " * 80), search_call("short query", "c2"),
                              {"content": "answer"}])
    run = dispatch(make_tools(tmp, FakeWeb(client, inner)), "q")
    check("invalid query: not sent", len(send.calls), 1)
    check("invalid query: error reached the inner model",
          any(m.get("role") == "tool" and m["content"].startswith("ERROR: query is 80 words")
              for m in inner.calls[1]["messages"]), True)
    check("invalid query: shown as failed progress",
          any(e.text.startswith("search failed: query is 80 words")
              for e in run.of(ToolProgress)), True)
    check("invalid query: did not use up the budget", run.payload.get("searches"), 1)

    # --- failure paths ------------------------------------------------------

    tmp = tmpdir()
    client, _ = make_brave(tmp)
    inner = ScriptedProvider([ProviderError("HTTP 503")])
    run = dispatch(make_tools(tmp, FakeWeb(client, inner)), "q")
    check("search model failure: clean error",
          (run.payload.get("ok"), "search model request failed" in run.payload.get("error", "")),
          (False, True))

    tmp = tmpdir()
    client, _ = make_brave(tmp)
    inner = ScriptedProvider([], default=lambda m, t: search_call("again"))
    run = dispatch(make_tools(tmp, FakeWeb(client, inner)), "q")
    check("out of steps: clean error",
          "without producing an answer" in run.payload.get("error", ""), True)
    check("out of steps: still one start/complete pair",
          (len(run.of(ToolStarted)), len(run.of(ToolCompleted))), (1, 1))

    tmp = tmpdir()
    client, _ = make_brave(tmp)
    inner = ScriptedProvider([{"content": "x" * (ws.ANSWER_CAP + 500)}])
    run = dispatch(make_tools(tmp, FakeWeb(client, inner)), "q")
    check("answer capped with a marker",
          (len(run.payload["answer"]) <= ws.ANSWER_CAP + 20,
           run.payload["answer"].endswith("[... truncated]")), (True, True))

    for label, web, needle in (
            ("not configured", None, "not configured"),
            ("no Brave key", FakeWeb(make_brave(tmpdir(), key="")[0], ScriptedProvider([])),
             ":key set brave")):
        run = dispatch(make_tools(tmpdir(), web), "q")
        check(f"{label}: clean implementation error",
              (run.payload.get("ok"), needle in run.payload.get("error", ""),
               DISPATCH_BUG in run.result), (False, True, False))

    run = dispatch(make_tools(tmpdir(), FakeWeb(make_brave(tmpdir())[0], ScriptedProvider([]))),
                   "q", answers=("n",))
    check("denied: no search run", run.payload.get("denied"), True)

    # --- the point of the design: the caller never sees the page text ------

    tmp = tmpdir()
    send = FakeSend(default=(200, sample_response(extra_snippet=FILLER)))
    client, _ = make_brave(tmp, send)
    inner = ScriptedProvider([search_call("adam update rule"),
                              {"content": "m_t = beta_1 m_{t-1} + (1-beta_1) g_t\n" + ADAM_URL}])
    tools = make_tools(tmp, FakeWeb(client, inner))
    outer = ScriptedProvider([
        {"content": "", "tool_calls": [{"id": "p1", "type": "function", "function": {
            "name": "web_search",
            "arguments": json.dumps({"prompt": "Adam update rule, with the source."})}}]},
        {"content": "Here it is."},
    ])
    caller = Agent(outer, tools, "caller system prompt")
    headless = Headless(answers=["y"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        final = drive(caller, "explain adam", headless)
    tool_msgs = [m for m in caller.messages if m.get("role") == "tool"]
    inner_tool_chars = sum(len(m["content"]) for m in inner.calls[1]["messages"]
                           if m.get("role") == "tool")
    check("caller: turn finished", final, "Here it is.")
    check("caller: one tool message, from web_search",
          [m.get("name") for m in tool_msgs], ["web_search"])
    check("caller: extracted page text never entered its history",
          any("filler-passage" in str(m.get("content")) for m in caller.messages), False)
    check("caller: the inner agent did see it",
          inner_tool_chars > len(FILLER) // 2, True)
    check("caller: its tool message is small",
          len(tool_msgs[0]["content"]) < 600, True)
    check("caller: progress reached the renderer",
          [e.text for e in headless.events if isinstance(e, ToolProgress)],
          ["search: adam update rule"])
    check("caller: nothing printed", out.getvalue(), "")

    # --- search model resolution ---------------------------------------------

    state = tmpdir()
    data = {"provider": "main", "model": "big",
            "providers": {"main": {"base_url": "https://main/v1", "models": ["big"],
                                   "timeout": 120},
                          "cheap": {"base_url": "https://cheap/v1", "models": ["small"],
                                    "timeout": 20},
                          "local": {"base_url": "http://local/v1", "models": ["tiny"],
                                    "auth_enabled": False}}}
    config = Config(state, data=json.loads(json.dumps(data)))
    keys = {"main": "k-main", "cheap": "k-cheap"}
    web = WebSearch(config, load_key=lambda n: keys.get(n, ""), brave=None)
    check("unset search_model: uses the selected chat model", web.target()[:2], ("main", "big"))
    p = web.make_provider()
    check("provider: model and key", (p.model, p.api_key), ("big", "k-main"))
    check("provider: timeout capped for the search agent", p.timeout, INNER_TIMEOUT)
    config.set("search_model", "cheap/small")
    check("search_model selects provider/model", web.target()[:2], ("cheap", "small"))
    p = web.make_provider()
    check("provider: its own key and base URL",
          (p.api_key, p.base_url, p.timeout), ("k-cheap", "https://cheap/v1", 20))
    check("describe() shows search_model", "search_model = 'cheap/small'" in config.describe(), True)
    check("unknown provider refused at set()",
          raises(lambda: config.set("search_model", "nope/x"), KeyError) is not None, True)
    config.set("search_model", "local/tiny")
    check("auth-disabled provider needs no key", web.make_provider().api_key, "")
    keys.pop("cheap")
    config.set("search_model", "cheap/small")
    check("missing key for the search model: clean error",
          "no API key" in (raises(web.make_provider) or ""), True)
    check("label for the preview", web.target_label(), "cheap/small")

    # --- :key set brave, and the console progress line -----------------------

    class Ctx:
        pass

    ctx = Ctx()
    ctx.config = config
    ctx.provider = SimpleNamespace(api_key="k-main")  # bare :key reads the live key
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        get_command("key")(ctx, "set brave bk-123")
    check(":key set brave stores the Brave key", load_brave_key(), "bk-123")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        get_command("key")(ctx, "")
    check(":key reports the Brave key", "Brave Search API key stored: True" in buf.getvalue(), True)
    check(":key still reports the provider key",
          "API key stored for provider" in buf.getvalue(), True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        Console().handle(ToolProgress("web_search", "search: adam"))
    check("console renders progress indented under the tool", buf.getvalue(), "  ↳ search: adam\n")

finally:
    os.environ.pop(BRAVE_ENV, None)
    if saved_brave_env is not None:
        os.environ[BRAVE_ENV] = saved_brave_env
    for d in tmp_roots:
        shutil.rmtree(d, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} web_search check(s) failed")
print("All checks passed.")
