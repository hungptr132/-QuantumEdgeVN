import sys
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from quantumedge import __version__
from quantumedge.config import Settings

if TYPE_CHECKING:
    from datetime import date as date_

    from alembic.config import Config
    from sqlalchemy.orm import Session

# The Windows console defaults to cp1252, which cannot encode Vietnamese - and Vietnamese
# is this product's output language. Without this, printing a flag name like
# "Giá dưới mệnh giá" raises UnicodeEncodeError and takes the command down. Found when an
# arrow character crashed `qe run`; the same fault would have hit any Vietnamese output.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

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
    "turnover_concentration_3m",
    "max_drawdown_52w",
    "sessions_since_52w_high",
)

# Valuation, leverage and profitability come from `calc.fundamentals.FUNDAMENTAL_CODES`
# rather than a list here. This file used to carry its own copy, and it went stale the
# first time the module grew: eleven metrics were implemented, defined in metrics.yaml,
# and silently never run - which is worse than not having them, because a metric that is
# never attempted writes no row at all, so the sheet cannot even say "không công bố" for
# it. They are run rather than skipped while statements are missing for the same reason:
# each one reports the exact fact it is waiting for.


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
    warnings = _check_freshness(table, settings) if db_ok else []

    console.print(table)
    if not db_ok:
        console.print(
            "\nDatabase unreachable. Start the local cluster:\n  .\\pipeline\\scripts\\pg.ps1 start"
        )
        raise typer.Exit(code=1)

    # Staleness is a data state, not a broken environment, so it does not fail the command -
    # but it is printed with the command that fixes it, because a warning nobody can act on
    # gets ignored.
    for warning in warnings:
        console.print(f"[yellow]![/yellow] {warning}")


def _min_benchmark_sample(settings: "Settings") -> int:
    """The minimum sample a median is allowed to be computed from.

    Read from rules/flags.yaml rather than hardcoded, because it is a published-output
    threshold: raising it changes which comparisons a reader is shown.
    """
    import yaml  # noqa: PLC0415

    rules = yaml.safe_load((settings.rules_dir / "flags.yaml").read_text(encoding="utf-8"))
    return int(rules.get("min_benchmark_sample", 3))


def _resolve_as_of(session: "Session", as_of: str) -> "date_":
    """An explicit date, or the latest date metrics were computed for.

    Defaulting to the newest `metric_value` date rather than today keeps invariant 8
    intact: the clock is asked in exactly one place, and never by a calc path.
    """
    from datetime import date as _date  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    if as_of:
        return _date.fromisoformat(as_of)
    latest = session.execute(sql("SELECT max(as_of_date) FROM metric_value")).scalar_one_or_none()
    if latest is None:
        console.print("[yellow]no metrics yet - run: qe calc all[/yellow]")
        raise typer.Exit(code=1)
    assert isinstance(latest, _date)
    return latest


def _num(value: object) -> str:
    """Compact display for a benchmark statistic in a terminal table.

    VND turnover runs to twelve digits, which makes a table unreadable; ratios do not.
    Scaling by magnitude keeps both legible. This is terminal formatting only - the fact
    sheet carries its own `display` strings, built in Vietnamese conventions.
    """
    from decimal import Decimal as _D  # noqa: PLC0415

    if value is None:
        return "[dim]-[/dim]"
    d = _D(str(value))
    magnitude = abs(d)
    if magnitude >= 1_000_000_000:
        return f"{d / _D(1_000_000_000):,.2f}bn"
    if magnitude >= 1_000_000:
        return f"{d / _D(1_000_000):,.2f}m"
    if magnitude >= 1_000:
        return f"{d:,.0f}"
    return f"{d:,.4f}".rstrip("0").rstrip(".")


def _evaluate_flags_at(session: "Session", as_of: "date_") -> int:
    """Evaluate flags for one date inside an existing session. Returns the active count."""
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc.flags import evaluate_flags  # noqa: PLC0415
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.seed import rule_spec_version  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    spec_version = rule_spec_version(load_settings())
    with run_scope(
        session,
        PipelineStage.CALC,
        "flags",
        as_of=as_of,
        rule_spec_version=spec_version,
        params={"symbols": "all"},
    ) as (run_id, stats):
        outcome = evaluate_flags(session, run_id, as_of)
        stats.update(outcome.as_stats())

    return int(
        session.execute(
            sql("SELECT count(*) FROM flag_instance WHERE as_of_date = :d AND state = 'active'"),
            {"d": as_of},
        ).scalar_one()
    )


