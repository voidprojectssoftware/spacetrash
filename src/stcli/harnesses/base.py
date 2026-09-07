"""The harness abstraction: how stcli talks to an agent CLI.

stcli never calls an agent CLI directly. Everything goes through a `Harness`,
which owns four decisions about its own agent and knows nothing else about
stcli:

- `locate`       where the agent is installed, if it is at all
- `build_prompt` how to phrase the request
- `command`      the argv that answers a prompt in one shot
- `parse`        how to get the answer back out of what the agent printed

Callers only ever use `Harness.ask`, so nothing above this layer knows which
agent answered or what flags it takes. See `claude.py` for the shipped
implementation and the package docstring for how to add another.
"""

from __future__ import annotations

import copy
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from stcli import cache


class HarnessError(RuntimeError):
    """Something went wrong reaching or running an agent CLI."""


class HarnessUnavailable(HarnessError):
    """The agent CLI is not installed on this machine."""


# --------------------------------------------------------------------------- #
# The request, the prompt, the answer
# --------------------------------------------------------------------------- #
def describe_environment() -> str:
    """A one-line description of the shell the answer has to run in."""
    if sys.platform == "win32":
        return "Windows, PowerShell"
    if sys.platform == "darwin":
        return "macOS, zsh"
    shell = os.path.basename(os.environ.get("SHELL", "sh"))
    if "microsoft" in platform.uname().release.lower():
        return f"WSL2 on Windows (Linux), {shell}"
    return f"{platform.system()}, {shell}"


@dataclass(frozen=True)
class AskRequest:
    """What the user asked, plus the context any harness needs to answer it."""

    question: str
    explain: bool = False
    environment: str = ""
    # Carried for harnesses that want it. The default prompts leave it out:
    # it rarely changes the command, and including it would stop an answer
    # cached in one directory being reused in another.
    cwd: str = ""

    @classmethod
    def build(cls, question: str, explain: bool = False) -> AskRequest:
        return cls(
            question=question.strip(),
            explain=explain,
            environment=describe_environment(),
            cwd=os.getcwd(),
        )


@dataclass(frozen=True)
class Prompt:
    """A request split the way agent CLIs take it.

    Harnesses with a system-prompt flag pass the two parts separately; the rest
    send `combined`.
    """

    system: str
    question: str

    @property
    def combined(self) -> str:
        return f"{self.system}\n\nRequest: {self.question}\n"


@dataclass(frozen=True)
class Answer:
    """What came back, cleaned and raw."""

    text: str
    raw: str
    error: str
    returncode: int
    elapsed: float
    cached: bool = False


@dataclass(frozen=True)
class Location:
    """Where a harness was found, and how to reach it from here."""

    executable: str
    via_wsl: bool = False

    def describe(self) -> str:
        return f"wsl: {self.executable}" if self.via_wsl else self.executable


@dataclass(frozen=True)
class Installed:
    """A harness paired with the location it was found at."""

    harness: Harness
    location: Location


DEFAULT_TIMEOUT = 180

# How long a stored answer stays good. Commands change slowly, and a stale
# one is always visibly marked and one flag away from being replaced.
DEFAULT_CACHE_DAYS = 14

# Settings every harness understands. Anything else in a harness's config
# block is one of its own options.
GENERIC_SETTINGS = frozenset({"model", "timeout", "args", "cache_days"})


@dataclass(frozen=True)
class HarnessOption:
    """A knob only one harness has, declared so stcli can list and check it.

    A `default` is what the harness asks for when nobody says otherwise. Set
    the option to null in config, or to nothing on the command line, to turn a
    defaulted option back off.
    """

    name: str
    help: str
    default: object = None


@dataclass(frozen=True)
class Settings:
    """How to run a harness.

    `model` and `timeout` mean the same thing whichever agent answers, so the
    core owns them. `options` and `args` are the harness's own business: the
    core carries them and never reads them.
    """

    model: str = ""
    timeout: int | None = None
    cache_days: int | None = None
    args: tuple[str, ...] = ()
    options: dict = dataclass_field(default_factory=dict)

    def option(self, name: str, default=None):
        return self.options.get(name, default)

    def merge(self, other: Settings) -> Settings:
        """Layer ``other`` on top of this, where it says anything at all."""
        return Settings(
            model=other.model or self.model,
            timeout=other.timeout if other.timeout is not None else self.timeout,
            cache_days=(
                other.cache_days if other.cache_days is not None else self.cache_days
            ),
            args=other.args or self.args,
            options={**self.options, **other.options},
        )

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        """Read a config block: known keys are generic, the rest are options."""
        if not isinstance(data, dict):
            return cls()

        args = data.get("args") or ()
        if isinstance(args, str):
            args = shlex.split(args)

        def whole(name: str) -> int | None:
            value = data.get(name)
            if isinstance(value, bool) or value is None:
                return None
            return int(value) if str(value).lstrip("-").isdigit() else None

        return cls(
            model=str(data.get("model") or ""),
            timeout=whole("timeout"),
            cache_days=whole("cache_days"),
            args=tuple(str(part) for part in args),
            options={k: v for k, v in data.items() if k not in GENERIC_SETTINGS},
        )


