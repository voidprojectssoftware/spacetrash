import click

from stcli.commands import wsl


@click.group(name="stcli")
def app():
    """spacetrash CLI."""


app.add_command(wsl.wsl)


if __name__ == "__main__":
    app()