def _check_freshness(table: Table, settings: Settings) -> list[str]:
    """Render the health assessment into the doctor report.

    The measuring lives in `quantumedge.health`, which takes a session and returns facts,
    so it can be tested. This only formats - and the split exists because an untested
    staleness detector is a detector nobody should believe.
    """
    from quantumedge.db.seed import rule_spec_version  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.health import assess  # noqa: PLC0415

    try:
        with session_scope() as session:
            health = assess(session, rule_spec_version(settings))
    except Exception as exc:  # noqa: BLE001
        table.add_row("data freshness", "[yellow]unknown[/yellow]", str(exc).splitlines()[0][:70])
        return []

    table.add_row(
        "rule_spec_version", "[green]current[/green]", health.rule_spec_version[:16] + "\u2026"
    )

    if health.latest_price_date is None:
        table.add_row("latest price bar", "[yellow]none[/yellow]", "run: qe ingest prices")
    else:
        table.add_row(
            "latest price bar",
            "[green]fresh[/green]" if health.prices_are_fresh else "[yellow]stale[/yellow]",
            f"{health.latest_price_date} ({health.sessions_behind} trading sessions behind)",
        )

    if health.stale_flag_rows:
        table.add_row(
            "flags vs current rules",
            "[yellow]stale[/yellow]",
            f"{health.stale_flag_rows} evaluated under an older rule spec",
        )
    else:
        table.add_row("flags vs current rules", "[green]current[/green]", "all at the current spec")

    if health.stale_published:
        table.add_row(
            "published vs current rules",
            "[red]stale[/red]",
            f"{len(health.stale_published)} of {health.published}: "
            f"{', '.join(health.stale_published[:6])}",
        )
    else:
        table.add_row(
            "published vs current rules",
            "[green]current[/green]",
            f"{health.published} published at the current spec",
        )

    if health.unpublished:
        table.add_row(
            "coverage published",
            "[yellow]partial[/yellow]",
            f"{health.covered - health.unpublished} of {health.covered} covered tickers "
            "are published",
        )
    else:
        table.add_row(
            "coverage published", "[green]complete[/green]", f"all {health.covered} published"
        )

    return health.warnings()


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


@calc_app.command("ttm")
def calc_ttm(
    symbol: str = typer.Argument(..., help="Ticker symbol, e.g. SMC."),
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
) -> None:
    """Show the trailing-twelve-month window behind a ticker's fundamentals.

    When a fundamental reads *không công bố*, this is what says why: whether the window
    itself is short a quarter, or whether only that one line item is absent. It computes
    nothing new - the same code path the metrics take.
    """
    from quantumedge.calc.periods import describe_ttm  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415

    with session_scope() as session:
        from sqlalchemy import text as sql  # noqa: PLC0415

        as_of_date = _resolve_as_of(session, as_of)
        ticker_id = session.execute(
            sql("SELECT id FROM ticker WHERE symbol = :s"), {"s": symbol.upper()}
        ).scalar_one_or_none()
        if ticker_id is None:
            console.print(f"[red]unknown ticker {symbol.upper()}[/red]")
            raise typer.Exit(code=1)

        described = describe_ttm(session, int(ticker_id), as_of_date)

        quarters = described["quarters_available"]
        console.print(
            f"[bold]{symbol.upper()}[/bold] TTM at {as_of_date} · "
            f"quarters on file: {', '.join(quarters) if quarters else '[dim]none[/dim]'}"
        )

        if described["window"] is None:
            # The window, not the line items, is what failed. Saying which is the whole
            # point of this command: four quarters missing and one line item missing need
            # different work, and "không công bố" cannot tell them apart.
            console.print(f"[yellow]no complete window: {described['unavailable_reason']}[/yellow]")
            return

        table = Table("line item", "kind", "value", "basis / reason", title="TTM inputs")
        for row in described["items"]:
            if row["value"] is None:
                table.add_row(
                    row["fact_code"],
                    row["kind"],
                    "[dim]-[/dim]",
                    f"[yellow]{row['unavailable_reason']}[/yellow]",
                )
            else:
                table.add_row(row["fact_code"], row["kind"], row["value"], row["basis"])
        console.print(table)

        present = sum(1 for r in described["items"] if r["value"] is not None)
        console.print(
            f"{present}/{len(described['items'])} line items resolved. "
            "[dim]flow items sum four quarters; stock items take the closing balance[/dim]"
        )