COMMAND_INSTRUCTIONS = """\
You are a terminal copilot. The user wants a command they can run right now.

Environment: {env}

Reply with the command only, following these rules exactly:
- No prose, no explanation, no preamble, no sign-off.
- No markdown, no code fences, no backticks.
- No leading shell prompt characters such as $, > or PS>.
- One command per line; when several steps are needed, list them in order.
- Never ask for a value you were not given. Put a <placeholder> in the command
  and let the user fill it in.
- Do not read, write or change any files while answering.
- Only when the request is not about a command at all, reply with one line
  starting with "# "."""

EXPLAIN_INSTRUCTIONS = """\
You are a terminal copilot answering someone who is reading a terminal.

Environment: {env}

Rules:
- Plain text only: no markdown, no code fences, no asterisks.
- Six short lines at most.
- Put any command on its own line, exactly as it should be typed.
- Do not read, write or change any files while answering."""


# --------------------------------------------------------------------------- #
# Reaching an executable, natively or through WSL
# --------------------------------------------------------------------------- #
def _wsl_exe() -> str | None:
    return shutil.which("wsl") if sys.platform == "win32" else None


def wsl_lookup(executable: str) -> str | None:
    """Return the path of ``executable`` inside the default WSL distro, if any."""
    wsl = _wsl_exe()
    if not wsl:
        return None
    try:
        result = subprocess.run(
            [wsl, "-e", "bash", "-lc", f"command -v {shlex.quote(executable)}"],
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if result.returncode == 0 and lines else None


def wrap_for_wsl(argv: list[str]) -> list[str]:
    """Re-express a Linux-side argv as something Windows can launch."""
    inner = " ".join(shlex.quote(part) for part in argv)
    return [_wsl_exe() or "wsl", "-e", "bash", "-lc", inner]


def run_command(argv: list[str], timeout: int) -> tuple[str, str, int]:
    """Run an agent CLI to completion with no terminal of its own."""
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", CLICOLOR="0")
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise HarnessError(f"No answer within {timeout}s.")
    except OSError as exc:
        raise HarnessError(f"Could not start the agent: {exc}")
    return result.stdout or "", result.stderr or "", result.returncode


# --------------------------------------------------------------------------- #
# Turning agent output into something pasteable
# --------------------------------------------------------------------------- #
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*\r?\n(.*?)```", re.DOTALL)
_PROMPT_RE = re.compile(r"^(?:\$|PS[^>]*>)\s+")


def clean_output(text: str, noise: tuple[re.Pattern[str], ...] = ()) -> str:
    """Strip the decoration agents wrap around an answer.

    Removes ANSI codes, a markdown code fence, copied shell prompts, and any
    session chatter a harness declares in its `noise_patterns`.
    """
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")

    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)

    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if any(pattern.match(line.strip()) for pattern in noise):
            continue
        lines.append(_PROMPT_RE.sub("", line))

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class Harness(ABC):
    """One agent CLI, described well enough for stcli to drive it."""

    name: str = ""
    label: str = ""
    executable: str = ""
    install_hint: str = ""

    # Lines this agent prints around its answer, e.g. session banners.
    noise_patterns: tuple[re.Pattern[str], ...] = ()

    # How this agent spells "use this model", and what it uses when nobody says.
    model_flag: str = ""
    default_model: str = ""

    # Knobs only this agent has. Declared so stcli can list them and warn
    # about a misspelt one, never so it can interpret them.
    options: tuple[HarnessOption, ...] = ()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def with_settings(self, settings: Settings) -> Harness:
        clone = copy.copy(self)
        clone.settings = settings
        return clone

    @property
    def model(self) -> str:
        return self.settings.model or self.default_model

    @property
    def timeout(self) -> int:
        return self.settings.timeout or DEFAULT_TIMEOUT

    @property
    def cache_days(self) -> int:
        if self.settings.cache_days is None:
            return DEFAULT_CACHE_DAYS
        return self.settings.cache_days

    def option_names(self) -> set[str]:
        return {option.name for option in self.options}

    def option(self, name: str):
        """The value in force for one of this harness's own options.

        What the settings say wins, even when what they say is nothing: that
        is how a defaulted option gets turned off.
        """
        if name in self.settings.options:
            return self.settings.options[name]
        for declared in self.options:
            if declared.name == name:
                return declared.default
        return None

    def option_is_default(self, name: str) -> bool:
        return name not in self.settings.options

    # ---- discovery ------------------------------------------------------- #
    def locate(self, allow_wsl: bool = True) -> Location | None:
        """Find the executable, on PATH first and inside WSL as a fallback."""
        path = shutil.which(self.executable)
        if path:
            return Location(path)
        if allow_wsl:
            path = wsl_lookup(self.executable)
            if path:
                return Location(path, via_wsl=True)
        return None

    # ---- prompting ------------------------------------------------------- #
    def build_prompt(self, request: AskRequest) -> Prompt:
        instructions = EXPLAIN_INSTRUCTIONS if request.explain else COMMAND_INSTRUCTIONS
        return Prompt(
            system=instructions.format(env=request.environment),
            question=request.question,
        )

    # ---- invocation ------------------------------------------------------ #
    def model_args(self) -> list[str]:
        """The model setting in this agent's own spelling."""
        if not self.model or not self.model_flag:
            return []
        return [self.model_flag, self.model]

    def option_args(self) -> list[str]:
        """This agent's own options in argv form. Nothing, unless it says so."""
        return []

    def tuning_args(self) -> list[str]:
        """Everything the settings add to a call: generic first, then its own.

        A harness drops this into its `command`. Anything in `args` is passed
        through untouched, so it must not end in a flag that swallows what
        comes after it.
        """
        return [*self.model_args(), *self.option_args(), *self.settings.args]

    @abstractmethod
    def command(self, location: Location, prompt: Prompt) -> list[str]:
        """The argv that makes this agent answer ``prompt`` and exit."""

    def argv(self, location: Location, prompt: Prompt) -> list[str]:
        """The argv actually launched, wrapped for WSL when it lives there."""
        parts = self.command(location, prompt)
        return wrap_for_wsl(parts) if location.via_wsl else parts

    # ---- reading the reply ----------------------------------------------- #
    def parse(self, stdout: str, stderr: str, returncode: int) -> str:
        return clean_output(stdout, self.noise_patterns)

    # ---- the only entry point callers need ------------------------------- #
    def cache_key(self, prompt: Prompt) -> str:
        """Everything that could change the answer, as one key.

        The question is normalised first, so asking the same thing with
        different capitals, spacing or a trailing question mark still hits.
        """
        question = " ".join(prompt.question.lower().split()).rstrip("?!. ")
        options = sorted(f"{k}={v}" for k, v in self.settings.options.items())
        return cache.key_for(
            [self.name, self.model, *options, *self.settings.args,
             prompt.system, question]
        )

    def ask(
        self,
        request: AskRequest,
        timeout: int | None = None,
        location: Location | None = None,
        use_cache: bool = True,
    ) -> Answer:
        location = location or self.locate()
        if location is None:
            raise HarnessUnavailable(f"{self.label} is not installed.")

        prompt = self.build_prompt(request)
        key = self.cache_key(prompt)

        if use_cache:
            stored = cache.load(key, self.cache_days)
            if stored is not None:
                return Answer(stored, stored, "", 0, 0.0, cached=True)

        started = time.monotonic()
        stdout, stderr, returncode = run_command(
            self.argv(location, prompt), timeout or self.timeout
        )
        elapsed = time.monotonic() - started
        text = self.parse(stdout, stderr, returncode)

        if returncode == 0 and text.strip() and self.cache_days > 0:
            cache.store(key, text, request.question)

        return Answer(
            text=text,
            raw=stdout,
            error=stderr,
            returncode=returncode,
            elapsed=elapsed,
        )


class CliHarness(Harness):
    """A harness whose whole integration is one argv template.

    Most agent CLIs need nothing more than this: give a `template` containing
    the token ``{prompt}`` and the base class does the rest. A second token,
    ``{args}``, says where the settings belong; without it they go straight
    after the executable.
    """

    template: tuple[str, ...] = ()

    def __init__(
        self,
        template: tuple[str, ...] | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(settings)
        self._template = tuple(template) if template else self.template

    def command(self, location: Location, prompt: Prompt) -> list[str]:
        tuning = self.tuning_args()

        parts: list[str] = []
        for part in self._template:
            if part == "{args}":
                parts.extend(tuning)
                tuning = []
                continue
            parts.append(part.replace("{prompt}", prompt.combined))

        if not parts:
            return parts
        parts[0] = location.executable
        return [parts[0], *tuning, *parts[1:]]


class ConfiguredHarness(CliHarness):
    """A harness declared in the user's config file rather than in code.

    This is the escape hatch for the day an agent changes its flags, and the
    way to drive an agent stcli does not ship support for yet. It takes its
    model flag from settings, since only the user knows how their agent
    spells one.
    """

    install_hint = "declared in your stcli config.json"
    options = (
        HarnessOption("model_flag", "The flag this agent takes its model on, e.g. --model"),
    )

    def __init__(
        self,
        name: str,
        template: tuple[str, ...],
        label: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(template, settings)
        self.name = name
        self.label = label or name
        self.executable = self._template[0] if self._template else name

    @property
    def model_flag(self) -> str:
        return str(self.settings.option("model_flag") or "")
