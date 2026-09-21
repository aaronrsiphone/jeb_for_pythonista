"""The interactive console: the command registry and the input loop.

``app.py`` needs only :func:`console_loop`; everything else here is
implementation detail (the ``@command`` registry, the handlers themselves,
and the ``:resume`` pager) that a caller reaches through this package only
when it needs to, e.g. in tests.
"""

from __future__ import annotations

from .loop import console_loop
from .registry import QUIT, all_specs, command, get_spec, render_command_list, render_help
from .resume import resume_command as resume_pager

__all__ = [
    "console_loop",
    "command",
    "get_spec",
    "all_specs",
    "render_help",
    "render_command_list",
    "QUIT",
    "resume_pager",
]