@ingest_app.command("news")
def ingest_news(
    feeds: str = typer.Option("", help="Comma-separated feed codes. Default: every feed."),
) -> None:
    """Fetch RSS feeds into raw_document. Headlines only — never article bodies."""
    from datetime import date  # noqa: PLC0415

    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.ingest import rss_news  # noqa: PLC0415
    from quantumedge.ingest.base import store_raw  # noqa: PLC0415
    from quantumedge.runs import PARTIAL_REASON, run_scope  # noqa: PLC0415

    settings = load_settings()
    codes = [c.strip() for c in feeds.split(",") if c.strip()] or None

    with session_scope() as session:
        try:
            specs = rss_news.load_feed_specs(settings, codes=codes)
        except rss_news.FeedSpecError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        specs = rss_news.apply_db_rate_limits(session, specs)

        table = Table("feed", "status", "bytes", title="qe ingest news")
        stored = unchanged = failed = 0

        # One run per feed, not one for all of them: `ingest_run.source_id` names a single
        # source, and a run spanning four publishers could not answer "which one went
        # quiet" - which is the question the manifest exists for.
        for spec in specs:
            with run_scope(
                session,
                PipelineStage.INGEST,
                f"rss_news:{spec.code}",
                source_code=spec.code,
                as_of=date.today(),
                params={"feed_url": spec.feed_url},
            ) as (run_id, stats):
                result = rss_news.fetch_feeds([spec])[0]
                stats.update(rss_news.fetch_stats([result]))

                if result.payload is not None:
                    outcome = store_raw(session, run_id, [result.payload])
                    stats.update(outcome.as_stats())
                    stored += outcome.inserted
                    unchanged += outcome.unchanged
                    status = "new" if outcome.inserted else "unchanged"
                else:
                    if result.failed:
                        failed += 1
                        stats[PARTIAL_REASON] = result.note
                    status = f"[yellow]{result.note}[/yellow]"

                table.add_row(spec.code, status, str(result.byte_size))

        console.print(table)
        console.print(
            f"stored [green]{stored}[/green] · unchanged [dim]{unchanged}[/dim] · "
            f"failed [red]{failed}[/red]"
        )
        if failed:
            console.print(
                "[dim]a failed feed is a partial run, not a failed one - the others "
                "still stored. See: qe doctor[/dim]"
            )


@extract_app.command("news")
def extract_news_cmd(
    feeds: str = typer.Option("", help="Comma-separated feed codes. Default: every feed."),
) -> None:
    """raw_document -> news_item. Headline, link, publisher, timestamp; nothing else."""
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.extract.news import extract_news  # noqa: PLC0415
    from quantumedge.runs import PARTIAL_REASON, run_scope  # noqa: PLC0415

    codes = [c.strip() for c in feeds.split(",") if c.strip()] or None

    with session_scope() as session:
        with run_scope(
            session,
            PipelineStage.EXTRACT,
            "news",
            params={"feeds": codes or "all"},
        ) as (run_id, stats):
            outcome = extract_news(session, run_id, feed_codes=codes)
            stats.update(outcome.as_stats())
            if outcome.documents_failed:
                # One publisher serving an error page must not cost the other three their
                # headlines, so extraction never raises - which means the manifest is the
                # only place the failure can be recorded.
                stats[PARTIAL_REASON] = f"{outcome.documents_failed} document(s) unparseable"

        table = Table("count", "what", title="qe extract news")
        table.add_row(str(outcome.documents_read), "feed documents read")
        table.add_row(str(outcome.items_seen), "items seen")
        table.add_row(str(outcome.written), "headlines written")
        table.add_row(str(outcome.unchanged), "already stored")
        table.add_row(str(outcome.attributed), "attributed to a ticker")
        table.add_row(str(outcome.unattributed), "left unattributed")
        table.add_row(str(outcome.skipped), "skipped (no date / link / headline)")
        console.print(table)

        # Attribution is exact-match only. An unattributed headline is the expected
        # outcome for most general-news items, not a failure to report.
        console.print(
            "[dim]attribution is exact alias or whole-symbol match only; a near miss is "
            "left unattributed rather than guessed[/dim]"
        )


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


@ingest_app.command("hose-ids")
def ingest_hose_ids(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
) -> None:
    """Resolve each symbol to HOSE's internal security id.

    Every other HOSE endpoint - statements, disclosures, attachments - is keyed by that
    numeric id rather than the ticker, so this is the join key the rest of the exchange
    needs. Safe to re-run: unchanged content is skipped.
    """
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.ingest import hose  # noqa: PLC0415
    from quantumedge.ingest.base import StoreResult, store_raw  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

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
                {"c": hose.SOURCE_CODE},
            ).scalar_one_or_none()
            or 20
        )

        with run_scope(
            session,
            PipelineStage.INGEST,
            "hose_identities",
            source_code=hose.SOURCE_CODE,
            params={"symbols": symbols},
        ) as (run_id, stats):
            fetched = hose.fetch_identities(symbols, rate_limit_per_min=int(limit))
            result = StoreResult()
            unresolved: list[str] = []
            for f in fetched:
                if f.payload is None:
                    unresolved.append(f.symbol)
                    continue
                store_raw(session, run_id, [f.payload], result=result)
            stats.update(result.as_stats())
            stats["symbols"] = len(symbols)
            if unresolved:
                stats["unresolved"] = unresolved

            table = Table("symbol", "HOSE id", title="qe ingest hose-ids")
            for f in fetched:
                table.add_row(
                    f.symbol,
                    str(f.security_id) if f.security_id else f"[yellow]{f.note}[/yellow]",
                )
            console.print(table)
            console.print(
                f"new [green]{result.inserted}[/green] · unchanged [dim]{result.unchanged}[/dim]"
            )


