import ctypes
import subprocess
import sys

import click
from rich.console import Console

console = Console()


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_elevated() -> int:
    # Spawns an elevated hidden PowerShell that runs the restart command,
    # waits for it, and surfaces the exit code back to the caller.
    ps_command = (
        "$p = Start-Process powershell "
        "-Verb RunAs "
        "-ArgumentList '-NoProfile -NonInteractive -Command "
        "\"try { Get-Service vmcompute -ErrorAction Stop | Restart-Service -ErrorAction Stop } "
        "catch { exit 1 }\"' "
        "-Wait -PassThru -WindowStyle Hidden; "
        "exit $p.ExitCode"
    )
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_command],
        text=True,
    )
    return result.returncode


@click.group(name="wsl", help="Commands for managing the WSL environment.")
def wsl():
    pass


@wsl.command("fix")
def wsl_fix():
    """Restart the WSL vmcompute service (Windows only)."""
    if sys.platform != "win32":
        console.print("[yellow]wsl fix must be run from Windows, not from inside WSL.[/yellow]")
        raise SystemExit(1)

    console.print("[cyan]Restarting vmcompute service...[/cyan]")

    if _is_admin():
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", "try { Get-Service vmcompute -ErrorAction Stop | Restart-Service -ErrorAction Stop } catch { exit 1 }"],
            capture_output=True,
            text=True,
        )
        returncode = result.returncode
        stderr = result.stderr.strip()
    else:
        console.print("[dim]Elevation required — UAC prompt will appear.[/dim]")
        returncode = _relaunch_elevated()
        stderr = ""

    if returncode == 0:
        console.print("[green]vmcompute restarted successfully.[/green]")
    else:
        msg = f": {stderr}" if stderr else ""
        console.print(f"[red]Failed to restart vmcompute{msg}[/red]")
        raise SystemExit(returncode)
