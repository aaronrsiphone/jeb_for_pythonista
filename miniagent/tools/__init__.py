"""Tool schemas, dispatch and permission gating.

This is a package rather than a single module because each tool is its own
file: one function is one tool, decorated with :func:`.registry.tool` and
(when gated) a matching ``@tool.preview``.  ``TOOL_SCHEMAS``,
``CAPABILITY_MAP`` and ``REQUIRED_ARGS`` below are *derived* from those
registrations, not hand-maintained — see :mod:`.registry` for how, and
:mod:`.dispatch` for the ``Tools`` class and the permission-gated dispatch
generator described there.

The imports below register every tool, in the order the model sees them in
``TOOL_SCHEMAS``.  This is what makes registration "automatic": importing
``miniagent.tools`` is enough, and adding a new tool is one new file plus one
new line here.
"""

from __future__ import annotations

from . import registry
from .list_files import list_files  # noqa: F401
from .read_file import read_file  # noqa: F401
from .search_files import search_files  # noqa: F401
from .create_file import create_file  # noqa: F401
from .edit_file import edit_file  # noqa: F401
from .multi_edit import multi_edit  # noqa: F401
from .overwrite_file import overwrite_file  # noqa: F401
from .clean_up import clean_up  # noqa: F401
from .run_python import run_python  # noqa: F401
from .ask_image import ask_image  # noqa: F401
from .knowledge import knowledge  # noqa: F401

from .dispatch import Tools  # noqa: E402  (after registration, before use)

# Tool name -> permission capability.  Read-only tools are absent here, which
# means "no permission required".  The read-only `knowledge` tool is absent
# too: none of its actions writes a file or executes code.  `ask_image` is
# gated even though it is read-only because it uploads image data — possibly
# photos or clipboard images from outside the workspace — to the vision
# provider's API.  Derived from each tool's own `@tool(capability=...)`
# declaration rather than hand-maintained here.
CAPABILITY_MAP = registry.build_capability_map()

# The full OpenAI-style tool schema list, in registration order (the order
# above), generated from each tool's function signature and metadata.
TOOL_SCHEMAS = registry.build_schemas()

# Tool name -> tuple of required argument names, read straight off each
# schema's own `required` list — see Tools.dispatch()'s required-argument
# gate in dispatch.py.
REQUIRED_ARGS = registry.build_required_args()

__all__ = ["Tools", "TOOL_SCHEMAS", "CAPABILITY_MAP", "REQUIRED_ARGS"]
