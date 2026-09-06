import click

from stcli.commands import ask, jumpgate, wsl


def _looks_like_question(args: list[str]) -> bool:
    """Decide whether unrecognised arguments should be read as a question.

    Free text is what a command name is not: several words, or one argument
    that already contains spaces or ends in a question mark. A single unknown
    word stays a typo and still gets the usual error.
    """
    if not args or args[0].startswith("-"):
        return False
    return len(args) > 1 or " " in args[0] or args[0].endswith("?")


class AliasedGroup(click.Group):
    """A group whose commands can carry shorthand aliases.

    Aliases resolve like the real command but are not listed as separate
    entries: instead the help shows them inline, e.g. ``jumpgate (jg)``.

    Arguments matching no command are handed to ``fallback`` when they read
    like free text, so ``st "how do I ..."`` works without a subcommand.
    """

    def __init__(self, *args, fallback: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}
        self.fallback = fallback

    def add_alias(self, alias: str, target: str) -> None:
        self._aliases[alias] = target

    def get_command(self, ctx, cmd_name):
        cmd_name = self._aliases.get(cmd_name, cmd_name)
        return super().get_command(ctx, cmd_name)

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if not self.fallback or not _looks_like_question(list(args)):
                raise
            command = self.get_command(ctx, self.fallback)
            if command is None:
                raise
            return self.fallback, command, list(args)

    def format_commands(self, ctx, formatter):
        aliases: dict[str, list[str]] = {}
        for alias, target in self._aliases.items():
            aliases.setdefault(target, []).append(alias)

        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            display = name
            if name in aliases:
                display = f"{name} ({', '.join(sorted(aliases[name]))})"
            rows.append((display, cmd))

        if not rows:
            return

        limit = formatter.width - 6 - max(len(display) for display, _ in rows)
        entries = [(display, cmd.get_short_help_str(limit)) for display, cmd in rows]
        with formatter.section("Commands"):
            formatter.write_dl(entries)


@click.group(cls=AliasedGroup, name="stcli", fallback="ask")
def app():
    """spacetrash CLI.

    \b
    Anything that is not a command is treated as a question for an agent
    harness installed on this machine:
      st "give me the command to set ubuntu as my default wsl distro"
    """


app.add_command(wsl.wsl)
app.add_command(jumpgate.jumpgate)
app.add_alias("jg", "jumpgate")
app.add_command(ask.ask)


if __name__ == "__main__":
    app()
