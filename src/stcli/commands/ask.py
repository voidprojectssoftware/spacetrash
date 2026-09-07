"""Ask an installed agent harness a question, straight from the terminal.

The point is speed: ``st "how do I set my default wsl distro"`` should print
the command and nothing else, so it can be copied, piped, or run. This module
is only the terminal surface; which agent answers, and how, lives behind the
`Harness` abstraction in `stcli.harnesses`.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys

import click
from rich.console import Console

from stcli import cache as answer_cache
from stcli import config as config_store
from stcli import harnesses
from stcli.harnesses import (
    AskRequest,
    Harness,
    HarnessError,
    Installed,
    Location,
    Settings,
)

# Chrome goes to stderr so stdout stays a clean, pipeable answer.
err = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Choosing a harness
# --------------------------------------------------------------------------- #
def _no_harness_message(config: dict) -> str:
    lines = ["No agent harness is installed on this system.", "stcli can use:"]
    for harness in harnesses.known(config):
        hint = f" ({harness.install_hint})" if harness.install_hint else ""
        lines.append(f"  {harness.label}{hint}")
    lines.append(
        f"Install one, or point stcli at your own agent with a 'commands' "
        f"entry in {config_store.config_path()}."
    )
    return "\n".join(lines)


def _pick(config: dict, requested: str | None) -> Installed:
    """Resolve the harness to use, or explain why there is not one."""
    found = harnesses.installed(config)

    if not requested:
        if not found:
            raise click.ClickException(_no_harness_message(config))
        return found[0]

    for entry in found:
        if entry.harness.name == requested:
            return entry

    harness = harnesses.get(requested, config)
    if harness is None:
        raise click.UsageError(
            f"Unknown harness '{requested}'. stcli knows: "
            f"{', '.join(harnesses.registered_names())}."
        )
    hint = f" Install it with: {harness.install_hint}" if harness.install_hint else ""
    raise click.ClickException(
        f"{harness.label} is not installed (looked for '{harness.executable}' "
        f"on PATH).{hint}"
    )


def _print_list(config: dict) -> None:
    found = harnesses.installed(config)
    if not found:
        err.print(f"[yellow]{_no_harness_message(config)}[/yellow]")
        return

    preferred = harnesses.preferred_name(config)
    err.print("[cyan]Agent harnesses detected:[/cyan]")
    for index, entry in enumerate(found):
        harness = entry.harness
        tag = " [green](default)[/green]" if index == 0 else ""
        if harness.name == preferred:
            tag += " [dim](preferred)[/dim]"
        err.print(f"  [green]{harness.name}[/green]  {harness.label}{tag}")
        err.print(f"      [dim]{entry.location.describe()}[/dim]")
        err.print(
            f"      [dim]model: {harness.model or 'harness default'}"
            f" | timeout: {harness.timeout}s | cache: {harness.cache_days}d[/dim]"
        )
        for option in harness.options:
            value = harness.option(option.name)
            shown = f"= {value}" if value not in (None, "") else "unset"
            if harness.option_is_default(option.name) and value not in (None, ""):
                shown += " (default)"
            err.print(f"      [dim]{option.name} {shown}  ({option.help})[/dim]")

    err.print(
        "[dim]Settings live in "
        f"{config_store.config_path()} under 'harnesses'.[/dim]"
    )


def _cli_settings(model: str | None, options: tuple[str, ...], timeout: int | None) -> Settings:
    """Turn the per-invocation flags into a settings layer."""
    parsed: dict = {}
    for item in options:
        key, sep, value = item.partition("=")
        if not sep:
            raise click.UsageError(f"--option takes key=value, got '{item}'.")
        parsed[key.strip()] = value.strip()
    return Settings(model=model or "", timeout=timeout, options=parsed)


def _apply_settings(entry: Installed, overrides: Settings) -> Installed:
    """Layer the flags over the config, and flag any option nobody knows."""
    harness = entry.harness.with_settings(entry.harness.settings.merge(overrides))
    for name in harnesses.unknown_options(harness):
        err.print(
            f"[yellow]{harness.label} has no option '{name}'; ignoring it.[/yellow]"
        )
    return Installed(harness, entry.location)


# --------------------------------------------------------------------------- #
# Doing something with the answer
# --------------------------------------------------------------------------- #
def echo_answer(text: str) -> None:
    """Print the answer, whatever characters it turns out to contain.

    Agents reply in UTF-8, but a redirected stdout on Windows defaults to the
    ANSI code page, which cannot encode most of it.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    click.echo(text)


def _powershell_copy(text: str) -> list[str] | None:
    """Set the Windows clipboard through PowerShell, encoding and all.

    The text travels as base64 in the command itself, so nothing depends on
    the console code page. Preferred over `clip`, which needs UTF-16LE with a
    byte order mark and then leaves that mark in what you paste.
    """
    exe = (
        shutil.which("pwsh")
        or shutil.which("powershell")
        or shutil.which("powershell.exe")  # reachable from WSL
    )
    if not exe:
        return None
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return [
        exe, "-NoProfile", "-Command",
        f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}')))",
    ]


def _clipboard_attempts(text: str) -> list[tuple[list[str], bytes]]:
    """Every way to reach a clipboard from here, best first."""
    utf8 = text.encode("utf-8", "replace")
    attempts: list[tuple[list[str], bytes]] = []

    def add(candidate: list[str], payload: bytes) -> None:
        exe = shutil.which(candidate[0])
        if exe:
            attempts.append(([exe] + candidate[1:], payload))

    powershell = _powershell_copy(text)

    if sys.platform == "win32":
        if powershell:
            attempts.append((powershell, b""))
        add(["clip"], b"\xff\xfe" + text.encode("utf-16-le", "replace"))
        return attempts

    if sys.platform == "darwin":
        add(["pbcopy"], utf8)
        return attempts

    add(["wl-copy"], utf8)
    add(["xclip", "-selection", "clipboard"], utf8)
    add(["xsel", "-ib"], utf8)
    if powershell:  # WSL, reaching the Windows clipboard
        attempts.append((powershell, b""))
    return attempts


