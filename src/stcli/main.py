import click

from stcli.commands import jumpgate, wsl


class AliasedGroup(click.Group):
    """A group whose commands can carry shorthand aliases.

    Aliases resolve like the real command but are not listed as separate
    entries: instead the help shows them inline, e.g. ``jumpgate (jg)``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}

    def add_alias(self, alias: str, target: str) -> None:
        self._aliases[alias] = target

    def get_command(self, ctx, cmd_name):
        cmd_name = self._aliases.get(cmd_name, cmd_name)
        return super().get_command(ctx, cmd_name)

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


@click.group(cls=AliasedGroup, name="stcli")
def app():
    """spacetrash CLI."""


app.add_command(wsl.wsl)
app.add_command(jumpgate.jumpgate)
app.add_alias("jg", "jumpgate")


if __name__ == "__main__":
    app()
