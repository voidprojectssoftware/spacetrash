"""Ask a locally installed agent harness a question, straight from the terminal.

The point is speed: ``st "how do I set my default wsl distro"`` should print the
command and nothing else, so it can be copied, piped, or run. Any agent CLI with
a one-shot print mode can act as the backend.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console

# Chrome goes to stderr so stdout stays a clean, pipeable answer.
err = Console(stderr=True)

ENV_HARNESS = "STCLI_HARNESS"


# --------------------------------------------------------------------------- #
# Known harnesses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Harness:
    """A CLI agent that can answer a single prompt and exit.

    ``args`` is the argument vector after the executable; the literal token
    ``{prompt}`` is replaced with the built prompt.
    """

    name: str
    label: str
    executable: str
    args: tuple[str, ...]
    noisy: bool = False  # prints session metadata around the answer


HARNESSES: tuple[Harness, ...] = (
    Harness("claude", "Claude Code", "claude", ("-p", "{prompt}")),
    Harness("codex", "OpenAI Codex CLI", "codex", ("exec", "{prompt}"), noisy=True),
    Harness("gemini", "Gemini CLI", "gemini", ("-p", "{prompt}")),
    Harness("copilot", "GitHub Copilot CLI", "copilot", ("-p", "{prompt}"), noisy=True),
    Harness("cursor", "Cursor Agent", "cursor-agent", ("-p", "{prompt}")),
    Harness("q", "Amazon Q Developer", "q", ("chat", "--no-interactive", "{prompt}"), noisy=True),
    Harness("opencode", "opencode", "opencode", ("run", "{prompt}"), noisy=True),
    Harness("goose", "Goose", "goose", ("run", "-t", "{prompt}"), noisy=True),
    Harness("crush", "Crush", "crush", ("run", "-q", "{prompt}"), noisy=True),
    Harness("llm", "llm", "llm", ("{prompt}",)),
)

_BY_NAME = {h.name: h for h in HARNESSES}


@dataclass(frozen=True)
class Detected:
    """A harness found on this machine, plus how to reach it."""

    harness: Harness
    location: str
    via_wsl: bool = False
    argv_override: tuple[str, ...] = field(default=())

    @property
    def where(self) -> str:
        return f"wsl: {self.location}" if self.via_wsl else self.location


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config_path() -> Path:
    return Path(click.get_app_dir("stcli")) / "config.json"


def _load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(config: dict) -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _preferred_name(config: dict) -> str | None:
    return os.environ.get(ENV_HARNESS) or config.get("harness")


def _command_override(config: dict, name: str) -> tuple[str, ...]:
    """A user-supplied argv template for a harness, from the config ``commands``."""
    commands = config.get("commands")
    if not isinstance(commands, dict):
        return ()
    argv = commands.get(name)
    if isinstance(argv, str):
        argv = shlex.split(argv)
    if not isinstance(argv, list) or not argv:
        return ()
    return tuple(str(part) for part in argv)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def _wsl_exe() -> str | None:
    return shutil.which("wsl") if sys.platform == "win32" else None


def _wsl_lookup(executable: str) -> str | None:
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


def detect(config: dict | None = None, include_wsl: bool = True) -> list[Detected]:
    """Find installed harnesses, in preference order.

    The native PATH is searched first. Only when nothing is installed natively
    do we look inside WSL, so the common case stays fast.
    """
    config = config if config is not None else _load_config()
    found: list[Detected] = []

    for harness in HARNESSES:
        override = _command_override(config, harness.name)
        executable = override[0] if override else harness.executable
        path = shutil.which(executable)
        if path:
            found.append(Detected(harness, path, argv_override=override))

    if found or not include_wsl:
        return _ordered(found, config)

    for harness in HARNESSES:
        override = _command_override(config, harness.name)
        executable = override[0] if override else harness.executable
        path = _wsl_lookup(executable)
        if path:
            found.append(Detected(harness, path, via_wsl=True, argv_override=override))

    return _ordered(found, config)


def _ordered(found: list[Detected], config: dict) -> list[Detected]:
    """Move the preferred harness, when installed, to the front."""
    preferred = _preferred_name(config)
    if not preferred:
        return found
    return sorted(found, key=lambda d: d.harness.name != preferred)


def _pick(found: list[Detected], requested: str | None) -> Detected:
    if requested:
        for detected in found:
            if detected.harness.name == requested:
                return detected
        if requested not in _BY_NAME:
            raise click.UsageError(
                f"Unknown harness '{requested}'. Known harnesses: "
                f"{', '.join(sorted(_BY_NAME))}."
            )
        raise click.ClickException(
            f"Harness '{requested}' is not installed (looked for "
            f"'{_BY_NAME[requested].executable}' on PATH)."
        )
    if not found:
        raise click.ClickException(_no_harness_message())
    return found[0]


def _no_harness_message() -> str:
    names = ", ".join(h.executable for h in HARNESSES)
    return (
        "No agent harness found on this system.\n"
        f"Looked for: {names}.\n"
        "Install one, or point stcli at your own command with a 'commands' "
        f"entry in {_config_path()}."
    )


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def _environment_hint() -> str:
    if sys.platform == "win32":
        return "Windows, PowerShell"
    if sys.platform == "darwin":
        return "macOS, zsh"
    shell = os.path.basename(os.environ.get("SHELL", "sh"))
    if "microsoft" in platform.uname().release.lower():
        return f"WSL2 on Windows (Linux), {shell}"
    return f"{platform.system()}, {shell}"


_COMMAND_PROMPT = """\
You are a terminal copilot. The user wants a command they can run right now.

