from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from quantumedge import __version__
from quantumedge.config import Settings

if TYPE_CHECKING:
    from alembic.config import Config

app = typer.Typer(
    name="qe",
    help="QuantumEdge deterministic pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Schema and seed management.", no_args_is_help=True)
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(
    help="Fetch external sources into immutable storage.", no_args_is_help=True
)
app.add_typer(ingest_app, name="ingest")

console = Console()


@app.command()
def version() -> None:
    """Print the pipeline version."""
    console.print(f"quantumedge-pipeline [bold]{__version__}[/bold]")


@app.command()
def doctor() -> None:
    """Report environment and configuration health."""
    from quantumedge.config import REPO_ROOT, load_settings  # noqa: PLC0415

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

    db_ok = _check_database(table, settings)

    console.print(table)
    if not db_ok:
        console.print(
            "\nDatabase unreachable. Start the local cluster:\n  .\\pipeline\\scripts\\pg.ps1 start"
        )
        raise typer.Exit(code=1)


def _check_database(table: Table, settings: Settings) -> bool:
    """Probe the database and record findings in the report table."""
    from sqlalchemy import text

    from quantumedge.db.session import get_engine

    try:
        with get_engine(settings).connect() as conn:
            server, tz = conn.execute(
                text("select current_setting('server_version'), current_setting('TimeZone')")
            ).one()
            user, db = conn.execute(text("select current_user, current_database()")).one()
            exts = conn.execute(
                text(
                    "select string_agg(extname, ', ' order by extname) from pg_extension "
                    "where extname in ('pg_trgm', 'unaccent')"
                )
            ).scalar()
            migrated = conn.execute(
                text(
                    "select exists (select 1 from information_schema.tables "
                    "where table_name = 'alembic_version')"
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        table.add_row("database", "[red]unreachable[/red]", str(exc).splitlines()[0][:70])
        return False

    table.add_row("database", "[green]connected[/green]", f"postgres {server} as {user}/{db}")
    # Storage must be UTC; the business timezone is applied only in Python.
    table.add_row(
        "server timezone",
        "[green]UTC[/green]" if tz == "UTC" else f"[yellow]{tz}[/yellow]",
        "storage is UTC by design" if tz == "UTC" else "expected UTC",
    )
    have_exts = bool(exts) and "pg_trgm" in str(exts) and "unaccent" in str(exts)
    table.add_row(
        "extensions",
        "[green]ok[/green]" if have_exts else "[red]missing[/red]",
        str(exts) if exts else "none",
    )
    table.add_row(
        "migrations",
        "[green]applied[/green]" if migrated else "[yellow]none yet[/yellow]",
        "alembic_version present" if migrated else "run: qe db upgrade",
    )
    return True


def _alembic_config() -> "Config":
    from alembic.config import Config  # noqa: PLC0415

    from quantumedge.config import REPO_ROOT  # noqa: PLC0415

    cfg = Config(str(REPO_ROOT / "pipeline" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "pipeline" / "migrations"))
    return cfg


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations up to REVISION (default: head)."""
    from alembic import command  # noqa: PLC0415

    command.upgrade(_alembic_config(), revision)
    console.print(f"[green]migrated to {revision}[/green]")


@db_app.command("downgrade")
def db_downgrade(revision: str) -> None:
    """Revert migrations down to REVISION. Destructive — name the target explicitly."""
    from alembic import command  # noqa: PLC0415

    command.downgrade(_alembic_config(), revision)
    console.print(f"[yellow]reverted to {revision}[/yellow]")


@db_app.command("current")
def db_current() -> None:
    """Show the applied revision."""
    from alembic import command  # noqa: PLC0415

    command.current(_alembic_config(), verbose=False)


@db_app.command("seed")
def db_seed() -> None:
    """Load reference data and rule specs from YAML. Idempotent — safe to re-run."""
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.seed import rule_spec_version, seed_all  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415

    settings = load_settings()
    with session_scope() as session:
        counts = seed_all(session, settings)

    table = Table("table", "rows", title="qe db seed")
    for name, n in counts.items():
        table.add_row(name, str(n))
    console.print(table)
    console.print(f"rule_spec_version [dim]{rule_spec_version(settings)[:16]}…[/dim]")


@ingest_app.command("prices")
def ingest_prices(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
    days: int = typer.Option(1100, help="How far back to fetch, in calendar days (~3y)."),
    start: str = typer.Option("", help="Explicit start date YYYY-MM-DD, overrides --days."),
    end: str = typer.Option("", help="Explicit end date YYYY-MM-DD. Default: today."),
) -> None:
    """Fetch daily OHLCV into raw_document. Safe to re-run: unchanged content is skipped."""
    from datetime import date, timedelta  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.ingest import vndirect  # noqa: PLC0415
    from quantumedge.ingest.base import StoreResult, store_raw  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    end_date = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start) if start else end_date - timedelta(days=days)

    with session_scope() as session:
        if tickers:
            symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()]
        else:
            symbols = list(
                session.execute(
                    sql("SELECT symbol FROM ticker WHERE is_covered ORDER BY symbol")
                ).scalars()
            )
        if not symbols:
            console.print("[yellow]no covered tickers - run: qe db seed[/yellow]")
            raise typer.Exit(code=1)

        limit = (
            session.execute(
                sql("SELECT rate_limit_per_min FROM source WHERE code = :c"),
                {"c": vndirect.SOURCE_CODE},
            ).scalar_one_or_none()
            or 20
        )

        with run_scope(
            session,
            PipelineStage.INGEST,
            "vndirect_prices",
            source_code=vndirect.SOURCE_CODE,
            as_of=end_date,
            params={"symbols": symbols, "from": str(start_date), "to": str(end_date)},
        ) as (run_id, stats):
            fetched = vndirect.fetch_prices(
                symbols, start_date, end_date, rate_limit_per_min=int(limit)
            )
            result = StoreResult()
            empty: list[str] = []
            for f in fetched:
                if f.payload is None:
                    empty.append(f.symbol)
                    continue
                store_raw(session, run_id, [f.payload], result=result)
            stats.update(result.as_stats())
            stats["symbols"] = len(symbols)
            if empty:
                stats["no_data"] = empty

            table = Table("symbol", "bars", title="qe ingest prices")
            for f in fetched:
                table.add_row(f.symbol, str(f.bars) if f.bars else f"[dim]{f.note}[/dim]")
            console.print(table)
            console.print(
                f"new [green]{result.inserted}[/green] · "
                f"unchanged [dim]{result.unchanged}[/dim] · "
                f"{start_date} to {end_date}"
            )


if __name__ == "__main__":
    app()
