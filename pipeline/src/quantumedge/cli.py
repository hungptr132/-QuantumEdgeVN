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
extract_app = typer.Typer(help="Parse stored raw documents into typed rows.", no_args_is_help=True)
app.add_typer(extract_app, name="extract")
calc_app = typer.Typer(help="Deterministic metrics, benchmarks, and flags.", no_args_is_help=True)
app.add_typer(calc_app, name="calc")
fs_app = typer.Typer(help="Build, validate and export fact sheets.", no_args_is_help=True)
app.add_typer(fs_app, name="factsheet")
review_app = typer.Typer(help="The human review gate.", no_args_is_help=True)
app.add_typer(review_app, name="review")

console = Console()

# The metric codes Agent 5 computes. Listed explicitly so `qe calc liquidity` cannot
# accidentally start evaluating fundamentals that need statements it has no access to.
_LIQUIDITY_CODES = (
    "close_price",
    "price_to_par",
    "avg_volume_20s",
    "avg_turnover_20s_vnd",
    "median_turnover_20s_vnd",
    "zero_trade_sessions_3m",
    "zero_trade_ratio_3m",
    "missing_bar_sessions_3m",
    "price_range_52w_pct",
    "pct_from_52w_high",
    "bid_ask_spread_pct",
    "sessions_to_exit",
)


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


@extract_app.command("prices")
def extract_prices_cmd(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: every stored one."),
) -> None:
    """Parse stored price payloads into price_bar, then rebuild the trading calendar."""
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.extract.calendar import rebuild_calendar  # noqa: PLC0415
    from quantumedge.extract.prices import extract_prices  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None

    with (
        session_scope() as session,
        run_scope(
            session, PipelineStage.EXTRACT, "prices", params={"symbols": symbols or "all"}
        ) as (run_id, stats),
    ):
        result = extract_prices(session, run_id, symbols=symbols)
        stats.update(result.as_stats())

        # The calendar depends on the bars just written, so it always follows them.
        cal = rebuild_calendar(session)
        stats["calendar_days"] = cal.rows_written

        table = Table("step", "result", title="qe extract prices")
        table.add_row("documents read", str(result.documents_read))
        table.add_row("bars written", str(result.rows_written))
        table.add_row("skipped", str(result.skipped))
        table.add_row("trading calendar", cal.notes[0] if cal.notes else "-")
        console.print(table)
        for note in result.notes:
            console.print(f"  [yellow]{note}[/yellow]")


@calc_app.command("liquidity")
def calc_liquidity(
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
) -> None:
    """Agent 5: liquidity risk metrics. Pure code, framed as exit risk, never a signal."""
    from datetime import date  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc import liquidity  # noqa: PLC0415  (registers the metrics)
    from quantumedge.calc.registry import load_contexts, run_metrics  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None

    with session_scope() as session:
        if as_of:
            as_of_date = date.fromisoformat(as_of)
        else:
            # Default to the most recent session the calendar knows about, never today():
            # an explicit date is what keeps the result reproducible.
            latest = session.execute(
                sql(
                    "SELECT max(trade_date) FROM market_calendar "
                    "WHERE exchange_code = 'HOSE' AND is_trading_day"
                )
            ).scalar_one_or_none()
            if latest is None:
                console.print("[yellow]no trading calendar - run: qe extract prices[/yellow]")
                raise typer.Exit(code=1)
            as_of_date = latest

        contexts = load_contexts(session, as_of_date, symbols=symbols)
        if not contexts:
            console.print("[yellow]no covered tickers - run: qe db seed[/yellow]")
            raise typer.Exit(code=1)

        with run_scope(
            session,
            PipelineStage.CALC,
            "liquidity",
            as_of=as_of_date,
            params={"symbols": symbols or "all", "calc_version": liquidity.CALC_VERSION},
        ) as (run_id, stats):
            outcome = run_metrics(
                session, run_id, liquidity.CALC_VERSION, contexts, codes=list(_LIQUIDITY_CODES)
            )
            stats.update(outcome.as_stats())

        rows = session.execute(
            sql(
                """
                SELECT t.symbol,
                       max(mv.value) FILTER (WHERE mv.metric_code = 'close_price') AS close,
                       max(mv.value) FILTER (WHERE mv.metric_code = 'price_to_par') AS to_par,
                       max(mv.value) FILTER (WHERE mv.metric_code = 'zero_trade_ratio_3m')
                           AS zero_ratio,
                       max(mv.value) FILTER (WHERE mv.metric_code = 'median_turnover_20s_vnd')
                           AS turnover,
                       max(mv.value) FILTER (WHERE mv.metric_code = 'sessions_to_exit')
                           AS exit_100m
                FROM metric_value mv JOIN ticker t ON t.id = mv.ticker_id
                WHERE mv.as_of_date = :as_of GROUP BY t.symbol ORDER BY zero_ratio DESC NULLS LAST
                """
            ),
            {"as_of": as_of_date},
        ).mappings()

        table = Table(
            "symbol",
            "close",
            "x par",
            "zero-trade",
            "median turnover",
            "exit 100mn",
            title=f"Agent 5 liquidity - as of {as_of_date}",
        )
        for r in rows:
            table.add_row(
                r["symbol"],
                f"{r['close']:,.0f}" if r["close"] is not None else "-",
                f"{r['to_par']:.2f}" if r["to_par"] is not None else "-",
                f"{r['zero_ratio'] * 100:.1f}%" if r["zero_ratio"] is not None else "-",
                f"{r['turnover'] / 1_000_000:,.1f}mn" if r["turnover"] is not None else "-",
                f"{r['exit_100m']:.1f} sessions" if r["exit_100m"] is not None else "khong cong bo",
            )
        console.print(table)
        console.print(
            f"available [green]{outcome.available}[/green] · "
            f"unavailable [dim]{outcome.unavailable}[/dim]"
        )