def copy_to_clipboard(text: str) -> bool:
    for argv, payload in _clipboard_attempts(text):
        try:
            subprocess.run(
                argv,
                input=payload,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-Command", command]
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-lc", command]


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


def _show_dry_run(harness: Harness, location: Location, request: AskRequest) -> None:
    argv = harness.argv(location, harness.build_prompt(request))
    err.print(f"[cyan]{harness.label}[/cyan] [dim]{location.describe()}[/dim]")
    echo_answer(" ".join(shlex.quote(part) for part in argv))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@click.command("ask")
@click.argument("question", nargs=-1)
@click.option("-H", "--harness", "harness_name", metavar="NAME",
              help="Use a specific harness instead of the default.")
@click.option("-m", "--model", metavar="NAME",
              help="Model for this question, in the harness's own naming.")
@click.option("-o", "--option", "options", metavar="KEY=VALUE", multiple=True,
              help="Set one of the harness's own options. Repeatable.")
@click.option("-l", "--list", "list_only", is_flag=True,
              help="List the agent harnesses installed, with their settings.")
@click.option("-e", "--explain", is_flag=True,
              help="Allow a short explanation instead of a bare command.")
@click.option("-c", "--copy", "copy_flag", is_flag=True,
              help="Copy the answer to the clipboard.")
@click.option("-r", "--run", "run_flag", is_flag=True,
              help="Run the returned command after confirming.")
@click.option("--no-cache", "no_cache", is_flag=True,
              help="Ask again instead of reusing a stored answer.")
@click.option("--clear-cache", "clear_cache", is_flag=True,
              help="Forget every stored answer, then exit.")
@click.option("--raw", is_flag=True, help="Print the harness output verbatim.")
@click.option("--timeout", type=int, default=None, metavar="SECONDS",
              help="Give up when the harness takes longer than this.  [default: 180]")
@click.option("--set-default", "set_default", metavar="NAME",
              help="Remember NAME as the harness to use, then exit.")
@click.option("--dry-run", is_flag=True,
              help="Show the command stcli would run against the harness.")
def ask(
    question: tuple[str, ...],
    harness_name: str | None,
    model: str | None,
    options: tuple[str, ...],
    list_only: bool,
    explain: bool,
    copy_flag: bool,
    run_flag: bool,
    no_cache: bool,
    clear_cache: bool,
    raw: bool,
    timeout: int | None,
    set_default: str | None,
    dry_run: bool,
) -> None:
    """Ask an installed agent harness QUESTION and print the answer.

    stcli finds an agent CLI already installed on this machine, runs it in its
    one-shot mode, and prints just the command you asked for. The answer goes
    to stdout on its own, so it pipes and copies cleanly.

    Anything stcli does not recognise as a command is treated as a question,
    so `st "..."` is the same as `st ask "..."`.

    \b
    Examples:
      st "set ubuntu as my default wsl distro"
      st ask "undo my last git commit but keep the changes" --copy
      st ask -e "what does chmod 755 mean"
      st ask -m opus -o effort=high "why is my docker build cache missing"
      st ask --list
    """
    config = config_store.load()

    if clear_cache:
        removed = answer_cache.clear()
        err.print(f"[green]Forgot {removed} stored answer(s).[/green]")
        return

    if set_default:
        if harnesses.get(set_default, config) is None:
            raise click.UsageError(
                f"Unknown harness '{set_default}'. stcli knows: "
                f"{', '.join(harnesses.registered_names())}."
            )
        config["harness"] = set_default
        path = config_store.save(config)
        err.print(f"[green]Default harness set to '{set_default}'.[/green]")
        err.print(f"[dim]Saved to {path}[/dim]")
        return

    if list_only:
        _print_list(config)
        return

    text = " ".join(question).strip()
    if not text:
        click.echo(click.get_current_context().get_help())
        return

    entry = _apply_settings(_pick(config, harness_name), _cli_settings(model, options, timeout))
    request = AskRequest.build(text, explain=explain)

    if dry_run:
        _show_dry_run(entry.harness, entry.location, request)
        return

    def run() -> harnesses.Answer:
        return entry.harness.ask(
            request, location=entry.location, use_cache=not no_cache
        )

    try:
        if err.is_terminal:
            with err.status(f"[dim]asking {entry.harness.label}...[/dim]", spinner="dots"):
                answer = run()
        else:
            answer = run()
    except HarnessError as exc:
        raise click.ClickException(f"{entry.harness.label}: {exc}")

    text_out = answer.raw if raw else answer.text
    if not text_out.strip():
        detail = harnesses.clean_output(answer.error).strip()
        message = f"{entry.harness.label} returned nothing"
        if detail:
            message += f":\n{detail}"
        raise click.ClickException(message)

    echo_answer(text_out)
    if answer.cached:
        err.print(f"[dim]{entry.harness.label} - remembered, --no-cache to ask again[/dim]")
    else:
        err.print(f"[dim]{entry.harness.label} - {answer.elapsed:.1f}s[/dim]")

    if answer.returncode != 0:
        err.print(f"[yellow]Harness exited with code {answer.returncode}.[/yellow]")

    if copy_flag:
        if copy_to_clipboard(text_out):
            err.print("[dim]Copied to clipboard.[/dim]")
        else:
            err.print("[yellow]No clipboard tool available; nothing copied.[/yellow]")

    if run_flag:
        _maybe_run(text_out)
