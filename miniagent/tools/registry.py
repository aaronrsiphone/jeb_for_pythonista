"""The ``@tool`` decorator: registration and schema generation.

A tool is one function.  Decorating it with :func:`tool` registers it (in
import order, which is why :mod:`miniagent.tools` imports every tool module
explicitly and in the order the model should see them) and derives its JSON
schema from the function's signature plus the small amount of metadata the
signature cannot express on its own:

* **description** — the function's docstring, dedented and whitespace
  collapsed to a single line, so a docstring can still be written and read
  as normal prose.
* **per-parameter descriptions** — the ``params`` mapping, kept next to the
  function instead of parsed out of the docstring.
* **shapes the signature cannot express** — enums, nested object arrays, or
  a JSON Schema ``default`` the implementation does not itself need — via
  the ``overrides`` mapping, which is shallow-merged onto the type derived
  from the parameter's annotation.

``TOOL_SCHEMAS`` and ``CAPABILITY_MAP`` (see ``miniagent/tools/__init__.py``)
are therefore *derived* from this registry rather than hand-maintained: a
tool that forgets to declare a required argument cannot happen, because
"required" is read straight off the function signature.
"""

from __future__ import annotations

import inspect

# Name -> ToolSpec, in registration order.  A plain dict rather than an
# OrderedDict: insertion order is preserved since Python 3.7, and the
# ordering only matters for the (deterministic) import order in __init__.py.
_REGISTRY: dict = {}

# Sentinel annotation -> base JSON Schema fragment.  Anything not listed
# here (including an unannotated parameter) falls back to "string", which
# is always overridable via `overrides` for the rare parameter that needs
# something else.
_TYPE_SCHEMAS = {
    str: {"type": "string"},
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    dict: {"type": "object"},
    list: {"type": "array", "items": {"type": "string"}},
}


def _clean_doc(doc: str) -> str:
    """Dedent a docstring and collapse all whitespace to single spaces.

    Lets a tool's docstring be written and read as wrapped prose while the
    generated schema description matches the baseline's single-line text
    exactly.
    """
    text = inspect.cleandoc(doc or "")
    return " ".join(text.split())


def _base_schema(annotation) -> dict:
    return dict(_TYPE_SCHEMAS.get(annotation, {"type": "string"}))


class ToolSpec:
    """One registered tool: its implementation, schema and preview hook."""

    def __init__(self, func, capability=None, params=None, overrides=None):
        self.func = func
        self.name = func.__name__
        self.capability = capability
        self.description = _clean_doc(func.__doc__)
        self.preview_func = None

        params = params or {}
        overrides = overrides or {}

        sig = inspect.signature(func)
        # The first parameter is always the tool's context (conventionally
        # named `ctx`); it is never part of the model-facing schema.
        arg_params = list(sig.parameters.values())[1:]

        properties = {}
        required = []
        for p in arg_params:
            prop = _base_schema(p.annotation)
            if p.name in overrides:
                prop.update(overrides[p.name])
            if p.name in params:
                prop.setdefault("description", params[p.name])
            properties[p.name] = prop
            if p.default is inspect.Parameter.empty:
                required.append(p.name)

        self.param_names = tuple(p.name for p in arg_params)
        self.required = tuple(required)

        parameters_schema = {"type": "object", "properties": properties}
        if required:
            parameters_schema["required"] = required

        self.schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters_schema,
            },
        }

    def preview(self, func):
        """Register *func* as this tool's permission-prompt preview.

        Usage::

            @some_tool.preview
            def _(ctx, path, old_text, new_text, **kw):
                return "EDIT FILE", ctx.workspace.preview_edit(...)

        Only called when the user will actually be asked (see
        ``Tools._preview`` in ``dispatch.py``); a tool with no preview falls
        back to a generic one.
        """
        self.preview_func = func
        return func

    def __call__(self, ctx, **kwargs):
        return self.func(ctx, **kwargs)


def tool(capability=None, params=None, overrides=None):
    """Decorator: register a function as a tool and return its ``ToolSpec``.

    ``capability`` gates the tool behind that permission (``None`` means the
    tool is always allowed).  ``params`` maps parameter name -> description
    text for the generated schema.  ``overrides`` maps parameter name -> a
    dict shallow-merged onto that parameter's derived schema, for shapes an
    annotation alone cannot express (an enum, a nested object array, an
    explicit ``default``).
    """

    def decorate(func):
        spec = ToolSpec(func, capability=capability, params=params, overrides=overrides)
        if spec.name in _REGISTRY:
            raise ValueError(f"tool '{spec.name}' is already registered")
        _REGISTRY[spec.name] = spec
        return spec

    return decorate


def get_spec(name: str):
    """Return the ``ToolSpec`` for *name*, or ``None`` if not registered."""
    return _REGISTRY.get(name)


def all_specs():
    """All registered tools, in registration order."""
    return list(_REGISTRY.values())


def build_schemas() -> list:
    """The OpenAI-style tool schema list, in registration order."""
    return [spec.schema for spec in _REGISTRY.values()]


def build_capability_map() -> dict:
    """Tool name -> capability, for every gated tool."""
    return {spec.name: spec.capability for spec in _REGISTRY.values() if spec.capability}


def build_required_args() -> dict:
    """Tool name -> tuple of required argument names, for every tool that has any."""
    return {spec.name: spec.required for spec in _REGISTRY.values() if spec.required}
