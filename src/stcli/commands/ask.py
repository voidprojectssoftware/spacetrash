"""Ask an installed agent harness a question, straight from the terminal.

The point is speed: ``st "how do I set my default wsl distro"`` should print
the command and nothing else, so it can be copied, piped, or run. This module
is only the terminal surface; which agent answers, and how, lives behind the
`Harness` abstraction in `stcli.harnesses`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import click
from rich.console import Console

from stcli import config as config_store
from stcli import harnesses
from stcli.harnesses import AskRequest, Harness, HarnessError, Installed, Location

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
    width = max(len(entry.harness.name) for entry in found)
    for index, entry in enumerate(found):
        tag = " [green](default)[/green]" if index == 0 else ""
        if entry.harness.name == preferred:
            tag += " [dim](preferred)[/dim]"
        err.print(
            f"  [green]{entry.harness.name.ljust(width)}[/green]  "
            f"{entry.harness.label} [dim]{entry.location.describe()}[/dim]{tag}"
        )
    err.print(f"[dim]Set a default with: st ask --set-default {found[0].harness.name}[/dim]")


# --------------------------------------------------------------------------- #
# Doing something with the answer
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
    click.echo(" ".join(shlex.quote(part) for part in argv))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
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
      st ask --list
    """
    config = config_store.load()

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

    entry = _pick(config, harness_name)
    request = AskRequest.build(text, explain=explain)

    if dry_run:
        _show_dry_run(entry.harness, entry.location, request)
        return

    try:
        if err.is_terminal:
            with err.status(f"[dim]asking {entry.harness.label}...[/dim]", spinner="dots"):
                answer = entry.harness.ask(request, timeout=timeout, location=entry.location)
        else:
            answer = entry.harness.ask(request, timeout=timeout, location=entry.location)
    except HarnessError as exc:
        raise click.ClickException(f"{entry.harness.label}: {exc}")

    text_out = answer.raw if raw else answer.text
    if not text_out.strip():
        detail = harnesses.clean_output(answer.error).strip()
        message = f"{entry.harness.label} returned nothing"
        if detail:
            message += f":\n{detail}"
        raise click.ClickException(message)

    click.echo(text_out)
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
