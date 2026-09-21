"""High-level construction — the console front end's entry point.

``run(project_root)`` is the single public entry point. Importing this
module does not start a session; all construction happens inside ``run()``.
Everything ``run()`` used to do inline — keychain access, JEB.md discovery,
the console loop and its command handlers — now lives in its own module
(:mod:`.keys`, :mod:`.jebmd`, :mod:`.console`); this file is left with just
the wiring that turns a project root into a running session (§5.6).
"""

from __future__ import annotations

from pathlib import Path

from .agent import Agent, build_system_prompt
from .checkpoints import Checkpoints
from .config import Config, default_state_dir
from .console import console_loop
from .context import Context
from .jebmd import load_jeb_md_context
from .keys import _ensure_api_key, _load_api_key
from .permissions import Permissions
from .provider import Provider
from .runner import Runner
from .sessions import SessionLogger
from .tools import Tools
from .ui import get_renderer
from .vision import Vision
from .workspace import Workspace, WorkspaceError


def run(project_root):
    """Launch the interactive MiniAgent console for *project_root*."""
    root = Path(project_root).resolve()
    if not root.exists():
        print(f"Project root does not exist: {root}")
        return
    if not root.is_dir():
        print(f"Project root is not a directory: {root}")
        return

    state_dir = default_state_dir()
    config = Config.load(state_dir)

    # First-run setup: import legacy config or prompt interactively.
    if not config.exists():
        config.first_run_setup(legacy_dir=str(root))

    api_key = _ensure_api_key(config)

    # Checkpoints (§5.2): snapshots live outside the workspace (like
    # permissions.json and the session logs) so the agent's own file tools
    # cannot reach or tamper with its own undo history.
    checkpoints = Checkpoints(root, state_dir=state_dir)

    try:
        workspace = Workspace(root, checkpoints=checkpoints)
    except WorkspaceError as exc:
        print(exc)
        return

    permissions = Permissions(state_dir, str(root))
    runner = Runner(workspace)
    # Vision collaborator for the ask_image tool: it resolves its own
    # provider/model from config.json's "vision_model" key and loads that
    # provider's API key through the same keychain scheme as the main chat.
    vision = Vision(config, load_key=lambda name: _load_api_key(config, name))
    tools = Tools(workspace, permissions, runner, vision=vision)
    provider = Provider(config, api_key)
    session_logger = SessionLogger(
        root,
        state_dir=state_dir,
        provider=config.provider,
        model=config.model,
    )
    jeb_context = load_jeb_md_context(root)
    agent = Agent(provider, tools, build_system_prompt(str(root), jeb_context),
                  recorder=session_logger)

    # The front end is a renderer over the agent's event stream, chosen
    # here and swappable at runtime with :verbose.  The engine itself prints
    # nothing, so this is the only place that decides how a turn looks.
    renderer_name = "console"
    renderer = get_renderer(renderer_name)

    ctx = Context(
        config=config,
        provider=provider,
        agent=agent,
        permissions=permissions,
        workspace=workspace,
        root=root,
        session_logger=session_logger,
        renderer=renderer,
        renderer_name=renderer_name,
        checkpoints=checkpoints,
    )
    console_loop(ctx)
