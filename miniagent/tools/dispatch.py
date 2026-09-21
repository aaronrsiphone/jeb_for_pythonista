"""``Tools``: the dispatch generator and permission gating.

Generic over the registry in :mod:`.registry` — this module knows nothing
about any individual tool.  Permission enforcement is centralised here so
that individual tool implementations do not each re-implement the check.

``dispatch`` is a *generator*: it yields the events of one tool call's
lifecycle (:class:`~miniagent.events.ToolStarted`, optionally
:class:`~miniagent.events.PermissionNeeded`, then
:class:`~miniagent.events.ToolCompleted`) and ``return``s the JSON string the
model receives.  Callers drive it with ``result = yield from
tools.dispatch(call)``.  Asking the user is therefore no longer something the
tool layer does behind the agent's back with ``input()``: it is an event the
driver answers by ``send()``-ing a
:class:`~miniagent.events.PermissionAnswer` back in.
"""

from __future__ import annotations

import json

from . import registry as _registry
from .. import permissions as _perm
from ..events import PermissionNeeded, ToolCompleted, ToolStarted
from ..knowledge import Knowledge, KnowledgeError
from ..runner import Runner, RunnerError
from ..vision import Vision, VisionError
from ..workspace import Workspace, WorkspaceError

_RESULT_LIMIT = 40000


def _call_kwargs(spec, args: dict) -> dict:
    """Keyword arguments for *spec*'s function, filtered from raw *args*.

    Keeps only names the function actually declares, and drops explicit
    ``None`` values so the function's own default applies — the same
    treatment the old required-argument gate already gives ``None``
    (missing and ``null`` are indistinguishable to the model, so they are
    treated the same here too).
    """
    return {k: v for k, v in args.items() if k in spec.param_names and v is not None}


