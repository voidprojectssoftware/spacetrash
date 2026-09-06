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

stcli drives an agent CLI already installed on the machine in one-shot mode.
**Claude Code is the harness we ship today.** On Windows, if `claude` is not on
the native PATH, stcli looks inside the default WSL distro too.

The answer goes to stdout on its own and everything else goes to stderr, so it
pipes and copies cleanly:

```powershell
st "flush the dns cache" | clip
```

```powershell
st ask --list                          # what harnesses are installed
st ask -c "undo my last commit but keep the changes"   # also copy it
st ask -r "show the biggest files here"                # run it, after confirming
st ask -e "what does chmod 755 mean"                   # allow a short explanation
st ask --dry-run "..."                 # show the harness command stcli would run
st ask --set-default claude            # pin one (or set STCLI_HARNESS)
st ask -H claude "resize a qcow2 image"                # pick one for one question
```

#### Harnesses

Nothing in stcli talks to an agent CLI directly. Each one is a `Harness` in
`src/stcli/harnesses/`, and `ask` only ever calls `harness.ask(request)`, so
which agent answers and what flags it takes stay in one place. A harness owns
four decisions: where it is installed, how to phrase the prompt, the argv that
answers it in one shot, and how to read the reply.

To add one: drop a module in that package, subclass `CliHarness` (a single argv
template with a `{prompt}` token) or `Harness` (anything more involved, as
Claude Code does to pass its system prompt separately), decorate it with
`@register`, and import it in `harnesses/__init__.py`. Nothing outside the
package changes.

You can also point stcli at an agent it does not ship support for without
touching the code. Preferences live in `config.json` under the stcli app dir
(`%APPDATA%\stcli` on Windows). An entry under `commands` defines a new harness,
or overrides how a shipped one is invoked for the day its flags change:

```json
{
  "harness": "claude",
  "commands": {
    "claude": ["claude", "--print", "{prompt}"],
    "some-other-agent": ["some-other-agent", "run", "{prompt}"]
  }
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