@calc_app.command("flags")
def calc_flags(
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
) -> None:
    """Evaluate every active flag rule. Missing inputs report unknown, never inactive."""
    from datetime import date  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc.flags import evaluate_flags  # noqa: PLC0415
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.seed import rule_spec_version  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None
    spec_version = rule_spec_version(load_settings())

    with session_scope() as session:
        if as_of:
            as_of_date = date.fromisoformat(as_of)
        else:
            latest = session.execute(
                sql("SELECT max(as_of_date) FROM metric_value")
            ).scalar_one_or_none()
            if latest is None:
                console.print("[yellow]no metrics yet - run: qe calc liquidity[/yellow]")
                raise typer.Exit(code=1)
            as_of_date = latest

        with run_scope(
            session,
            PipelineStage.CALC,
            "flags",
            as_of=as_of_date,
            rule_spec_version=spec_version,
            params={"symbols": symbols or "all"},
        ) as (run_id, stats):
            outcome = evaluate_flags(session, run_id, as_of_date, symbols=symbols)
            stats.update(outcome.as_stats())

        rows = session.execute(
            sql(
                """
                SELECT t.symbol, fi.flag_code, fi.state, fi.severity, fi.reason,
                       jsonb_array_length(fi.evidence) AS evidence_count
                FROM flag_instance fi JOIN ticker t ON t.id = fi.ticker_id
                WHERE fi.as_of_date = :d AND fi.state = 'active'
                ORDER BY fi.severity DESC, t.symbol
                """
            ),
            {"d": as_of_date},
        ).mappings()

        table = Table(
            "symbol", "flag", "severity", "evidence", title=f"Active flags - {as_of_date}"
        )
        for r in rows:
            table.add_row(
                r["symbol"], r["flag_code"], r["severity"], f"{r['evidence_count']} item(s)"
            )
        console.print(table)

        unknown = session.execute(
            sql(
                "SELECT fi.flag_code, fi.reason, count(*) AS n FROM flag_instance fi "
                "WHERE fi.as_of_date = :d AND fi.state = 'unknown' "
                "GROUP BY fi.flag_code, fi.reason ORDER BY fi.flag_code"
            ),
            {"d": as_of_date},
        ).mappings()
        unknown_table = Table("flag", "why unknown", "tickers", title="Not assessable yet")
        for r in unknown:
            unknown_table.add_row(r["flag_code"], r["reason"] or "-", str(r["n"]))
        console.print(unknown_table)

        console.print(
            f"active [red]{outcome.active}[/red] · "
            f"inactive [green]{outcome.inactive}[/green] · "
            f"unknown [yellow]{outcome.unknown}[/yellow] · "
            f"transitions {outcome.transitions}"
        )


@fs_app.command("build")
def factsheet_build(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest metrics."),
) -> None:
    """Assemble fact sheets. An unchanged rebuild writes nothing and keeps its review stamp."""
    from datetime import date  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.factsheet.build import build_fact_sheet  # noqa: PLC0415
    from quantumedge.factsheet.store import store_fact_sheet  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    with session_scope() as session:
        if as_of:
            as_of_date = date.fromisoformat(as_of)
        else:
            latest = session.execute(
                sql("SELECT max(as_of_date) FROM metric_value")
            ).scalar_one_or_none()
            if latest is None:
                console.print("[yellow]no metrics yet - run: qe calc liquidity[/yellow]")
                raise typer.Exit(code=1)
            as_of_date = latest

        if tickers:
            symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()]
        else:
            symbols = list(
                session.execute(
                    sql("SELECT symbol FROM ticker WHERE is_covered ORDER BY symbol")
                ).scalars()
            )

        table = Table(
            "symbol", "version", "QA", "result", title=f"qe factsheet build - {as_of_date}"
        )
        with run_scope(
            session,
            PipelineStage.FACTSHEET,
            "build",
            as_of=as_of_date,
            params={"symbols": symbols},
        ) as (run_id, stats):
            created = unchanged = failed_qa = 0
            for symbol in symbols:
                sheet = build_fact_sheet(session, symbol, as_of_date, run_id)
                outcome = store_fact_sheet(session, sheet, run_id)
                created += int(outcome.created)
                unchanged += int(not outcome.created)
                failed_qa += int(not outcome.qa_passed)
                table.add_row(
                    outcome.symbol,
                    f"v{outcome.version}",
                    "[green]pass[/green]" if outcome.qa_passed else "[red]FAIL[/red]",
                    outcome.note,
                )
            stats.update({"created": created, "unchanged": unchanged, "qa_failed": failed_qa})

        console.print(table)
        console.print(f"new [green]{created}[/green] · unchanged [dim]{unchanged}[/dim]")


