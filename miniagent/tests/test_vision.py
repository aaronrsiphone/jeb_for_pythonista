"""Tests for the vision module and the ask_image tool (Vision + Tools wiring).

Safe to run via run_python: no network (the HTTP layer is faked), no console
loop, no interactive prompts (permission prompts are scripted), and no
writes outside the system temp directory. Lives permanently in
miniagent/tests/; run it directly or through run_all.py.
"""

import builtins
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

import miniagent.vision as vision_mod  # noqa: E402
from miniagent import permissions as perm_mod  # noqa: E402
from miniagent.config import Config  # noqa: E402
from miniagent.permissions import Permissions  # noqa: E402
from miniagent.tools import CAPABILITY_MAP, TOOL_SCHEMAS, Tools  # noqa: E402
from miniagent.vision import (  # noqa: E402
    Vision,
    VisionError,
    build_payload,
    extract_answer,
    fit_file_bytes,
    load_image_file,
    split_target,
)
from miniagent.workspace import Workspace  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


def check_true(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


def check_raises(name, fn, exc_type, text=None):
    try:
        fn()
    except exc_type as exc:
        if text is not None and text not in str(exc):
            failures.append(name)
            print(f"FAIL: {name}\n  exception text {str(exc)!r} lacks {text!r}")
        else:
            print(f"PASS: {name}")
        return
    except Exception as exc:  # wrong exception type
        failures.append(name)
        print(f"FAIL: {name}\n  raised {type(exc).__name__}: {exc}")
        return
    failures.append(name)
    print(f"FAIL: {name}\n  did not raise {exc_type.__name__}")


# Rule 4 of docs/testing.md: guard builtins.input as belt-and-braces.
def _no_input(message):
    raise AssertionError(f"unexpected interactive prompt: {message!r}")


builtins.input = _no_input


class ScriptedPrompt:
    """Answers a queue of scripted responses; raises when exhausted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, message):
        self.seen.append(message)
        if not self.answers:
            raise AssertionError(
                f"scripted answers exhausted; unexpected prompt: {message!r}"
            )
        return self.answers.pop(0)


class FakeRequests:
    """Stands in for the requests module inside miniagent.vision."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response
        self.exc = exc
        self.RequestException = vision_requests.RequestException

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


import requests as vision_requests  # noqa: E402


class FakeConfig:
    """Duck-typed config: providers + selected provider + vision_model."""

    def __init__(self, providers, provider="mistral", vision_model=""):
        self.providers = providers
        self.provider = provider
        self.vision_model = vision_model


PROVIDERS = {
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "chat_path": "/chat/completions",
        "auth_enabled": True,
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "extra_headers": {},
        "extra_body": {},
        "timeout": 30,
        "models": ["zai-glm-5-3"],
    },
}


def make_image(path, size=(50, 40), fmt="JPEG", noise=False):
    from PIL import Image

    if noise:
        img = Image.effect_noise(size, 64).convert("RGB")
    else:
        img = Image.new("RGB", size, (200, 30, 30))
    img.save(path, fmt)
    return Path(path)