class Tools:
    """Holds tool implementations and centralised permission gating."""

    def __init__(self, workspace: Workspace, permissions: _perm.Permissions, runner: Runner,
                 knowledge: Knowledge = None, vision: Vision = None):
        self.workspace = workspace
        self.permissions = permissions
        self.runner = runner
        # Collaborator behind the read-only `knowledge` tool.  Optional so
        # tests can inject one rooted at a throwaway directory; by default
        # it is created lazily on first use from the package's knowledge
        # directory, which lives outside the project workspace.
        self._kb = knowledge
        # Collaborator behind the `ask_image` tool.  Unlike knowledge it
        # cannot be self-constructed (it needs the provider config and a
        # keychain-backed key loader), so app.py passes it in; when absent,
        # ask_image reports that vision is not configured.
        self._vision = vision
        # Files created with create_file during this session (canonical
        # absolute paths).  clean_up may only move these, so it needs no
        # permission prompt: the agent can only tidy away its own debris,
        # and the move into to_delete is recoverable.
        self._session_created = set()
        self._session_cleaned = set()

    @property
    def schemas(self):
        return _registry.build_schemas()

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, tool_call: dict):
        """Run one tool call as a generator, returning a JSON string.

        Yields :class:`ToolStarted`, then — only when stored policy does not
        already decide a gated capability — :class:`PermissionNeeded`, whose
        answer the driver ``send()``s back, and finally
        :class:`ToolCompleted`.  The JSON payload for the model is the
        generator's *return* value, so callers write::

            result = yield from tools.dispatch(call)
        """
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {}
        except ValueError:
            # Arguments never parsed, so there are none to report.
            yield ToolStarted(name, {})
            outcome = {"error": "Invalid JSON arguments", "raw": raw_args}
            yield ToolCompleted(name, "error", "", outcome)
            return _result(outcome)

        yield ToolStarted(name, args)

        # Required-argument gate.  The registry already declares what each
        # tool needs (derived from its function signature), so enforce it
        # here instead of letting _execute() raise a bare KeyError that
        # reaches the model as "Unexpected error in 'read_file': 'path'" —
        # a message it cannot act on, and one indistinguishable from a
        # genuine harness fault.  Checked before the permission gate so a
        # malformed call never becomes a question for the user.
        required = _registry.build_required_args().get(name, ())
        missing = [key for key in required if args.get(key) is None]
        if missing:
            outcome = {
                "ok": False,
                "error": (
                    f"{name} requires the argument(s) "
                    + ", ".join(repr(k) for k in missing)
                ),
            }
            yield ToolCompleted(name, "error", "", outcome)
            return _result(outcome)

        cap = _registry.build_capability_map().get(name)
        user_comment = ""
        if cap is not None:
            allowed = self.permissions.decide(cap)
            if allowed is None:
                # Build the preview only when the user will actually see it:
                # _preview() reads the target file and renders a diff, work
                # that is wasted whenever stored policy already decides.
                title, details = self._preview(name, args)
                answer = yield PermissionNeeded(cap, title, details)
                allowed, user_comment = self.permissions.apply_answer(cap, answer)
            if not allowed:
                outcome = {"ok": False, "denied": True}
                if user_comment:
                    outcome["user_comment"] = user_comment
                yield ToolCompleted(name, "denied", user_comment, outcome)
                return _result(outcome)

        try:
            outcome = self._execute(name, args)
        except (WorkspaceError, RunnerError, KnowledgeError, VisionError) as exc:
            msg = str(exc)
            if msg.startswith("Blocked"):
                outcome = {"ok": False, "blocked": True, "error": msg}
            else:
                outcome = {"ok": False, "error": msg}
        except Exception as exc:  # defensive: never leak a traceback to the model
            outcome = {"ok": False, "error": f"Unexpected error in '{name}': {exc}"}

        yield ToolCompleted(name, _status_of(outcome), user_comment,
                            outcome if isinstance(outcome, dict) else {})
        return _result(_with_comment(outcome, user_comment))

    # -- preview for permission prompt --------------------------------------

    def _preview(self, name: str, args: dict) -> tuple[str, str]:
        """Return ``(title, details)`` for the permission prompt.

        Delegates to the tool's own ``@tool.preview`` function, if it
        registered one; falls back to a generic label otherwise (dead today
        — every gated tool registers a preview — but harmless if a future
        gated tool forgets to).
        """
        spec = _registry.get_spec(name)
        if spec is None or spec.preview_func is None:
            return name.upper(), f"{name}: {args}"
        kwargs = _call_kwargs(spec, args)
        try:
            return spec.preview_func(self, **kwargs)
        except WorkspaceError as exc:
            return name.upper(), f"{name}: {exc}"

    # -- implementations -----------------------------------------------------

    def _execute(self, name: str, args: dict) -> dict:
        spec = _registry.get_spec(name)
        if spec is None:
            return {"error": f"Unknown tool: {name}"}
        kwargs = _call_kwargs(spec, args)
        return spec.func(self, **kwargs)

    # -- session bookkeeping shared by create_file and clean_up --------------

    def _record_created(self, path: str):
        """Remember that *path* was created this session (for clean_up)."""
        try:
            key = str(self.workspace.resolve(path))
        except WorkspaceError:
            return
        self._session_created.add(key)
        self._session_cleaned.discard(key)

    # -- knowledge collaborator, created lazily -------------------------------

    def _knowledge_base(self) -> Knowledge:
        """The knowledge collaborator, created lazily on first use."""
        if self._kb is None:
            self._kb = Knowledge()
        return self._kb


def _status_of(outcome) -> str:
    """Classify a tool outcome as ``ok``/``denied``/``blocked``/``error``.

    Read straight off the outcome dict the tool layer already holds, in the
    same precedence the console status line used when it re-parsed the
    serialized result.  A non-dict (nothing produces one today) counts as
    ``ok``, matching the old "otherwise" branch.
    """
    if not isinstance(outcome, dict):
        return "ok"
    if outcome.get("ok"):
        return "ok"
    if outcome.get("denied"):
        return "denied"
    if outcome.get("blocked"):
        return "blocked"
    if outcome.get("error"):
        return "error"
    return "ok"


def _with_comment(outcome, comment: str):
    """Attach the user's permission-prompt comment to a tool result.

    The comment a user appended to their permission answer (e.g. the redirect
    in ``"n. Write it to foo/bar"`` or the addendum in ``"y. Also check
    xyz"``) is relayed to the model under the ``"user_comment"`` key so it
    can act on the instruction.  Results are left untouched when there is no
    comment.
    """
    if comment and isinstance(outcome, dict):
        outcome = dict(outcome)
        outcome.setdefault("user_comment", comment)
    return outcome


def _result(obj: dict) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > _RESULT_LIMIT:
        text = text[:_RESULT_LIMIT] + '...["truncated"]}'
    return text
