"""Agent harnesses stcli can ask a question.

Claude Code is the only one wired up today. Everything above this package
talks to whatever is registered here through the `Harness` interface, so
supporting another agent is a change inside this package alone:

1. Add a module, e.g. `gemini.py`, with a `Harness` subclass. Agents that take
   the whole prompt as one argument need only a `CliHarness` template, e.g.
   `template = ("gemini", "-p", "{prompt}")`. Agents with a system-prompt flag
   subclass `Harness` and write `command()`, as `claude.py` does. Override
   `build_prompt` or `parse` only when an agent needs different wording or
   prints chatter around its answer.
2. Decorate the class with `@register`.
3. Import the module below so the decorator runs.

Users can also point stcli at an agent it does not ship support for by adding
a `commands` entry to their config; see `registry.ConfiguredHarness`.
"""

from stcli.harnesses.base import (
    Answer,
    AskRequest,
    CliHarness,
    ConfiguredHarness,
    Harness,
    HarnessError,
    HarnessUnavailable,
    Installed,
    Location,
    Prompt,
    clean_output,
    describe_environment,
)
from stcli.harnesses.registry import (
    ENV_HARNESS,
    get,
    installed,
    known,
    preferred_name,
    register,
    registered_names,
)

# Importing a harness module is what registers it.
from stcli.harnesses import claude  # noqa: E402,F401  (side effect: registration)

__all__ = [
    "Answer",
    "AskRequest",
    "CliHarness",
    "ConfiguredHarness",
    "ENV_HARNESS",
    "Harness",
    "HarnessError",
    "HarnessUnavailable",
    "Installed",
    "Location",
    "Prompt",
    "clean_output",
    "describe_environment",
    "get",
    "installed",
    "known",
    "preferred_name",
    "register",
    "registered_names",
]
