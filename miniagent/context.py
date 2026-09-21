"""The live wiring object handed to every console command (§5.6).

Named ``Context`` rather than the design doc's "Session object": this
codebase already gives "session" a specific, different meaning
(``miniagent.sessions`` — a recorded conversation log resumed with
``:resume``), and reusing the word for the live in-memory wiring here would
be a genuine reading hazard in a codebase meant to be self-edited. ``Context``
says what it is: the collaborators one console command needs to see or
mutate.

Before this existed, ``Provider`` snapshotted config at construction, so
switching provider/model meant ``_apply_selection`` had to *return* a new
``Provider`` that every command handler threaded back through a local
variable in the console loop — mutable state passed by return value, through
every call site that could switch providers. A command handler now mutates
``ctx.provider`` (or ``ctx.renderer``) in place instead.
"""

from __future__ import annotations

from pathlib import Path


class Context:
    """The live session wiring: config, provider, agent and friends.

    One instance is built in ``run()`` and lives for the whole console
    session. Command handlers take it as their first argument and mutate its
    attributes directly (``ctx.provider = new_provider``) rather than
    returning replacements.
    """

    def __init__(self, *, config, provider, agent, permissions, workspace,
                 root: Path, session_logger, renderer, renderer_name: str,
                 checkpoints=None):
        self.config = config
        self.provider = provider
        self.agent = agent
        self.permissions = permissions
        self.workspace = workspace
        self.root = root
        self.session_logger = session_logger
        self.renderer = renderer
        self.renderer_name = renderer_name
        self.checkpoints = checkpoints
