"""MiniAgent — a small coding agent for Pythonista.

Importing this package is cheap and side-effect light.  Start an agent
session with ``miniagent.run(project_root)``.
"""

from .app import run

__all__ = ["run"]
__version__ = "0.1.0"
