"""Claude Code, driven in print mode."""

from __future__ import annotations

import json

from stcli.harnesses.base import Harness, HarnessOption, Location, Prompt
from stcli.harnesses.registry import register

# Denied outright: an answer to "what is the command for X" never needs to
# change a file, and a denied tool cannot stall on a permission prompt.
_DENIED_TOOLS = "Edit,Write,NotebookEdit"


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@register
class ClaudeCode(Harness):
    name = "claude"
    label = "Claude Code"
    executable = "claude"
    install_hint = "npm install -g @anthropic-ai/claude-code"

    model_flag = "--model"
    # Looking up a command is a small question, so ask the small model.
    default_model = "haiku"

    options = (
        # Low effort by default: these are lookups, not investigations.
        HarnessOption(
            "effort", "Reasoning effort: low, medium, high, xhigh, max", default="low"
        ),
        HarnessOption("fast", "Ask for fast mode. Claude only grants it on Opus models"),
    )

    def option_args(self) -> list[str]:
        args: list[str] = []

        effort = self.option("effort")
        if effort:
            args += ["--effort", str(effort)]

        # Fast mode has no flag of its own: it is a setting Claude reads at
        # startup, and it only takes on the models Claude allows it for.
        if _truthy(self.option("fast")):
            args += ["--settings", json.dumps({"fastMode": True})]

        return args

    def command(self, location: Location, prompt: Prompt) -> list[str]:
        # --disallowed-tools takes a variable number of values, so it must be
        # followed by another flag rather than by the question itself.
        return [
            location.executable,
            "--print",
            "--output-format", "text",
            "--no-session-persistence",
            *self.tuning_args(),
            "--disallowed-tools", _DENIED_TOOLS,
            "--append-system-prompt", prompt.system,
            prompt.question,
        ]
