"""Agent loop: model/tool orchestration.

The agent owns the conversation, the system prompt, and the per-turn step
budget.  It depends on a provider and a tools dispatcher but does not know
about Pythonista-specific startup details.
"""

from __future__ import annotations

import json

from .provider import Provider, ProviderError
from .sessions import repair_messages


class AgentError(Exception):
    """Raised when the agent cannot complete a turn."""


DEFAULT_MAX_STEPS = 50


def build_system_prompt(project_root: str, extra_context: str = "") -> str:
    prompt = f"""\
You are a coding agent running inside Pythonista on iOS.

Important environment constraints:

- There is no shell.
- There is no subprocess support.
- Use only the provided tools for project operations.
- Your filesystem access is confined to the project workspace.
- Python execution occurs inside the SAME Pythonista process as the agent.
- run_python is NOT a sandbox.
- run_python captures the script's output and disables interactive input:
  a script that calls input() or reads sys.stdin fails fast with an error
  instead of prompting. Never write validation scripts that read stdin —
  inject or script any answers the code under test would prompt for.

Never add or intentionally execute process/application termination behavior:
- exit()
- quit()
- SystemExit
- sys.exit()
- os._exit()
- os.abort()
- os.kill()
- os.fork()
- os.exec*()

Some Python or native operations can destabilize Pythonista, so execute code only
when useful for validating the user's task.

Coding behavior:

- Inspect relevant files before changing them.
- Prefer edit_file for focused modifications.
- Use create_file only when creating a new path.
- Use overwrite_file only when replacing an entire existing file is appropriate.
- Run changed code when useful and permission is granted.
- Recover from tool errors instead of blindly repeating the same call.
- Keep changes focused.
- A tool result may carry a "user_comment" field: feedback the user attached
  to their permission answer (a redirect such as "n. Write it to foo/bar
  instead", or an addendum such as "y. But also check xyz"). Treat it as a
  direct instruction from the user and act on it.

The project root is:

  {project_root}

All file paths you supply are interpreted relative to that root.  You cannot
access files outside that root.

When you are finished with the user's request, reply with a normal text
message (no tool calls).
"""

    extra_context = (extra_context or "").strip()
    if extra_context:
        prompt = (
            prompt + "\n\n" + _EXTRA_CONTEXT_HEADER + "\n\n" + extra_context + "\n"
        )
    return prompt


_EXTRA_CONTEXT_HEADER = (
    "Additional context from JEB.md files is provided below.\n"
    "Global instructions come first: the Documents version "
    "(~/Documents/miniagent/JEB.md) merged with any original copy "
    "(~/miniagent/JEB.md), with conflicts resolved in favour of the "
    "Documents version. Project-local instructions (from the workspace) "
    "follow. Treat these as standing instructions that augment the rules "
    "above."
)


