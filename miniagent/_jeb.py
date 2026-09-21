"""Template launcher script for MiniAgent.

This file is not imported by the ``miniagent`` package itself — nothing in
the package references it, and the leading underscore is there specifically
to keep it from being mistaken for part of the package's public API. It is
a *template*: copy it into a project directory (naming the copy ``jeb.py``,
or anything else you like) and run it from there to start an interactive
MiniAgent console scoped to that project.

See INSTALL.md step 4 for how to copy this file into a project, and
README.md's Quick Start for the same snippet typed out by hand.
"""

from pathlib import Path
from miniagent import run

if __name__ == "__main__":
    run(project_root=Path(__file__).resolve().parent)