@ingest_app.command("filings")
def ingest_filings(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: every folder."),
) -> None:
    """Store filings placed in pipeline/data/filings/<SYMBOL>/ as immutable documents.

    Nothing is fetched. A person downloads the company's own statements from its IR page
    or HOSE and drops them in; this hashes the bytes, archives them under their hash, and
    records them. Parsing happens later, against captured bytes, so a parser fix is a
    re-run rather than a re-download.

    Filenames must say which period they cover: <year>-<period>-<description>, where the
    period is Y, H1, H2 or Q1..Q4. Guessing it from the contents would mean parsing before
    storing, and from the file date would attach real figures to the wrong year.
    """
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.ingest import filings as filings_source  # noqa: PLC0415
    from quantumedge.ingest.base import StoreResult, store_raw  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    settings = load_settings()
    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None

    if not settings.filings_dir.exists():
        settings.filings_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"[yellow]created {settings.filings_dir}[/yellow]")
        console.print("Place filings in a folder per ticker, named <year>-<period>-<desc>:")
        console.print("  pipeline/data/filings/AGM/2024-Y-bao-cao-tai-chinh-kiem-toan.xlsx")
        console.print(
            "[dim]Prefer XLSX over PDF - a spreadsheet carries figures as numbers, "
            "a PDF carries a picture of them.[/dim]"
        )
        raise typer.Exit(code=0)

    outcome = filings_source.scan(settings.filings_dir, symbols=symbols)

    if not outcome.found and not outcome.rejected:
        console.print(f"[yellow]no filings under {settings.filings_dir}[/yellow]")
        raise typer.Exit(code=0)

    with (
        session_scope() as session,
        run_scope(
            session,
            PipelineStage.INGEST,
            "company_filings",
            source_code=filings_source.SOURCE_CODE,
            params={"symbols": symbols or "all"},
        ) as (run_id, stats),
    ):
        result = StoreResult()
        for found in outcome.found:
            stored = filings_source.archive(found, settings.filing_store_dir)
            found.stored_path = stored
            payload = filings_source.to_payload(found, stored)
            store_raw(session, run_id, [payload], result=result)
        stats.update(result.as_stats())
        stats.update(outcome.as_stats())

    table = Table("symbol", "period", "file", "size", title="qe ingest filings")
    for found in outcome.found:
        table.add_row(
            found.symbol,
            found.parsed.period_key,
            found.path.name,
            f"{found.byte_size / 1024:,.0f} KB",
        )
    console.print(table)
    console.print(f"new [green]{result.inserted}[/green] · unchanged [dim]{result.unchanged}[/dim]")
    for rejected in outcome.rejected:
        console.print(f"  [yellow]skipped[/yellow] {rejected}")


