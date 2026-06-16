import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console

console = Console()

# Each managed jumpgate is wrapped in a marked block so it can be updated or
# removed idempotently without disturbing the rest of the profile file.
_BEGIN = "# >>> stcli jumpgate '{name}' >>>"
_END = "# <<< stcli jumpgate '{name}' <<<"
_BLOCK_RE = re.compile(
    r"# >>> stcli jumpgate '(?P<name>.+?)' >>>\n(?P<body>.*?)\n# <<< stcli jumpgate '(?P=name)' <<<",
    re.DOTALL,
)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])/(.*)$")


@dataclass
class GateEntry:
    name: str
    detail: str
    managed: bool


# --------------------------------------------------------------------------- #
# Path translation
# --------------------------------------------------------------------------- #
def _looks_like_windows_path(path: str) -> bool:
    return bool(_WIN_PATH_RE.match(path))


def _to_wsl_path(path: str) -> str:
    """Translate a Windows path to its WSL mount equivalent."""
    if not _looks_like_windows_path(path):
        return path

    wslpath = shutil.which("wslpath")
    if wslpath:
        try:
            result = subprocess.run(
                [wslpath, "-u", path],
                capture_output=True,
                text=True,
                check=True,
            )
            converted = result.stdout.strip()
            if converted:
                return converted
        except subprocess.CalledProcessError:
            pass

    # Manual fallback: C:\Users\x -> /mnt/c/Users/x
    drive = path[0].lower()
    rest = path[2:].replace("\\", "/").lstrip("/")
    return f"/mnt/{drive}/{rest}"


def _to_windows_path(path: str) -> str:
    """Translate a WSL mount path back to a Windows path; leave others as-is."""
    match = _WSL_MOUNT_RE.match(path)
    if match:
        drive = match.group(1).upper()
        rest = match.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"
    return path


# --------------------------------------------------------------------------- #
# Environment / profile targets
# --------------------------------------------------------------------------- #
def _powershell_exe() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        raise click.ClickException("Could not find pwsh or powershell on PATH.")
    return exe


def _powershell_profile() -> Path:
    """Return the PowerShell CurrentUserCurrentHost profile path."""
    result = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
         "$PROFILE.CurrentUserCurrentHost"],
        capture_output=True,
        text=True,
    )
    profile = result.stdout.strip()
    if not profile:
        raise click.ClickException("Could not determine the PowerShell $PROFILE path.")
    return Path(profile)


def _shell_rc() -> Path:
    """Return the rc file for the active POSIX shell (zsh -> ~/.zshrc, else ~/.bashrc)."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if shell.endswith("zsh"):
        return home / ".zshrc"
    return home / ".bashrc"


def _target() -> tuple[str, Path, str]:
    """Return (kind, profile_path, reload_hint) for the current environment."""
    if sys.platform == "win32":
        return "powershell", _powershell_profile(), ". $PROFILE"
    rc = _shell_rc()
    return "posix", rc, f"source {rc}"


# --------------------------------------------------------------------------- #
# Jumpgate definition bodies
# --------------------------------------------------------------------------- #
def _ps_body(name: str, path: str) -> tuple[str, str]:
    win_path = _to_windows_path(path)
    escaped = win_path.replace("'", "''")
    return f"function {name} {{ Set-Location '{escaped}' }}", win_path


def _posix_body(name: str, path: str) -> tuple[str, str]:
    wsl_path = _to_wsl_path(path)
    # Double-quote the cd target so spaces survive, then single-quote the whole
    # alias value. Single quotes inside are closed/escaped/reopened ('\'').
    dq = (
        wsl_path.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    cmd = f'cd "{dq}"'
    sq = cmd.replace("'", "'\\''")
    return f"alias {name}='{sq}'", wsl_path


# --------------------------------------------------------------------------- #
# Managed-block read/write
# --------------------------------------------------------------------------- #
def _block(name: str, body: str) -> str:
    return f"{_BEGIN.format(name=name)}\n{body}\n{_END.format(name=name)}"


def _block_pattern(name: str) -> re.Pattern:
    return re.compile(
        re.escape(_BEGIN.format(name=name)) + r".*?" + re.escape(_END.format(name=name)),
        re.DOTALL,
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _upsert(file_path: Path, name: str, body: str) -> bool:
    """Insert or replace the managed block for ``name``. Returns True if replaced."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    content = _read(file_path)
    block = _block(name, body)

    # Use a function replacement so backslashes in the block (Windows paths)
    # are not interpreted as regex replacement escapes.
    new_content, count = _block_pattern(name).subn(lambda _m: block, content)
    if count == 0:
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        new_content += block + "\n"

    file_path.write_text(new_content, encoding="utf-8")
    return count > 0


