# spacetrash
CLI Tools / scripts that augment us when working on Void Projects

>[!warning] Use at your own risk. This is firmly space trash.

Everything in this repo is completely vibe coded and likely never looked at. We use the tooling in here to augment our work and choose willingly to ignore the quality for the sake of quick tooling.

## Installation

Requires [uv](https://docs.astral.sh/uv/).

```powershell
uv tool install -e .
```

## Usage

```
stcli [OPTIONS] COMMAND [ARGS]...
```

`st` is installed as a shorthand alias for `stcli`, so `st wsl fix` is equivalent
to `stcli wsl fix`.

Blank `stcli` command prints help.

### Ask

Type a question and get the command back, nothing else:

```powershell
st "give me the command to set ubuntu as my default distribution for wsl"
# wsl --set-default Ubuntu
```

Anything `stcli` does not recognise as a command is treated as a question, so
`st "..."` and `st ask "..."` are the same thing.

stcli looks for an agent harness already installed on the machine and drives it
in one-shot mode: Claude Code, Codex, Gemini CLI, GitHub Copilot CLI, Cursor
Agent, Amazon Q, opencode, Goose, Crush, or `llm`. On Windows, if none is on the
native PATH, it looks inside the default WSL distro too.

The answer goes to stdout on its own and everything else goes to stderr, so it
pipes and copies cleanly:

```powershell
st "flush the dns cache" | clip
```

```powershell
st ask --list                          # what harnesses are installed
st ask --set-default claude            # pin one (or set STCLI_HARNESS)
st ask -H gemini "resize a qcow2 image"
st ask -c "undo my last commit but keep the changes"   # also copy it
st ask -r "show the biggest files here"                # run it, after confirming
st ask -e "what does chmod 755 mean"                   # allow a short explanation
st ask --dry-run "..."                 # show the harness command stcli would run
```

Preferences live in `config.json` under the stcli app dir
(`%APPDATA%\stcli` on Windows). A `commands` entry overrides how a harness is
invoked, for the day one of these CLIs changes its flags:

```json
{
  "harness": "claude",
  "commands": { "claude": ["claude", "-p", "{prompt}"] }
}
```

### Jumpgates

A jumpgate is a named warp point to a directory. Place one, then warp to that
folder just by typing its name, without going through `stcli`. The command is
`stcli jumpgate`, aliased as `jg`.

```powershell
st jg add repos "C:\Users\Blake\source\repos"   # open or recalibrate a jumpgate
st jg list                                      # list (or just: st jg)
st jg remove repos                              # collapse a jumpgate
```

The jumpgate is written in the form the current environment needs:

- **PowerShell** (Windows): a function in your `$PROFILE`, e.g. `function repos { Set-Location 'C:\Users\Blake\source\repos' }`.
- **WSL / bash / zsh**: an alias in `~/.bashrc` or `~/.zshrc`, e.g. `alias repos='cd "/mnt/c/Users/Blake/source/repos"'`.

Windows paths passed inside WSL are auto-converted to `/mnt/...` (and the
reverse for PowerShell). After opening a jumpgate, reload the profile
(`. $PROFILE` or `source ~/.bashrc`) or open a new shell to bring it online,
then just type the name (e.g. `repos`) to warp there.

`list` also **discovers** functions and aliases already present in your
profile, so on a fresh install you can see jumpgates you wrote by hand. They
are tagged `(discovered)` versus stcli's own `(stcli)` entries. `remove` works
on either, and `add` adopts a hand-written definition into a managed jumpgate
when the names match.