@ingest_app.command("statement-template")
def statement_template(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
    year: int = typer.Option(..., help="Fiscal year the filing covers, e.g. 2024."),
    period: str = typer.Option("Y", help="Y, H1, H2, or Q1..Q4."),
    force: bool = typer.Option(False, help="Overwrite a template that already exists."),
) -> None:
    """Write a YAML template per ticker, to be filled in from a filing by hand.

    For the case the spreadsheet route cannot serve: a great many Vietnamese filings are
    scanned PDFs with no text layer, and OCR on a financial statement would be guessing at
    digits. A person reads the filing and types the dozen figures that matter, citing the
    page each came from.

    The transcription is held to the same standard as a parse. Every figure needs a
    citation, and the statement must satisfy the VAS identities - so a typo in one line
    breaks the balance sheet and the whole period is refused rather than written.
    """
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.extract.mapping.vas import spec_for  # noqa: PLC0415

    settings = load_settings()
    period = period.upper()
    if period not in {"Y", "H1", "H2", "Q1", "Q2", "Q3", "Q4"}:
        console.print(f"[red]period must be Y, H1, H2 or Q1..Q4, not {period}[/red]")
        raise typer.Exit(code=1)

    spec = spec_for(settings.rules_dir)
    # The lines the identities need come first: without them the statement cannot
    # self-validate and would be accepted on trust.
    required = sorted({c for i in spec.identities for c in (*i.left, *i.right)})
    required = [c.removeprefix("_negative_") for c in required]
    others = sorted({line.fact_code for line in spec.lines.values()} - set(required))

    with session_scope() as session:
        if tickers:
            symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()]
        else:
            symbols = list(
                session.execute(
                    sql("SELECT symbol FROM ticker WHERE is_covered ORDER BY symbol")
                ).scalars()
            )
        # The Vietnamese label for each fact code, so a template reads as a statement
        # rather than as a list of identifiers.
        labels: dict[str, str] = {
            str(row[0]): str(row[1])
            for row in session.execute(sql("SELECT code, label_vi FROM financial_fact_def")).all()
        }

    written, skipped = [], []
    for symbol in symbols:
        folder = settings.filings_dir / symbol
        folder.mkdir(parents=True, exist_ok=True)
        # Written with a .template suffix so ingest cannot pick it up while it is still
        # empty. An unfilled form is not a document, and storing one would make the
        # fact sheet claim a filing source it does not have. The alternative - having
        # ingest peek inside to see whether it is filled - would mean parsing before
        # storing, which is the stage boundary this pipeline keeps.
        path = folder / f"{year}-{period}-transcribed.yaml.template"
        if path.exists() and not force:
            skipped.append(f"{symbol}: {path.name} already exists")
            continue

        lines = [
            "# Transcribed by hand from the filing. Delete any line you cannot find -",
            "# a blank is read as 'not disclosed', which is honest. A zero is a claim.",
            "#",
            "# Every figure needs a `cite` saying where it came from, or it is refused:",
            "# a number nobody can trace is worse than no number.",
            "#",
            "# CHECK THE UNIT FIRST. Vietnamese statements often print",
            "# 'Don vi tinh: trieu dong' - figures in MILLIONS. Every figure here must be",
            "# in dong, so multiply those out. This is the one error the balance checks",
            "# cannot catch: a statement in millions balances perfectly and is stored a",
            "# million times too small.",
            "#",
            "# The statement must also satisfy the VAS identities, so the lines marked",
            "# REQUIRED have to be present or there is nothing to check the rest against.",
            "",
            "source:",
            '  document: ""        # e.g. Bao cao tai chinh hop nhat da kiem toan 2024',
            '  url: ""             # where it was downloaded from, if anywhere',
            '  unit: "dong"        # the filing\'s unit. If it says trieu/nghin dong,',
            "                      # multiply the figures out and leave this as dong.",
            '  transcribed_by: ""  # your reviewer slug',
            "",
            "facts:",
        ]
        for group, codes in (
            ("REQUIRED - the identities need these", required),
            ("Optional", others),
        ):
            lines.append(f"  # ---- {group} ----")
            for code in codes:
                lines.append(f"  # {labels.get(code, code)}")
                lines.append(f"  {code}:")
                lines.append("    value:")
                lines.append('    cite: ""')
            lines.append("")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(str(path.relative_to(settings.filings_dir.parent.parent)))

    for w in written:
        console.print(f"  [green]wrote[/green] {w}")
    for s in skipped:
        console.print(f"  [yellow]skipped[/yellow] {s} [dim](--force to overwrite)[/dim]")
    if written:
        console.print()
        console.print(
            f"[bold]{len(written)} templates.[/bold] Fill in the figures, then rename "
            "each file to drop the [bold].template[/bold] suffix - that is what makes it "
            "a filing rather than a blank form."
        )
        console.print("Then: qe ingest filings && qe extract statements")


@extract_app.command("statements")
def extract_statements_cmd(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all stored."),
) -> None:
    """Parse stored filings into financial_period + financial_fact.

    A statement whose own arithmetic does not hold is refused in full rather than written:
    VAS prescribes that total assets equal total liabilities plus equity, so a parse that
    took the wrong column breaks the identity and the figures cannot be trusted.
    """
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.extract.mapping.vas import spec_for  # noqa: PLC0415
    from quantumedge.extract.statements import extract_statements  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    settings = load_settings()
    spec = spec_for(settings.rules_dir)
    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None

    with session_scope() as session:
        with run_scope(
            session,
            PipelineStage.EXTRACT,
            "statements",
            params={"symbols": symbols or "all", "extractor": "vas-v1"},
        ) as (run_id, stats):
            outcome = extract_statements(session, run_id, spec, symbols=symbols)
            stats.update(outcome.as_stats())

        rows = session.execute(
            sql(
                "SELECT t.symbol, fp.fiscal_year, fp.period_type::text AS pt, "
                "       count(*) AS facts "
                "FROM financial_fact ff "
                "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                "JOIN ticker t ON t.id = fp.ticker_id "
                "GROUP BY t.symbol, fp.fiscal_year, fp.period_type "
                "ORDER BY t.symbol, fp.fiscal_year DESC"
            )
        ).mappings()

        table = Table("symbol", "period", "facts", title="qe extract statements")
        for r in rows:
            table.add_row(r["symbol"], f"{r['fiscal_year']} {r['pt']}", str(r["facts"]))
        console.print(table)

        console.print(
            f"facts [green]{outcome.facts_written}[/green] · "
            f"periods [green]{outcome.periods_written}[/green] · "
            f"read [dim]{outcome.documents_read}[/dim] · skipped [yellow]{outcome.skipped}[/yellow]"
        )
        if outcome.assumed_lines_used:
            console.print(
                f"[yellow]{outcome.assumed_lines_used} facts came from UNVERIFIED "
                "mappings[/yellow] "
                "- confirm them against the filing and set status: verified in "
                "rules/vas_lines.yaml"
            )
        for failure in outcome.identity_failures[:10]:
            console.print(f"  [red]refused[/red] {failure}")
        for note in outcome.notes[:10]:
            console.print(f"  [dim]{note}[/dim]")