@fs_app.command("export-schema")
def factsheet_export_schema() -> None:
    """Write the JSON Schema the web app generates its types from."""
    import json  # noqa: PLC0415

    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.factsheet.schema import FactSheet  # noqa: PLC0415

    settings = load_settings()
    settings.contracts_dir.mkdir(parents=True, exist_ok=True)
    out = settings.contracts_dir / "factsheet.v1.schema.json"
    out.write_text(
        json.dumps(FactSheet.model_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    console.print(f"[green]wrote[/green] {out}")


@fs_app.command("show")
def factsheet_show(symbol: str) -> None:
    """Print one sheet's QA report and flag summary."""
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.db.session import session_scope  # noqa: PLC0415

    with session_scope() as session:
        row = (
            session.execute(
                sql(
                    "SELECT fs.version, fs.status, fs.qa_passed, fs.qa_report, fs.payload "
                    "FROM fact_sheet fs JOIN ticker t ON t.id = fs.ticker_id "
                    "WHERE t.symbol = :s ORDER BY fs.version DESC LIMIT 1"
                ),
                {"s": symbol.upper()},
            )
            .mappings()
            .first()
        )
        if row is None:
            console.print(f"[yellow]no fact sheet for {symbol}[/yellow]")
            raise typer.Exit(code=1)

        console.print(f"[bold]{symbol.upper()}[/bold] v{row['version']} · {row['status']}")
        qa = Table("check", "result", title="machine QA")
        for check in row["qa_report"]["checks"]:
            qa.add_row(
                check["check"],
                "[green]pass[/green]" if check["passed"] else f"[red]FAIL[/red] {check['detail']}",
            )
        console.print(qa)

        payload = row["payload"]
        flags = Table("flag", "state", "severity", title="flags")
        for f in payload["flags"]:
            colour = {"active": "red", "inactive": "green", "unknown": "yellow"}[f["state"]]
            flags.add_row(f["code"], f"[{colour}]{f['state']}[/{colour}]", f["severity"])
        console.print(flags)
        console.print(f"disclosure gaps: {len(payload['disclosure_gaps'])}")
        console.print(f"allowed_numbers: {len(payload['allowed_numbers'])} entries")


@review_app.command("list")
def review_list(status: str = typer.Option("", help="Filter by status.")) -> None:
    """Show the review queue."""
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.review.service import queue  # noqa: PLC0415

    with session_scope() as session:
        rows = queue(session, status or None)

    table = Table("symbol", "ver", "status", "QA", "flags", "reviewed by", title="review queue")
    for r in rows:
        table.add_row(
            r["symbol"],
            f"v{r['version']}",
            r["status"],
            "[green]pass[/green]" if r["qa_passed"] else "[red]FAIL[/red]",
            str(r["flags"]),
            r["reviewed_by"] or "-",
        )
    console.print(table)


@review_app.command("approve")
def review_approve(
    symbol: str,
    reviewer: str = typer.Option(..., "--as", help="Reviewer slug, e.g. richard."),
    note: str = typer.Option("", help="What you checked."),
) -> None:
    """Approve a sheet. Requires passing machine QA and a real reviewer."""
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.review.service import ReviewError, approve  # noqa: PLC0415

    try:
        with session_scope() as session:
            result = approve(session, symbol.upper(), reviewer, note=note or None)
    except ReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]approved[/green] {result.symbol} v{result.version} by {reviewer}")


@review_app.command("publish")
def review_publish(
    symbol: str,
    reviewer: str = typer.Option(..., "--as", help="Who is publishing. Required."),
) -> None:
    """Publish an approved sheet. Any previously published version is superseded."""
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.review.service import ReviewError, publish  # noqa: PLC0415

    try:
        with session_scope() as session:
            result = publish(session, symbol.upper(), reviewer)
    except ReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]published[/green] {result.symbol} v{result.version}")


@review_app.command("reject")
def review_reject(
    symbol: str,
    reviewer: str = typer.Option(..., "--as", help="Reviewer slug."),
    reason: str = typer.Option(..., help="Why. This log is how the process improves."),
) -> None:
    """Reject a sheet with a stated reason."""
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.review.service import ReviewError, reject  # noqa: PLC0415

    try:
        with session_scope() as session:
            result = reject(session, symbol.upper(), reviewer, reason)
    except ReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[yellow]rejected[/yellow] {result.symbol} v{result.version}")


if __name__ == "__main__":
    app()
