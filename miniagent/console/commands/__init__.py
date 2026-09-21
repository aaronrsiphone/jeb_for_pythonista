"""Every console command, registered with ``@command`` (§5.5), by area.

One function is one command. Split into a handful of small modules purely to
keep each file well under the ~350-line target — the design doc's example
puts them all in one ``commands.py``, but the full handler set (§5.6's
:command, :key, :model, :effort, :verbose, :perms, :clear-perms, :files,
:workspace, :context, :reset, :resume, :undo, :checkpoints, :help, :quit) ran
past that once each handler carried its own help text as a docstring.

Importing this package registers every command, in the order the submodules
are imported below (which is what ``:help`` and the startup banner list them
in) — the same convention :mod:`miniagent.tools` uses for tools. Nothing
here needs to be imported directly; :mod:`miniagent.console.loop` imports
this package for that side effect alone.
"""

from __future__ import annotations

from . import help_cmds  # noqa: F401
from . import session_cmds  # noqa: F401
from . import config_cmds  # noqa: F401
from . import perms_cmds  # noqa: F401
from . import workspace_cmds  # noqa: F401

__all__: list = []