@extract_app.command("identifiers")
def extract_identifiers_cmd(
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all stored."),
) -> None:
    """Stored HOSE identity documents → security_identifier, with provenance per field."""
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.extract.identifiers import extract_identifiers  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None

    with session_scope() as session:
        with run_scope(
            session,
            PipelineStage.EXTRACT,
            "identifiers",
            params={"symbols": symbols or "all"},
        ) as (run_id, stats):
            result = extract_identifiers(session, run_id, symbols=symbols)
            stats.update(result.as_stats())

        rows = session.execute(
            sql(
                "SELECT t.symbol, si.scheme, si.value FROM security_identifier si "
                "JOIN ticker t ON t.id = si.ticker_id ORDER BY t.symbol, si.scheme"
            )
        ).mappings()

        table = Table("symbol", "scheme", "value", title="qe extract identifiers")
        for r in rows:
            table.add_row(r["symbol"], r["scheme"], r["value"])
        console.print(table)
        console.print(
            f"rows [green]{result.rows_written}[/green] from "
            f"[dim]{result.documents_read}[/dim] documents"
        )
        for note in result.notes[:10]:
            console.print(f"  [yellow]{note}[/yellow]")


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
def review_list(
    status: str = typer.Option("", help="Filter by status."),
    all_versions: bool = typer.Option(
        False, "--all-versions", help="Include superseded and older drafts."
    ),
) -> None:
    """Show what needs a decision: the newest version per ticker, plus live ones."""
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.review.service import queue  # noqa: PLC0415

    with session_scope() as session:
        rows = queue(session, status or None, all_versions=all_versions)

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


@fs_app.command("export")
def factsheet_export() -> None:
    """Write committed JSON snapshots, one per sheet version.

    The diff of these files after a rule change is the review artifact and the regression
    baseline at once.
    """
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.factsheet.export import export_snapshots  # noqa: PLC0415

    settings = load_settings()
    with session_scope() as session:
        outcome = export_snapshots(session, settings.factsheets_dir)

    console.print(
        f"wrote [green]{outcome.written}[/green] · unchanged [dim]{outcome.unchanged}[/dim] "
        f"→ {settings.factsheets_dir}"
    )
    for path in outcome.paths[:12]:
        console.print(f"  [dim]{path}[/dim]")


