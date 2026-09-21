"""Tests for the console command registry and its handlers (§5.5, §5.6).

Drives every handler directly against a Context built over temp
directories, with a stub Agent/Provider so no network is ever touched.
builtins.input is stubbed only for the handlers that genuinely prompt
(:model bare, :clear-perms, :undo with something to undo). No console
loop is started here beyond two focused checks of loop dispatch itself
(unknown command, clean :quit) — a fuller end-to-end drive of the loop is
covered separately, outside the checked-in suite, per the rearchitecture
plan's verification steps. Lives permanently in miniagent/tests/; run it
directly or through run_all.py.
"""

import builtins
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

# Re-import the package fresh so the current (edited) source is exercised.
# The runner's post-run purge removes these modules again afterwards.
for name in [n for n in list(sys.modules)
             if n == "miniagent" or n.startswith("miniagent.")]:
    del sys.modules[name]

# The tests live in miniagent/tests/, so the importable package root (the
# site-packages directory containing miniagent/) is three levels up.
parent = Path(__file__).resolve().parent.parent.parent
if str(parent) not in sys.path:
    sys.path.insert(0, str(parent))

from miniagent.agent import Agent  # noqa: E402
from miniagent.checkpoints import Checkpoints  # noqa: E402
from miniagent.config import Config  # noqa: E402
from miniagent.console import commands as _commands  # noqa: E402,F401 (registers)
from miniagent.console import loop as console_loop_mod  # noqa: E402
from miniagent.console.registry import (  # noqa: E402
    QUIT,
    all_specs,
    command,
    get_spec,
    render_command_list,
    render_help,
)
from miniagent.context import Context  # noqa: E402
from miniagent.events import ToolCompleted, ToolStarted  # noqa: E402
from miniagent.permissions import Permissions  # noqa: E402
from miniagent.provider import Provider  # noqa: E402
from miniagent.sessions import SessionLogger  # noqa: E402
from miniagent.ui import get_renderer  # noqa: E402
from miniagent.workspace import Workspace  # noqa: E402

failures = []


def check(name, got, want):
    if got == want:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name}\n  got : {got!r}\n  want: {want!r}")


class FakeProvider:
    """Canned chat responses; never actually called in this file."""

    def __init__(self, responses=()):
        self.responses = list(responses)

    def chat(self, messages, tools=None):  # pragma: no cover - unused here
        return {"choices": [{"message": self.responses.pop(0)}]}


class FakeTools:
    """Dispatcher stub matching the generator contract of Tools.dispatch."""

    schemas = []

    def dispatch(self, call):  # pragma: no cover - unused here
        name = call.get("function", {}).get("name", "")
        outcome = {"ok": True}
        yield ToolStarted(name, {})
        yield ToolCompleted(name, "ok", "", outcome)
        return "{}"


