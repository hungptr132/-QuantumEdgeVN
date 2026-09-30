import typer
from rich.console import Console
from rich.table import Table

from quantumedge import __version__

app = typer.Typer(
    name="qe",
    help="QuantumEdge deterministic pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Schema and seed management.", no_args_is_help=True)
app.add_typer(db_app, name="db")

console = Console()


@app.command()
def version() -> None:
    """Print the pipeline version."""
    console.print(f"quantumedge-pipeline [bold]{__version__}[/bold]")


@app.command()
def doctor() -> None:
    """Report environment and configuration health."""
    from quantumedge.config import REPO_ROOT, load_settings

    table = Table("check", "status", "detail", title="qe doctor")

    env_path = REPO_ROOT / ".env"
    table.add_row(
        "repo root",
        "[green]ok[/green]" if REPO_ROOT.exists() else "[red]missing[/red]",
        str(REPO_ROOT),
    )
    table.add_row(
        ".env",
        "[green]found[/green]" if env_path.exists() else "[yellow]absent[/yellow]",
        str(env_path),
    )

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        missing = "QE_DATABASE_URL" in str(exc)
        table.add_row("settings", "[red]incomplete[/red]", "required values not set")
        console.print(table)
        if not env_path.exists():
            console.print(
                f"\nNo .env yet. Create one:\n"
                f"  copy {REPO_ROOT / '.env.example'} {env_path}\n"
                f"then fill in the password values."
            )
        elif missing:
            console.print("\n.env exists but QE_DATABASE_URL is not set.")
        raise typer.Exit(code=1) from exc

    # Never print the DSN itself — it carries a password.
    safe_dsn = settings.database_url.split("@")[-1] if "@" in settings.database_url else "set"
    table.add_row("database_url", "[green]set[/green]", f"…@{safe_dsn}")
    table.add_row("timezone", "[green]ok[/green]", settings.timezone)

    console.print(table)


if __name__ == "__main__":
    app()