@calc_app.command("benchmarks")
def calc_benchmarks(
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
) -> None:
    """Sector distributions and each stock's own trailing range.

    A bare number tells a reader little; the same number beside its sector median and its
    own three-year range is information. Nothing here ranks or concludes.
    """
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc.benchmarks import (  # noqa: PLC0415
        CALC_VERSION,
        build_exchange_benchmarks,
        build_history_benchmarks,
        build_sector_benchmarks,
        parse_exchange_window_label,
    )
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    settings = load_settings()
    minimum = _min_benchmark_sample(settings)

    with session_scope() as session:
        as_of_date = _resolve_as_of(session, as_of)

        with run_scope(
            session,
            PipelineStage.CALC,
            "benchmarks",
            as_of=as_of_date,
            params={"min_sample": minimum, "calc_version": CALC_VERSION},
        ) as (run_id, stats):
            sector = build_sector_benchmarks(session, run_id, as_of_date, minimum_sample=minimum)
            history = build_history_benchmarks(session, run_id, as_of_date, minimum_sample=minimum)
            exchange = build_exchange_benchmarks(
                session, run_id, as_of_date, minimum_sample=minimum
            )
            stats.update(
                {
                    "sector": sector.as_stats(),
                    "ticker_history": history.as_stats(),
                    "exchange": exchange.as_stats(),
                }
            )

        rows = session.execute(
            sql(
                "SELECT s.code AS sector, b.metric_code, b.sample_size, b.median, "
                "       b.p25, b.p75 "
                "FROM metric_benchmark b JOIN sector s ON s.id = b.sector_id "
                "WHERE b.as_of_date = :d AND b.scope = 'sector' AND b.is_available "
                "ORDER BY b.metric_code, s.code"
            ),
            {"d": as_of_date},
        ).mappings()

        table = Table(
            "sector", "metric", "n", "p25", "median", "p75", title=f"sector medians - {as_of_date}"
        )
        for r in rows:
            table.add_row(
                r["sector"],
                r["metric_code"],
                str(r["sample_size"]),
                _num(r["p25"]),
                _num(r["median"]),
                _num(r["p75"]),
            )
        console.print(table)

        # Exchange rows carry no sector_id, so the join above cannot see them. Printed
        # separately rather than left out: a median the pipeline wrote and no command
        # shows is a number nobody checks.
        venue_rows = session.execute(
            sql(
                "SELECT b.window_label, b.metric_code, b.sample_size, b.median, b.p25, b.p75 "
                "FROM metric_benchmark b "
                "WHERE b.as_of_date = :d AND b.scope = 'exchange' AND b.is_available "
                "ORDER BY b.window_label, b.metric_code"
            ),
            {"d": as_of_date},
        ).mappings()

        venue_table = Table(
            "exchange",
            "metric",
            "n",
            "p25",
            "median",
            "p75",
            title=f"exchange medians - {as_of_date}",
        )
        for r in venue_rows:
            parsed = parse_exchange_window_label(r["window_label"])
            venue_table.add_row(
                parsed[0] if parsed else r["window_label"],
                r["metric_code"],
                str(r["sample_size"]),
                _num(r["p25"]),
                _num(r["median"]),
                _num(r["p75"]),
            )
        console.print(venue_table)

        console.print(
            f"sector [green]{sector.available}[/green]/[dim]{sector.written}[/dim] available · "
            f"own-history [green]{history.available}[/green]/[dim]{history.written}[/dim] · "
            f"exchange [green]{exchange.available}[/green]/[dim]{exchange.written}[/dim]"
        )


@calc_app.command("backfill")
def calc_backfill(
    quarters: int = typer.Option(12, help="How many quarter-ends back to compute (~3 years)."),
    flags: bool = typer.Option(True, help="Also evaluate flags at each date."),
) -> None:
    """Compute metrics at past quarter-end sessions, creating real history.

    Own-range benchmarks and flag state changes both need a time series, and the liquidity
    metrics can produce one from price history already ingested - every calc function takes
    an explicit `as_of` and reads only rows at or before it, so a past date is as valid as
    today. This is what makes "worst liquidity in three years" a statement the data
    supports rather than a claim.
    """
    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc import fundamentals, liquidity  # noqa: PLC0415, F401
    from quantumedge.calc.benchmarks import (  # noqa: PLC0415
        build_exchange_benchmarks,
        build_history_benchmarks,
        build_sector_benchmarks,
    )
    from quantumedge.calc.fundamentals import FUNDAMENTAL_CODES  # noqa: PLC0415
    from quantumedge.calc.registry import load_contexts, run_metrics  # noqa: PLC0415
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    settings = load_settings()
    minimum = _min_benchmark_sample(settings)
    codes = [*_LIQUIDITY_CODES, *FUNDAMENTAL_CODES]

    with session_scope() as session:
        # Quarter-end trading sessions, taken from the calendar rather than computed, so
        # every date is one the market was actually open.
        dates = list(
            session.execute(
                sql(
                    """
                    SELECT max(trade_date) AS d
                    FROM market_calendar
                    WHERE exchange_code = 'HOSE' AND is_trading_day
                    GROUP BY date_trunc('quarter', trade_date)
                    ORDER BY d DESC
                    LIMIT :n
                    """
                ),
                {"n": quarters},
            ).scalars()
        )
        if not dates:
            console.print("[yellow]no trading calendar - run: qe extract prices[/yellow]")
            raise typer.Exit(code=1)

        dates.reverse()  # oldest first, so flag state changes are detected in order
        table = Table("as of", "metrics", "available", "flags active", title="qe calc backfill")

        for as_of_date in dates:
            contexts = load_contexts(session, as_of_date)
            with run_scope(
                session,
                PipelineStage.CALC,
                "backfill",
                as_of=as_of_date,
                params={"codes": len(codes)},
            ) as (run_id, stats):
                outcome = run_metrics(
                    session, run_id, liquidity.CALC_VERSION, contexts, codes=codes
                )
                build_sector_benchmarks(session, run_id, as_of_date, minimum_sample=minimum)
                build_history_benchmarks(session, run_id, as_of_date, minimum_sample=minimum)
                build_exchange_benchmarks(session, run_id, as_of_date, minimum_sample=minimum)
                stats.update(outcome.as_stats())

            active = ""
            if flags:
                active = str(_evaluate_flags_at(session, as_of_date))

            table.add_row(str(as_of_date), str(outcome.written), str(outcome.available), active)

        console.print(table)
        console.print(
            f"[green]{len(dates)}[/green] quarter-ends computed. "
            "Own-range benchmarks now have a sample."
        )


