import typer
from stcli.commands import wsl

app = typer.Typer(name="stcli", no_args_is_help=True)
app.add_typer(wsl.app, name="wsl")


if __name__ == "__main__":
    app()