def _delete_managed(file_path: Path, name: str) -> bool:
    """Remove the managed block for ``name``. Returns True if something was removed."""
    content = _read(file_path)
    new_content, count = _block_pattern(name).subn("", content)
    if count == 0:
        return False
    new_content = re.sub(r"\n{3,}", "\n\n", new_content)
    file_path.write_text(new_content, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Discovery (aliases already in the profile, not written by stcli)
# --------------------------------------------------------------------------- #
def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _ps_target(body: str) -> str | None:
    """Pull a Set-Location / cd target out of a PowerShell function body."""
    m = re.search(r"(?:Set-Location|cd|sl)\s+(?:-Path\s+)?(['\"])(.*?)\1", body)
    if m:
        return m.group(2)
    m = re.search(r"(?:Set-Location|cd|sl)\s+(?:-Path\s+)?(\S+)", body)
    if m:
        return _strip_quotes(m.group(1))
    return None


def _posix_detail(raw: str) -> str:
    """Turn an alias right-hand side into a readable target."""
    value = _strip_quotes(raw)
    m = re.match(r"cd\s+(.*)$", value)
    if m:
        return _strip_quotes(m.group(1).strip())
    return value


def _detail_from_body(body: str, kind: str) -> str:
    body = body.strip()
    if kind == "powershell":
        return _ps_target(body) or body
    m = re.match(r"^\s*alias\s+[A-Za-z_][\w-]*=(.*)$", body)
    return _posix_detail(m.group(1)) if m else body


def _discover_powershell(content: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for m in re.finditer(
        r"^[ \t]*function[ \t]+([A-Za-z_][\w-]*)[ \t]*\{(.*?)\}[ \t]*$",
        content,
        re.MULTILINE,
    ):
        name, body = m.group(1), m.group(2)
        found.append((name, _ps_target(body) or body.strip()))

    for m in re.finditer(
        r"^[ \t]*(?:Set-Alias|New-Alias)[ \t]+(.*)$",
        content,
        re.MULTILINE,
    ):
        name, value = _parse_set_alias(m.group(1))
        if name:
            found.append((name, value))
    return found


def _parse_set_alias(rest: str) -> tuple[str | None, str]:
    name_m = re.search(r"-Name[ \t]+(\S+)", rest)
    val_m = re.search(r"-Value[ \t]+(\S+)", rest)
    if name_m and val_m:
        return _strip_quotes(name_m.group(1)), _strip_quotes(val_m.group(1))
    parts = rest.split()
    if len(parts) >= 2:
        return _strip_quotes(parts[0]), _strip_quotes(parts[1])
    return None, ""


def _discover_posix(content: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for m in re.finditer(
        r"^[ \t]*alias[ \t]+([A-Za-z_][\w-]*)=(.*)$",
        content,
        re.MULTILINE,
    ):
        found.append((m.group(1), _posix_detail(m.group(2))))
    return found


def _collect(profile: Path, kind: str) -> tuple[list[GateEntry], list[GateEntry]]:
    """Return (managed, discovered) jumpgate entries found in the profile."""
    content = _read(profile)

    managed: list[GateEntry] = []
    for m in _BLOCK_RE.finditer(content):
        managed.append(
            GateEntry(m.group("name"), _detail_from_body(m.group("body"), kind), True)
        )
    managed_names = {e.name for e in managed}

    stripped = _BLOCK_RE.sub("", content)
    raw = _discover_powershell(stripped) if kind == "powershell" else _discover_posix(stripped)

    discovered: list[GateEntry] = []
    seen: set[str] = set()
    for name, detail in raw:
        if name in managed_names or name in seen:
            continue
        seen.add(name)
        discovered.append(GateEntry(name, detail, False))

    return managed, discovered


def _delete_discovered(profile: Path, name: str, kind: str) -> bool:
    """Remove a non-managed alias/function definition by name. Returns True if removed."""
    content = _read(profile)
    n = re.escape(name)
    if kind == "powershell":
        patterns = [
            rf"^[ \t]*function[ \t]+{n}[ \t]*\{{.*?\}}[ \t]*\r?\n?",
            rf"^[ \t]*(?:Set-Alias|New-Alias)[ \t]+(?:-Name[ \t]+)?['\"]?{n}['\"]?(?:[ \t][^\n]*)?\r?\n?",
        ]
    else:
        patterns = [rf"^[ \t]*alias[ \t]+{n}=[^\n]*\r?\n?"]

    new_content = content
    removed = False
    for pattern in patterns:
        new_content, count = re.subn(pattern, "", new_content, flags=re.MULTILINE)
        removed = removed or count > 0

    if removed:
        new_content = re.sub(r"\n{3,}", "\n\n", new_content)
        profile.write_text(new_content, encoding="utf-8")
    return removed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@click.group("jumpgate")
def jumpgate() -> None:
    """Manage jumpgates: named warp points to your directories.

    A jumpgate is written into the active shell's profile so you can warp to a
    folder just by typing its name (no stcli needed): a function in the
    PowerShell $PROFILE on Windows, or a shell alias in ~/.bashrc / ~/.zshrc
    under WSL. Aliased as `jg`.

    Use `st jg list` to see your jumpgates.
    """


@jumpgate.command("add")
@click.argument("name")
@click.argument("path")
def add_cmd(name: str, path: str) -> None:
    """Add or update a jumpgate NAME that warps to PATH.

    \b
    Example:
      st jg add repos "C:\\Users\\Blake\\source\\repos"
    """
    kind, profile, reload_hint = _target()

    if not _NAME_RE.match(name):
        raise click.UsageError(
            f"Invalid jumpgate name '{name}'. Use letters, digits, '_' or '-' and "
            "start with a letter or underscore."
        )

    if kind == "powershell":
        body, resolved = _ps_body(name, path)
    else:
        body, resolved = _posix_body(name, path)

    # Adopt any hand-written definition of the same name into a managed block.
    _delete_discovered(profile, name, kind)
    replaced = _upsert(profile, name, body)

    if not Path(resolved).exists():
        console.print(f"[yellow]Note: '{resolved}' does not currently exist.[/yellow]")

    verb = "Recalibrated" if replaced else "Opened"
    console.print(f"[green]{verb} jumpgate '{name}'[/green] -> {resolved}")
    console.print(f"[dim]Written to {profile}[/dim]")
    console.print(f"[dim]Warp online after reload: {reload_hint}  (or open a new shell)[/dim]")


@jumpgate.command("remove")
@click.argument("name")
def remove_cmd(name: str) -> None:
    """Remove a jumpgate NAME (stcli-managed or discovered)."""
    kind, profile, reload_hint = _target()

    if _delete_managed(profile, name) or _delete_discovered(profile, name, kind):
        console.print(f"[green]Collapsed jumpgate '{name}'[/green] in {profile}.")
        console.print(f"[dim]Reload with: {reload_hint}[/dim]")
    else:
        console.print(f"[yellow]No jumpgate '{name}' found in {profile}.[/yellow]")


@jumpgate.command("list")
def list_cmd() -> None:
    """List jumpgates in the current shell's profile."""
    kind, profile, _ = _target()
    managed, discovered = _collect(profile, kind)

    if not managed and not discovered:
        console.print(f"[dim]No jumpgates found in {profile}.[/dim]")
        return

    console.print(f"[cyan]Jumpgates in {profile}:[/cyan]")
    width = max((len(e.name) for e in managed + discovered), default=0)
    for e in sorted(managed, key=lambda x: x.name):
        console.print(f"  [green]{e.name.ljust(width)}[/green]  {e.detail} [dim](stcli)[/dim]")
    for e in sorted(discovered, key=lambda x: x.name):
        console.print(f"  [yellow]{e.name.ljust(width)}[/yellow]  {e.detail} [dim](discovered)[/dim]")
