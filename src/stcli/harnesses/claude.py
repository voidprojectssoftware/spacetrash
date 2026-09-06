"""Claude Code, driven in print mode."""

from __future__ import annotations

from stcli.harnesses.base import Harness, Location, Prompt
from stcli.harnesses.registry import register

# Denied outright: an answer to "what is the command for X" never needs to
# change a file, and a denied tool cannot stall on a permission prompt.
_DENIED_TOOLS = "Edit,Write,NotebookEdit"


@register
class ClaudeCode(Harness):
    name = "claude"
    label = "Claude Code"
    executable = "claude"
    install_hint = "npm install -g @anthropic-ai/claude-code"

    def command(self, location: Location, prompt: Prompt) -> list[str]:
        # --disallowed-tools takes a variable number of values, so it must be
        # followed by another flag rather than by the question itself.
        return [
            location.executable,
            "--print",
            "--output-format", "text",
            "--no-session-persistence",
            "--disallowed-tools", _DENIED_TOOLS,
            "--append-system-prompt", prompt.system,
            prompt.question,
        ]