class ScriptedInput:
    """Answers a queue of scripted input() responses; raises when exhausted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"scripted input exhausted; prompt: {prompt!r}")
        return self.answers.pop(0)


def with_input(fake, fn):
    """Run fn() with builtins.input replaced by *fake*; restore afterwards."""
    original = builtins.input
    builtins.input = fake
    try:
        return fn()
    finally:
        builtins.input = original


def capture(fn):
    """Run fn(), returning (result, printed-stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = fn()
    return result, buffer.getvalue()


def make_context(root: Path, state_dir: Path):
    """Build a real Context wired to temp dirs and stub agent/provider."""
    config = Config(state_dir=str(state_dir), data={
        "provider": "provA",
        "model": "m1",
        "providers": {
            "provA": {
                "base_url": "https://a.example/v1",
                "chat_path": "chat/completions",
                "models": ["m1", "m2"],
                "model": "m1",
                "auth_enabled": False,
            },
            "provB": {
                "base_url": "https://b.example/v1",
                "chat_path": "chat/completions",
                "models": ["mB1"],
                "model": "mB1",
                "auth_enabled": False,
            },
        },
    })
    provider = Provider(config, api_key="")
    permissions = Permissions(str(state_dir), str(root))
    workspace = Workspace(root)
    session_logger = SessionLogger(root, state_dir=str(state_dir),
                                    provider=config.provider, model=config.model)
    agent = Agent(FakeProvider(), FakeTools(), "sysprompt", recorder=session_logger)
    checkpoints = Checkpoints(root, state_dir=str(state_dir))
    renderer_name = "console"
    ctx = Context(
        config=config,
        provider=provider,
        agent=agent,
        permissions=permissions,
        workspace=workspace,
        root=root,
        session_logger=session_logger,
        renderer=get_renderer(renderer_name),
        renderer_name=renderer_name,
        checkpoints=checkpoints,
    )
    return ctx


tmp_roots = []  # cleaned in finally

try:
    root = Path(tempfile.mkdtemp(prefix="ma_cmd_ws_"))
    state = Path(tempfile.mkdtemp(prefix="ma_cmd_state_"))
    tmp_roots += [root, state]
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub").mkdir()

    ctx = make_context(root, state)

    # --- every registered command dispatches without raising ----------------

    specs = all_specs()
    check("registry is not empty", len(specs) > 0, True)
    check("expected canonical command names present",
          sorted(s.name for s in specs),
          sorted(["help", "quit", "reset", "resume", "undo", "checkpoints",
                  "config", "key", "model", "effort", "verbose", "perms",
                  "clear-perms", "files", "workspace", "context"]))

    for spec in specs:
        def run_it(spec=spec):
            return spec(ctx, "")
        try:
            with_input(ScriptedInput(["", "", "", ""]), lambda: capture(run_it))
        except Exception as exc:  # pragma: no cover - failure path
            failures.append(f"dispatching :{spec.name} raised {exc!r}")
            print(f"FAIL: dispatching :{spec.name} did not raise\n  got : {exc!r}")
        else:
            print(f"PASS: dispatching :{spec.name} did not raise")

    # --- aliases resolve to the same CommandSpec -----------------------------

    check("':q' resolves to the :quit spec", get_spec("q") is get_spec("quit"), True)
    check("':exit' resolves to the :quit spec",
          get_spec("exit") is get_spec("quit"), True)
    check("':models' resolves to the :model spec",
          get_spec("models") is get_spec("model"), True)
    check("unregistered word resolves to nothing", get_spec("nope"), None)

    # --- :help is generated and mentions every registered command -----------

    help_text = render_help()
    for spec in specs:
        check(f":help mentions :{spec.name}", f":{spec.name}" in help_text, True)
        for alias in spec.aliases:
            check(f":help mentions alias :{alias}", f":{alias}" in help_text, True)

    # --- the banner list matches the registry --------------------------------

    banner = render_command_list()
    for spec in specs:
        check(f"banner mentions :{spec.name}", f":{spec.name}" in banner, True)

    # --- duplicate registration raises ---------------------------------------

    try:
        @command("quit")
        def _dup(ctx, arg):  # pragma: no cover - never runs
            """Duplicate."""
        check("registering a duplicate name raises", "no error", "ValueError")
    except ValueError:
        check("registering a duplicate name raises", "ValueError", "ValueError")

    try:
        @command("totally-new-name", aliases=("q",))
        def _dup_alias(ctx, arg):  # pragma: no cover - never runs
            """Duplicate alias."""
        check("registering a duplicate alias raises", "no error", "ValueError")
    except ValueError:
        check("registering a duplicate alias raises", "ValueError", "ValueError")

    # --- bare vs. argument forms ----------------------------------------------

    # :config / :config set KEY VALUE
    _, out = capture(lambda: get_spec("config")(ctx, ""))
    check("bare :config prints configuration", "provider" in out, True)
    _, out = capture(lambda: get_spec("config")(ctx, "set model m2"))
    check("':config set' updates the live config", ctx.config.model, "m2")
    check("':config set' confirms the update", "model updated and saved" in out, True)

    # :key / :key set VALUE
    _, out = capture(lambda: get_spec("key")(ctx, ""))
    check("bare :key reports stored state", "API key stored for provider" in out, True)
    _, out = capture(lambda: get_spec("key")(ctx, "set s3cr3t"))
    check("':key set' reports success",
          ("keychain" in out or "environment" in out), True)

    # :effort / :effort high
    _, out = capture(lambda: get_spec("effort")(ctx, ""))
    check("bare :effort prints the current level", "Effort:" in out, True)
    _, out = capture(lambda: get_spec("effort")(ctx, "high"))
    check("':effort high' sets the level", ctx.provider.effort, "high")
    _, out = capture(lambda: get_spec("effort")(ctx, "bogus"))
    check("an invalid effort level is rejected", "Valid levels" in out, True)

    # :model NUMBER selects directly; bare :model prompts. Entries are listed
    # in configuration order: provA/m1, provA/m2, provB/mB1 — entry 3 is the
    # only one that switches provider (provA/m2 is already selected above).
    _, out = capture(lambda: get_spec("model")(ctx, "3"))
    check("':model 3' selects the third entry directly",
          (ctx.config.provider, ctx.config.model), ("provB", "mB1"))
    check("provider switch rebuilt the live Provider object",
          ctx.provider.base_url, "https://b.example/v1")
    check("provider switch updated the agent's provider too",
          ctx.agent.provider is ctx.provider, True)

    scripted_blank = ScriptedInput([""])
    result, out = with_input(
        scripted_blank, lambda: capture(lambda: get_spec("model")(ctx, "")))
    check("bare ':model' with a blank answer cancels",
          (ctx.config.provider, ctx.config.model), ("provB", "mB1"))
    check("bare ':model' actually prompted for a number (input() was called)",
          any("Select model number" in p for p in scripted_blank.prompts), True)

    # :verbose on|off; bare prints current
    _, out = capture(lambda: get_spec("verbose")(ctx, ""))
    check("bare :verbose reports the current renderer", "Renderer:" in out, True)
    capture(lambda: get_spec("verbose")(ctx, "on"))
    check("':verbose on' switches to the verbose renderer",
          ctx.renderer_name, "verbose")
    capture(lambda: get_spec("verbose")(ctx, "off"))
    check("':verbose off' switches back to console", ctx.renderer_name, "console")
    _, out = capture(lambda: get_spec("verbose")(ctx, "sideways"))
    check("an invalid :verbose argument is rejected", "Usage" in out, True)

    # --- :files / :workspace ---------------------------------------------------

    _, out = capture(lambda: get_spec("files")(ctx, ""))
    check(":files lists the workspace's top-level entries",
          ("a.txt" in out, "sub" in out), (True, True))

    _, out = capture(lambda: get_spec("workspace")(ctx, ""))
    check(":workspace prints the project root", str(root) in out, True)

    # --- :perms / :clear-perms --------------------------------------------------

    _, out = capture(lambda: get_spec("perms")(ctx, ""))
    check(":perms prints JSON permission state", out.strip().startswith("{"), True)

    _, out = with_input(
        ScriptedInput(["n"]),
        lambda: capture(lambda: get_spec("clear-perms")(ctx, "")))
    check("':clear-perms' answered 'n' does not confirm clearing",
          "Permissions cleared" in out, False)

    # --- :reset ------------------------------------------------------------------

    ctx.agent.messages.append({"role": "user", "content": "something"})
    _, out = capture(lambda: get_spec("reset")(ctx, ""))
    check(":reset reports success", "Conversation reset" in out, True)
    check(":reset actually reset the agent's history",
          len(ctx.agent.messages), 1)

    # --- :resume with nothing recorded -------------------------------------------

    _, out = capture(lambda: get_spec("resume")(ctx, ""))
    check(":resume with no sessions reports that plainly",
          "No recorded sessions" in out, True)

    # --- :undo / :checkpoints with nothing recorded -------------------------------

    _, out = capture(lambda: get_spec("undo")(ctx, ""))
    check(":undo with nothing to undo says so", "Nothing to undo" in out, True)
    _, out = capture(lambda: get_spec("checkpoints")(ctx, ""))
    check(":checkpoints with none recorded says so",
          "No checkpoints recorded" in out, True)

    # --- :context (JEB.md inspector) — just needs to run cleanly -----------------

    _, out = capture(lambda: get_spec("context")(ctx, ""))
    check(":context prints something about JEB.md discovery",
          "Local" in out, True)

    # --- :quit signals the sentinel, not SystemExit -------------------------------

    result, out = capture(lambda: get_spec("quit")(ctx, ""))
    check(":quit returns the QUIT sentinel", result is QUIT, True)
    check(":quit reports stopping", "MiniAgent stopped" in out, True)

    # --- loop dispatch: unknown command is reported, not sent to the agent -------

    scripted = ScriptedInput([":bogus-command", ":quit"])
    _, out = with_input(scripted, lambda: capture(lambda: console_loop_mod.console_loop(ctx)))
    check("an unknown command is reported", "Unknown command: :bogus-command" in out, True)
    check("the loop still exits cleanly on :quit", "MiniAgent stopped" in out, True)

finally:
    for root_dir in tmp_roots:
        shutil.rmtree(root_dir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    raise AssertionError(f"{len(failures)} command self-test check(s) failed")
print("All checks passed.")