tmp = tempfile.mkdtemp(prefix="ma_vision_test_")
try:
    # --- split_target -------------------------------------------------------
    check("split_target provider/model", split_target("mistral/pixtral-12b-2409"),
          ("mistral", "pixtral-12b-2409"))
    check("split_target bare model", split_target("mistral-large-latest"),
          (None, "mistral-large-latest"))
    check("split_target empty", split_target(""), (None, ""))

    # --- build_payload ------------------------------------------------------
    payload = build_payload(
        "Describe this image.",
        ["data:image/jpeg;base64,AAAA", "https://example.com/x.png"],
        "mistral-large-latest",
        system="Be terse.",
        max_tokens=77,
    )
    check("payload model", payload.get("model"), "mistral-large-latest")
    check("payload max_tokens", payload.get("max_tokens"), 77)
    check("payload message count", len(payload["messages"]), 2)
    check("payload system message", payload["messages"][0],
          {"role": "system", "content": "Be terse."})
    user = payload["messages"][1]
    check("payload user role", user["role"], "user")
    check("payload content types",
          [b["type"] for b in user["content"]],
          ["text", "image_url", "image_url"])
    check("payload text block", user["content"][0],
          {"type": "text", "text": "Describe this image."})
    check("payload image block 1", user["content"][1],
          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}})
    check("payload image block 2", user["content"][2],
          {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}})

    payload_min = build_payload("q", ["data:image/png;base64,BBBB"], "m")
    check("payload minimal keys", sorted(payload_min.keys()), ["messages", "model"])
    check("payload minimal user content", payload_min["messages"][0]["content"],
          [{"type": "text", "text": "q"},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}])

    # --- extract_answer -----------------------------------------------------
    check("extract string", extract_answer(
        {"choices": [{"message": {"content": "hello"}}]}), "hello")
    check("extract block list", extract_answer(
        {"choices": [{"message": {"content": [
            {"type": "text", "text": "one"},
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "two"},
        ]}}]}), "one\ntwo")
    check_raises("extract bad shape raises",
                 lambda: extract_answer({"choices": []}), VisionError)

    # --- image file loading / fitting --------------------------------------
    small = make_image(Path(tmp, "small.jpg"))
    raw_small = small.read_bytes()
    got_raw, got_mime, got_dims, got_shrunk = fit_file_bytes(
        raw_small, "image/jpeg", 2 * 1024 * 1024, 1280)
    check("fit small passes through", got_raw, raw_small)
    check("fit small mime", got_mime, "image/jpeg")
    check("fit small shrunk flag", got_shrunk, False)
    check("fit small dims unknown", got_dims, None)

    big_png = make_image(Path(tmp, "big.png"), size=(1500, 1500),
                         fmt="PNG", noise=True)
    raw_big = big_png.read_bytes()
    got_raw, got_mime, got_dims, got_shrunk = fit_file_bytes(
        raw_big, "image/png", 2 * 1024 * 1024, 1280)
    check_true("fit big shrunk flag", got_shrunk is True)
    check("fit big mime", got_mime, "image/jpeg")
    check_true("fit big dims capped",
               got_dims is not None and max(got_dims) <= 1280,
               f"dims={got_dims}")
    check_true("fit big under cap",
               (4 * len(got_raw) + 2) // 3 + 32 <= 2 * 1024 * 1024,
               f"encoded {len(got_raw)} bytes -> data URL too big")

    check_raises("load missing file", lambda: load_image_file(
        str(Path(tmp, "nope.jpg"))), VisionError, "not found")

    # --- Vision target resolution ------------------------------------------
    vcfg = FakeConfig(PROVIDERS, vision_model="mistral/mistral-large-latest")
    vision = Vision(vcfg, load_key=lambda name: "key-" + name)
    check("target label", vision.target_label(),
          "mistral/mistral-large-latest via https://api.mistral.ai/v1/chat/completions")

    bare = Vision(FakeConfig(PROVIDERS, vision_model="mistral-large-latest"),
                  load_key=lambda name: "k")
    check("target label bare model", bare.target_label(),
          "mistral/mistral-large-latest via https://api.mistral.ai/v1/chat/completions")

    check_raises("no vision_model configured",
                 lambda: Vision(FakeConfig(PROVIDERS), lambda n: "k").ask("q", urls=["https://x/y.png"]),
                 VisionError, "no vision_model configured")
    check_raises("unknown vision provider",
                 lambda: Vision(FakeConfig(PROVIDERS, vision_model="nope/m"),
                               lambda n: "k").ask("q", urls=["https://x/y.png"]),
                 VisionError, "not configured")
    no_base = dict(PROVIDERS)
    no_base["mistral"] = {"models": []}
    check_raises("provider without base_url",
                 lambda: Vision(FakeConfig(no_base, vision_model="mistral/m"),
                                lambda n: "k").ask("q", urls=["https://x/y.png"]),
                 VisionError, "no base_url")

    # --- Vision.ask over a faked HTTP layer ---------------------------------
    ok_response = FakeResponse(200, payload={
        "model": "mistral-large-latest",
        "choices": [{"message": {"content": "A red rectangle."}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    })
    fake_http = FakeRequests(response=ok_response)
    saved_requests = vision_mod.requests
    vision_mod.requests = fake_http
    try:
        result = vision.ask("What shape?", file_paths=[str(small)])
        check("ask ok", result.get("ok"), True)
        check("ask answer", result.get("answer"), "A red rectangle.")
        check("ask provider", result.get("provider"), "mistral")
        check("ask model", result.get("model"), "mistral-large-latest")
        check("ask usage", result.get("usage"),
              {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        check_true("ask labels", len(result.get("images", [])) == 1
                   and result["images"][0].startswith("file:"),
                   f"images={result.get('images')}")
        check("ask call count", len(fake_http.calls), 1)
        call = fake_http.calls[0]
        check("ask url", call["url"], "https://api.mistral.ai/v1/chat/completions")
        check("ask auth header", call["headers"].get("Authorization"), "Bearer key-mistral")
        check("ask content type", call["headers"].get("Content-Type"), "application/json")
        check("ask timeout", call["timeout"], 30)
        body = call["json"]
        check("ask body model", body.get("model"), "mistral-large-latest")
        check("ask body default max_tokens", body.get("max_tokens"), 1024)
        sent_url = body["messages"][0]["content"][1]["image_url"]["url"]
        check_true("ask sends data url",
                   sent_url.startswith("data:image/jpeg;base64,"),
                   f"url={sent_url[:40]}")

        # explicit max_tokens + system pass through
        fake_http.calls.clear()
        vision.ask("q", file_paths=[str(small)], system="terse", max_tokens=9)
        body = fake_http.calls[0]["json"]
        check("ask explicit max_tokens", body.get("max_tokens"), 9)
        check("ask system message", body["messages"][0],
              {"role": "system", "content": "terse"})

        # extra_body from provider settings is merged
        prov2 = {"mistral": dict(PROVIDERS["mistral"],
                                 extra_body={"temperature": 0.2})}
        v2 = Vision(FakeConfig(prov2, vision_model="mistral/mistral-large-latest"),
                   load_key=lambda n: "k")
        fake_http.calls.clear()
        v2.ask("q", file_paths=[str(small)])
        check("ask extra_body merged", fake_http.calls[0]["json"].get("temperature"), 0.2)

        # no API key -> clean error before any HTTP call
        fake_http.calls.clear()
        check_raises("ask without key",
                     lambda: Vision(vcfg, load_key=lambda n: "").ask(
                         "q", file_paths=[str(small)]),
                     VisionError, "no API key available")
        check("ask without key made no calls", len(fake_http.calls), 0)

        # auth disabled -> no Authorization header
        prov3 = {"mistral": dict(PROVIDERS["mistral"], auth_enabled=False)}
        v3 = Vision(FakeConfig(prov3, vision_model="mistral/mistral-large-latest"),
                    load_key=lambda n: "unused")
        fake_http.calls.clear()
        v3.ask("q", file_paths=[str(small)])
        check_true("ask auth disabled drops header",
                   "Authorization" not in fake_http.calls[0]["headers"],
                   f"headers={fake_http.calls[0]['headers']}")

        # HTTP error surfaces as VisionError with the snippet
        err_http = FakeRequests(response=FakeResponse(400, text='{"message": "nope"}'))
        vision_mod.requests = err_http
        check_raises("ask http error",
                     lambda: vision.ask("q", file_paths=[str(small)]),
                     VisionError, "HTTP 400")

        # network failure surfaces as VisionError
        vision_mod.requests = FakeRequests(
            exc=vision_requests.RequestException("boom"))
        check_raises("ask network failure",
                     lambda: vision.ask("q", file_paths=[str(small)]),
                     VisionError, "vision request failed")

        # invalid JSON response
        vision_mod.requests = FakeRequests(response=FakeResponse(200, text="not json"))
        check_raises("ask invalid json",
                     lambda: vision.ask("q", file_paths=[str(small)]),
                     VisionError, "invalid JSON response")

        # no sources at all
        vision_mod.requests = fake_http
        check_raises("ask no sources", lambda: vision.ask("q"), VisionError,
                     "no image sources")
        check_raises("ask empty question", lambda: vision.ask("  ", urls=["https://x/y.png"]),
                     VisionError, "non-empty question")
        check_raises("ask non-http url",
                     lambda: vision.ask("q", urls=["ftp://x/y.png"]),
                     VisionError, "not an http(s)")
    finally:
        vision_mod.requests = saved_requests

    # --- Config: vision_model persistence ----------------------------------
    state = tempfile.mkdtemp(prefix="ma_vision_cfg_")
    cfg = Config(state, data={
        "provider": "mistral",
        "model": "zai-glm-5-3",
        "providers": {"mistral": dict(PROVIDERS["mistral"])},
    })
    cfg.set("vision_model", "mistral/mistral-large-latest")
    reloaded = Config.load(state)
    check("config vision_model persisted", getattr(reloaded, "vision_model", ""),
          "mistral/mistral-large-latest")
    check("config describe shows vision_model",
          "vision_model = 'mistral/mistral-large-latest'" in reloaded.describe(), True)
    check_raises("config set unknown vision provider",
                 lambda: reloaded.set("vision_model", "ghost/m"), KeyError, "Unknown vision provider")
    check_raises("config set vision_model without model",
                 lambda: reloaded.set("vision_model", "mistral/"), KeyError, "model name")
    cfg_plain = Config(tempfile.mkdtemp(prefix="ma_vision_cfg2_"), data={
        "provider": "mistral", "model": "zai-glm-5-3",
        "providers": {"mistral": dict(PROVIDERS["mistral"])},
    })
    check("config describe omits absent vision_model",
          "vision_model" in cfg_plain.describe(), False)

    # --- Tools wiring: schema, capability, dispatch ------------------------
    check_true("schema has ask_image",
               any(t["function"]["name"] == "ask_image" for t in TOOL_SCHEMAS))
    check("capability map entry", CAPABILITY_MAP.get("ask_image"), perm_mod.ASK_IMAGE)
    check("capability registered", "ask_image" in perm_mod.CAPABILITIES, True)

    ws_root = tempfile.mkdtemp(prefix="ma_vision_ws_")
    img = make_image(Path(ws_root, "test.jpg"))
    ws = Workspace(ws_root)

    class FakeVision:
        def __init__(self):
            self.calls = []

        def target_label(self):
            return "mistral/mistral-large-latest via https://api.mistral.ai/v1/chat/completions"

        def ask(self, question, file_paths=(), urls=(), photo=None,
                clipboard=False, system=None, max_tokens=None):
            self.calls.append({
                "question": question, "file_paths": list(file_paths),
                "urls": list(urls), "photo": photo, "clipboard": clipboard,
                "system": system, "max_tokens": max_tokens,
            })
            return {"ok": True, "answer": "A red box.",
                    "provider": "mistral", "model": "mistral-large-latest",
                    "images": ["file:test.jpg"], "usage": {"total_tokens": 3}}


    def dispatch_with(prompt_answers, args, vision_engine, root=ws_root):
        ws_local = Workspace(root)
        prompt = ScriptedPrompt(prompt_answers)
        perms = Permissions(tempfile.mkdtemp(prefix="ma_vision_perm_"),
                            str(root), prompt=prompt)
        tools = Tools(ws_local, perms, None, vision=vision_engine)
        return json.loads(tools.dispatch({
            "id": "t1",
            "function": {"name": "ask_image",
                          "arguments": json.dumps(args)},
        })), prompt

    fake = FakeVision()
    result, prompt = dispatch_with(
        ["y"], {"question": "What is it?", "images": ["test.jpg"]}, fake)
    check("dispatch ok", result.get("ok"), True)
    check("dispatch answer", result.get("answer"), "A red box.")
    check("dispatch consumed prompt", len(prompt.seen), 1)
    check("dispatch resolved workspace path",
         fake.calls[0]["file_paths"], [str(img.resolve())])
    check("dispatch question", fake.calls[0]["question"], "What is it?")

    # denial
    fake2 = FakeVision()
    result, _ = dispatch_with(
        ["n"], {"question": "q", "images": ["test.jpg"]}, fake2)
    check("dispatch denied ok", result.get("ok"), False)
    check("dispatch denied flag", result.get("denied"), True)
    check("dispatch denied made no calls", len(fake2.calls), 0)

    # workspace escape is rejected after approval
    fake3 = FakeVision()
    result, _ = dispatch_with(
        ["y"], {"question": "q", "images": ["../outside.jpg"]}, fake3)
    check("escape denied ok", result.get("ok"), False)
    check_true("escape error mentions workspace",
               "escapes workspace" in str(result.get("error", "")),
               f"error={result.get('error')}")
    check("escape made no calls", len(fake3.calls), 0)

    # no image sources
    fake4 = FakeVision()
    result, _ = dispatch_with(["y"], {"question": "q"}, fake4)
    check("no sources ok false", result.get("ok"), False)
    check_true("no sources error",
               "at least one image source" in str(result.get("error", "")),
               f"error={result.get('error')}")

    # no vision collaborator configured
    result, _ = dispatch_with(["y"], {"question": "q", "images": ["test.jpg"]}, None)
    check("no engine ok false", result.get("ok"), False)
    check_true("no engine error",
               "not configured in this session" in str(result.get("error", "")),
               f"error={result.get('error')}")

    # photo passthrough: index forwarded to the engine
    fake5 = FakeVision()
    result, _ = dispatch_with(["y"], {"question": "q", "photo": -1}, fake5)
    check("photo call ok", result.get("ok"), True)
    check("photo index forwarded", fake5.calls[0]["photo"], -1)

    # bad question type
    fake6 = FakeVision()
    result, _ = dispatch_with(["y"], {"question": "", "images": ["test.jpg"]}, fake6)
    check("empty question ok false", result.get("ok"), False)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for name in failures:
        print(f"  - {name}")
    raise AssertionError(f"{len(failures)} vision test check(s) failed")
print("All vision checks passed.")
