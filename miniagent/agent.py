"""Agent loop: model/tool orchestration.

The agent owns the conversation, the system prompt, and the per-turn step
budget.  It depends on a provider and a tools dispatcher but does not know
about Pythonista-specific startup details.
"""

from __future__ import annotations

import json

from .provider import Provider, ProviderError


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
    "Global instructions (from ~/miniagent) appear first, followed by "
    "project-local instructions (from the workspace). Treat these as standing "
    "instructions that augment the rules above."
)


class Agent:
    """Owns the conversation and runs the model/tool loop per turn."""

    def __init__(self, provider: Provider, tools, system_prompt: str,
                 max_steps: int = DEFAULT_MAX_STEPS):
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self._system_prompt = system_prompt
        self.messages = [{"role": "system", "content": system_prompt}]

    # -- introspection ------------------------------------------------------

    def reset(self):
        """Drop the conversation back to just the system prompt."""
        self.messages = [{"role": "system", "content": self._system_prompt}]

    def last_assistant_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                text, _ = Provider.split_content(msg.get("content"))
                return text
        return ""

    # -- turn ---------------------------------------------------------------

    def turn(self, user_text: str) -> str:
        """Run one user turn, returning the assistant's final text."""
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(self.max_steps):
            try:
                response = self.provider.chat(
                    self.messages, tools=self.tools.schemas
                )
            except ProviderError as exc:
                print()
                print("Provider error:")
                print(exc)
                return ""

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
                print()
                print("Reasoning:")
                print(reasoning)

            if content:
                print()
                print("Assistant:")
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
            self.messages.append(assistant_message)

            if not tool_calls:
                return content if content else "(no response)"

            for call in tool_calls:
                call_id = call.get("id", "")
                function = call.get("function", {}) or {}
                name = function.get("name", "")

                print()
                print(f"Tool request: {name}")

                result = self.tools.dispatch(call)

                # Status line matching the original jeb.py output.
                try:
                    parsed = json.loads(result)
                except Exception:
                    parsed = {}

                if parsed.get("ok"):
                    print("Tool result: ok")
                elif parsed.get("denied"):
                    print("Tool result: denied")
                elif parsed.get("blocked"):
                    print("Tool result: blocked")
                elif parsed.get("error"):
                    print("Tool result: error")
                else:
                    print("Tool result: ok")

                self.messages.append(
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