class Agent:
    """Owns the conversation and runs the model/tool loop per turn.

    When a *recorder* is attached (a :class:`miniagent.sessions.SessionLogger`),
    every message appended to the history is recorded to the session log as
    it happens; ``reset()`` rotates the log so a fresh conversation segment
    gets its own file.  The recorder is duck-typed and never sees the system
    prompt.
    """

    def __init__(self, provider: Provider, tools, system_prompt: str,
                 max_steps: int = DEFAULT_MAX_STEPS, recorder=None):
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self._system_prompt = system_prompt
        self.recorder = recorder
        self.messages = [{"role": "system", "content": system_prompt}]
        # Usage accounting (§1.7): last_usage is the most recent response's
        # "usage" dict (provider-shaped, may be empty); session_usage sums
        # numeric fields across the agent's lifetime for a future status
        # line. Pure data capture — no printing here.
        self.last_usage = {}
        self.session_usage = {}

    # -- introspection -----------------------------

    def reset(self):
        """Drop the conversation back to just the system prompt."""
        self.messages = [{"role": "system", "content": self._system_prompt}]
        if self.recorder is not None:
            # The recorded history of the abandoned segment stays on disk as
            # its own session file; new messages start a fresh one.
            self.recorder.rotate()

    def restore(self, history):
        """Reinstate *history* as the conversation (after the system prompt).

        System messages inside *history* are ignored — the current system
        prompt is kept, so a resumed session picks up the current JEB.md
        instructions rather than the ones from when it was recorded.  A
        caller resuming a recorded session should also attach the recorder
        to that session's file (``SessionLogger.attach``) so new messages are
        appended to it.
        """
        kept = [m for m in history
                if isinstance(m, dict) and m.get("role") != "system"]
        self.messages = [{"role": "system", "content": self._system_prompt}] + kept

    def _append(self, message: dict):
        """Append *message* to the history, recording it if a recorder is set."""
        self.messages.append(message)
        if self.recorder is not None:
            try:
                self.recorder.record(message)
            except Exception:  # logging must never break a turn
                pass

    def last_assistant_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                text, _ = Provider.split_content(msg.get("content"))
                return text
        return ""

    # -- turn --------------------------------------

    def turn(self, user_text: str) -> str:
        """Run one user turn, returning the assistant's final text."""
        # Defensive repair (§1.4): an interrupted previous turn (e.g. the
        # Pythonista stop button raising KeyboardInterrupt mid-tool-loop) can
        # leave self.messages ending in an assistant tool_calls message with
        # no matching tool results, which most providers reject with a 400.
        # Heal the tail before adding to it. repair_messages() drops all
        # system-role messages from what it's given, so only the tail after
        # the system prompt is passed through it — the system prompt itself
        # (self.messages[0]) is preserved untouched.
        if len(self.messages) > 1:
            self.messages = self.messages[:1] + repair_messages(self.messages[1:])

        self._append({"role": "user", "content": user_text})

        for _ in range(self.max_steps):
            try:
                response = self.provider.chat(
                    self.messages, tools=self.tools.schemas
                )
            except ProviderError as exc:
                print()
                print("Provider error:")
                print(exc)
                # Keep the conversation and session log balanced: every user
                # turn gets a reply, even a synthetic one (§1.6).
                text = f"[provider error: {exc}]"
                self._append({"role": "assistant", "content": text})
                return text

            usage = response.get("usage") if isinstance(response, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            self.last_usage = usage
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.session_usage[key] = self.session_usage.get(key, 0) + value

            message = Provider.extract_message(response)

            # Reasoning can arrive three ways: a dedicated reasoning_content /
            # reasoning field, typed thinking blocks inside a block-list
            # content, or both.  split_content() normalises all of them.
            reasoning_text, reasoning_blocks = Provider.split_content(
                message.get("reasoning_content") or message.get("reasoning")
            )

            # Content is normally a plain string; some providers return a
            # list of typed blocks (thinking / text / ...) instead.
            raw_content = message.get("content")
            content, content_thinking = Provider.split_content(raw_content)

            reasoning = "\n".join(
                part
                for part in (reasoning_text, reasoning_blocks, content_thinking)
                if part
            )
            if reasoning:
                # §3.1: collapse the reasoning dump to one line — it is the
                # single largest source of scroll and is usually skimmed at
                # best. Long reasoning collapses to a char count; short
                # reasoning is shown inline (newlines flattened to spaces so
                # it stays one line).
                if len(reasoning) > 200:
                    print(f"· thinking ({len(reasoning)} chars)")
                else:
                    print(f"· {' '.join(reasoning.split())}")

            if content:
                print(content)

            tool_calls = message.get("tool_calls")

            # Keep the original content shape (string or block list) in the
            # history so providers that emit blocks receive them back.
            assistant_message = {
                "role": "assistant",
                "content": raw_content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            self._append(assistant_message)

            if not tool_calls:
                return content if content else "(no response)"

            for call in tool_calls:
                call_id = call.get("id", "")
                function = call.get("function", {}) or {}
                name = function.get("name", "")

                result = self.tools.dispatch(call)

                # Status line matching the original jeb.py output, with the
                # user's permission comment (if any) shown alongside.
                try:
                    parsed = json.loads(result)
                except Exception:
                    parsed = {}

                comment = str(parsed.get("user_comment") or "").strip()
                note = f" — {comment}" if comment else ""

                if parsed.get("ok"):
                    status = "ok"
                elif parsed.get("denied"):
                    status = "denied"
                elif parsed.get("blocked"):
                    status = "blocked"
                elif parsed.get("error"):
                    status = "error"
                else:
                    status = "ok"

                # §3.1: one line per tool call (request + result combined),
                # printed once dispatch completes, instead of a separate
                # "Tool request" line before and a "Tool result" line after.
                print(f"→ {name}  {status}{note}")

                self._append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result,
                    }
                )
            # loop continues: send tool results back to the model

        print()
        print("Per-turn agent step limit reached.")
        return "[maximum agent steps reached; stopping]"