Environment: {env}
Working directory: {cwd}

Reply with the command only, following these rules exactly:
- No prose, no explanation, no preamble, no sign-off.
- No markdown, no code fences, no backticks.
- No leading shell prompt characters such as $, > or PS>.
- One command per line; when several steps are needed, list them in order.
- Use <angle-bracket placeholders> for values only the user can supply.
- Do not read, write or change any files while answering.
- If no command can answer this, reply with a single line starting with "# ".

Request: {question}
"""

_EXPLAIN_PROMPT = """\
You are a terminal copilot answering someone who is reading a terminal.

Environment: {env}
Working directory: {cwd}

Rules:
- Plain text only: no markdown, no code fences, no asterisks.
- Six short lines at most.
- Put any command on its own line, exactly as it should be typed.
- Do not read, write or change any files while answering.

Request: {question}
"""


def build_prompt(question: str, explain: bool) -> str:
    template = _EXPLAIN_PROMPT if explain else _COMMAND_PROMPT
    return template.format(env=_environment_hint(), cwd=os.getcwd(), question=question.strip())


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #
def build_argv(detected: Detected, prompt: str) -> list[str]:
    template = detected.argv_override or ((detected.harness.executable,) + detected.harness.args)
    parts = [part.replace("{prompt}", prompt) for part in template]

    if detected.via_wsl:
        parts[0] = detected.location
        inner = " ".join(shlex.quote(part) for part in parts)
        return [_wsl_exe() or "wsl", "-e", "bash", "-lc", inner]

    if not detected.argv_override:
        parts[0] = detected.location
    return parts


def run_harness(argv: list[str], timeout: int) -> tuple[str, str, int]:
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
        raise click.ClickException(f"The harness did not answer within {timeout}s.")
    except OSError as exc:
        raise click.ClickException(f"Could not start the harness: {exc}")
    return result.stdout or "", result.stderr or "", result.returncode


# --------------------------------------------------------------------------- #
# Output cleaning
# --------------------------------------------------------------------------- #
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*\r?\n(.*?)```", re.DOTALL)
_PROMPT_RE = re.compile(r"^(?:\$|PS[^>]*>)\s+")
_NOISE_RE = re.compile(
    r"^(?:"
    r"-{3,}|_{3,}|={3,}"
    r"|\[[^\]]{4,}\]\s*(?:codex|thinking|tokens used.*)?"
    r"|(?:workdir|model|provider|approval|sandbox|session id|tokens used"
    r"|reasoning \w+|user instructions)\s*:.*"
    r"|OpenAI Codex .*"
    r"|Thinking\.{0,3}|Working\.{0,3}|Loading\.{0,3}"
    r")$",
    re.IGNORECASE,
)


def clean_answer(text: str, noisy: bool) -> str:
    """Reduce harness output to something that can be pasted into a shell."""
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")

    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)

    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if noisy and _NOISE_RE.match(line.strip()):
            continue
        lines.append(_PROMPT_RE.sub("", line))

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Clipboard and execution
# --------------------------------------------------------------------------- #
def _clipboard_argv() -> list[str] | None:
    if sys.platform == "win32":
        exe = shutil.which("clip")
        return [exe] if exe else None
    if sys.platform == "darwin":
        exe = shutil.which("pbcopy")
        return [exe] if exe else None
    candidates = (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "-ib"],
        ["clip.exe"],  # WSL, falls through to the Windows clipboard
    )
    for candidate in candidates:
        exe = shutil.which(candidate[0])
        if exe:
            return [exe] + candidate[1:]
    return None


def copy_to_clipboard(text: str) -> bool:
    argv = _clipboard_argv()
    if not argv:
        return False
    try:
        subprocess.run(argv, input=text, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-Command", command]
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-lc", command]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_list(found: list[Detected], config: dict) -> None:
    if not found:
        err.print(f"[yellow]{_no_harness_message()}[/yellow]")
        return

    preferred = _preferred_name(config)
    err.print("[cyan]Agent harnesses detected:[/cyan]")
    width = max(len(d.harness.name) for d in found)
    for index, detected in enumerate(found):
        tag = " [green](default)[/green]" if index == 0 else ""
        if detected.harness.name == preferred:
            tag += " [dim](preferred)[/dim]"
        err.print(
            f"  [green]{detected.harness.name.ljust(width)}[/green]  "
            f"{detected.harness.label} [dim]{detected.where}[/dim]{tag}"
        )
    err.print(f"[dim]Set a default with: st ask --set-default {found[0].harness.name}[/dim]")


@click.command("ask")
@click.argument("question", nargs=-1)
@click.option("-H", "--harness", "harness_name", metavar="NAME",
              help="Use a specific harness instead of the default.")
@click.option("-l", "--list", "list_only", is_flag=True,
              help="List the agent harnesses installed on this system.")
@click.option("-e", "--explain", is_flag=True,
              help="Allow a short explanation instead of a bare command.")
@click.option("-c", "--copy", "copy_flag", is_flag=True,
              help="Copy the answer to the clipboard.")
@click.option("-r", "--run", "run_flag", is_flag=True,
              help="Run the returned command after confirming.")
@click.option("--raw", is_flag=True, help="Print the harness output verbatim.")
@click.option("--timeout", type=int, default=180, show_default=True, metavar="SECONDS",
              help="Give up when the harness takes longer than this.")
@click.option("--set-default", "set_default", metavar="NAME",
              help="Remember NAME as the harness to use, then exit.")
@click.option("--dry-run", is_flag=True,
              help="Show the command stcli would run against the harness.")
def ask(
    question: tuple[str, ...],
    harness_name: str | None,
    list_only: bool,
    explain: bool,
    copy_flag: bool,
    run_flag: bool,
    raw: bool,
    timeout: int,
    set_default: str | None,
    dry_run: bool,
) -> None:
    """Ask an installed agent harness QUESTION and print the answer.

    stcli finds an agent CLI already installed on this machine (Claude Code,
    Codex, Gemini, Copilot and friends), runs it in its one-shot mode, and
    prints just the command you asked for. The answer goes to stdout on its
    own, so it pipes and copies cleanly.

    Anything stcli does not recognise as a command is treated as a question,
    so `st "..."` is the same as `st ask "..."`.

    \b
    Examples:
      st "set ubuntu as my default wsl distro"
      st ask "undo my last git commit but keep the changes" --copy
      st ask -e "what does chmod 755 mean"
      st ask --list
    """
    config = _load_config()

    if set_default:
        if set_default not in _BY_NAME:
            raise click.UsageError(
                f"Unknown harness '{set_default}'. Known harnesses: "
                f"{', '.join(sorted(_BY_NAME))}."
            )
        config["harness"] = set_default
        path = _save_config(config)
        err.print(f"[green]Default harness set to '{set_default}'.[/green]")
        err.print(f"[dim]Saved to {path}[/dim]")
        return

    if list_only:
        _print_list(detect(config), config)
        return

    text = " ".join(question).strip()
    if not text:
        click.echo(click.get_current_context().get_help())
        return

    detected = _pick(detect(config), harness_name)
    prompt = build_prompt(text, explain)
    argv = build_argv(detected, prompt)

    if dry_run:
        err.print(f"[cyan]{detected.harness.label}[/cyan] [dim]{detected.where}[/dim]")
        click.echo(" ".join(shlex.quote(part) for part in argv))
        return

    started = time.monotonic()
    if err.is_terminal:
        with err.status(f"[dim]asking {detected.harness.label}...[/dim]", spinner="dots"):
            stdout, stderr, returncode = run_harness(argv, timeout)
    else:
        stdout, stderr, returncode = run_harness(argv, timeout)
    elapsed = time.monotonic() - started

    answer = stdout if raw else clean_answer(stdout, detected.harness.noisy)
    if not answer.strip():
        detail = clean_answer(stderr, detected.harness.noisy).strip()
        message = f"{detected.harness.label} returned nothing"
        if detail:
            message += f":\n{detail}"
        raise click.ClickException(message)

    click.echo(answer)
    err.print(f"[dim]{detected.harness.label} - {elapsed:.1f}s[/dim]")

    if returncode != 0:
        err.print(f"[yellow]Harness exited with code {returncode}.[/yellow]")

    if copy_flag:
        if copy_to_clipboard(answer):
            err.print("[dim]Copied to clipboard.[/dim]")
        else:
            err.print("[yellow]No clipboard tool available; nothing copied.[/yellow]")

    if run_flag:
        _maybe_run(answer)


def _maybe_run(answer: str) -> None:
    command = answer.strip()
    if command.startswith("#"):
        err.print("[yellow]Nothing runnable was returned.[/yellow]")
        return

    err.print("[cyan]About to run:[/cyan]")
    for line in command.split("\n"):
        err.print(f"  [white]{line}[/white]")
    if "<" in command and ">" in command:
        err.print("[yellow]This command still contains placeholders.[/yellow]")
    if not click.confirm("Run it?", default=False, err=True):
        err.print("[dim]Not run.[/dim]")
        return

    result = subprocess.run(_shell_argv(command))
    if result.returncode != 0:
        raise SystemExit(result.returncode)