@calc_app.command("all")
def calc_all(
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
    tickers: str = typer.Option("", help="Comma-separated symbols. Default: all covered."),
) -> None:
    """Every registered metric: liquidity (Agent 5) and fundamentals."""
    from datetime import date  # noqa: PLC0415

    from sqlalchemy import text as sql  # noqa: PLC0415

    from quantumedge.calc import fundamentals, liquidity  # noqa: PLC0415, F401
    from quantumedge.calc.fundamentals import FUNDAMENTAL_CODES  # noqa: PLC0415
    from quantumedge.calc.registry import load_contexts, run_metrics  # noqa: PLC0415
    from quantumedge.db.enums import PipelineStage  # noqa: PLC0415
    from quantumedge.db.session import session_scope  # noqa: PLC0415
    from quantumedge.runs import run_scope  # noqa: PLC0415

    symbols = [s.strip().upper() for s in tickers.split(",") if s.strip()] or None
    codes = [*_LIQUIDITY_CODES, *FUNDAMENTAL_CODES]

    with session_scope() as session:
        if as_of:
            as_of_date = date.fromisoformat(as_of)
        else:
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
        with run_scope(
            session,
            PipelineStage.CALC,
            "all_metrics",
            as_of=as_of_date,
            params={"symbols": symbols or "all", "codes": len(codes)},
        ) as (run_id, stats):
            outcome = run_metrics(session, run_id, liquidity.CALC_VERSION, contexts, codes=codes)
            stats.update(outcome.as_stats())

        rows = session.execute(
            sql(
                "SELECT mv.unavailable_reason AS reason, count(*) AS n FROM metric_value mv "
                "WHERE mv.as_of_date = :d AND NOT mv.is_available "
                "GROUP BY mv.unavailable_reason ORDER BY n DESC"
            ),
            {"d": as_of_date},
        ).mappings()

        table = Table("why unavailable", "count", title=f"qe calc all - {as_of_date}")
        for r in rows:
            table.add_row(r["reason"] or "-", str(r["n"]))
        console.print(table)
        console.print(
            f"available [green]{outcome.available}[/green] · "
            f"unavailable [yellow]{outcome.unavailable}[/yellow]"
        )


@app.command("run")
def run_daily(
    as_of: str = typer.Option("", help="Valuation date YYYY-MM-DD. Default: latest session."),
    skip_ingest: bool = typer.Option(False, "--skip-ingest", help="Reuse stored raw documents."),
) -> None:
    """The whole deterministic chain in order, stopping at the review gate.

    It deliberately does not publish. Nothing reaches a reader without a person running
    `qe review approve` and `qe review publish` - automating the drafting is fine,
    automating the verifying is what would destroy the thing being sold.
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    # Seed first, always. `flag_def` and `metric_def` mirror rules/*.yaml, and nothing
    # downstream reloads them - so without this, editing a threshold and running the chain
    # evaluated flags against the OLD definitions and stamped them with the old
    # rule_spec_version, with no error. `qe calc flags` now refuses in that state, which
    # would have made the chain fail here instead. Seeding is idempotent and cheap.
    steps: list[tuple[str, list[str]]] = [("db seed (rules -> definitions)", ["db", "seed"])]
    if not skip_ingest:
        steps.append(("ingest prices", ["ingest", "prices"]))
    steps += [
        ("extract prices + calendar", ["extract", "prices"]),
        # Filings are local files, so these two cost nothing when the folder is empty and
        # pick up anything newly dropped in without needing a separate command remembered.
        ("ingest filings", ["ingest", "filings"]),
        ("extract statements", ["extract", "statements"]),
        ("calc metrics", ["calc", "all"] + (["--as-of", as_of] if as_of else [])),
        ("calc flags", ["calc", "flags"] + (["--as-of", as_of] if as_of else [])),
        ("factsheet build", ["factsheet", "build"] + (["--as-of", as_of] if as_of else [])),
        ("factsheet export", ["factsheet", "export"]),
    ]

    for label, args in steps:
        console.print(f"[bold]→ {label}[/bold]")
        # PYTHONIOENCODING so the child's streams are UTF-8 too, regardless of the
        # console's code page.
        result = subprocess.run(
            [sys.executable, "-m", "quantumedge.cli", *args],
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0:
            console.print(f"[red]failed at: {label}[/red]")
            raise typer.Exit(code=result.returncode)

    console.print(
        "\n[green]chain complete.[/green] Nothing is published yet - review first:\n"
        "  qe review list\n"
        '  qe review approve <SYMBOL> --as <reviewer> --note "what you checked"\n'
        "  qe review publish <SYMBOL> --as <reviewer>"
    )


if __name__ == "__main__":
    app()
