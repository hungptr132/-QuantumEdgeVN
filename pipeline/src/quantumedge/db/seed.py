"""Seed loader: YAML files into reference tables.

Every write is an upsert keyed on the natural key, so running this repeatedly converges
rather than duplicating or failing. That matters because the rule files change often and
re-seeding is the normal way to apply an edit.

The rule YAML is the source of truth; these tables are a queryable mirror of it.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.config import Settings
from quantumedge.hashing import hash_files
from quantumedge.vietnamese import vn_decimal, vn_money, vn_scaled


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"seed file missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping at the top level")
    return data


def rule_spec_version(settings: Settings) -> str:
    """Content hash across every rule file — the staleness marker for derived rows."""
    files = sorted(settings.rules_dir.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"no rule files in {settings.rules_dir}")
    return hash_files(files)


def seed_reference(session: Session, settings: Settings) -> dict[str, int]:
    """Exchanges, sectors, companies, tickers, users. Returns counts per table."""
    data = _load(settings.seeds_dir / "reference.yaml")
    counts: dict[str, int] = {}

    for row in data.get("exchanges", []):
        session.execute(
            text(
                "INSERT INTO exchange (code, name) VALUES (:code, :name) "
                "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name"
            ),
            row,
        )
    counts["exchange"] = len(data.get("exchanges", []))

    sector_ids: dict[str, int] = {}
    for row in data.get("sectors", []):
        sector_ids[row["code"]] = int(
            session.execute(
                text(
                    "INSERT INTO sector (code, name_vi, name_en) "
                    "VALUES (:code, :name_vi, :name_en) "
                    "ON CONFLICT (code) DO UPDATE SET name_vi = EXCLUDED.name_vi, "
                    "name_en = EXCLUDED.name_en, updated_at = now() RETURNING id"
                ),
                {"code": row["code"], "name_vi": row["name_vi"], "name_en": row.get("name_en")},
            ).scalar_one()
        )
    counts["sector"] = len(sector_ids)

    coverage = _load(settings.rules_dir / "coverage.yaml")
    covered = {c["symbol"]: c.get("coverage", "shallow") for c in coverage.get("covered", [])}

    companies = data.get("companies", [])
    for row in companies:
        # short_name is not unique in the schema, so companies are matched via their
        # ticker symbol, which is.
        symbol = row["ticker"]["symbol"]
        existing = session.execute(
            text("SELECT company_id FROM ticker WHERE symbol = :s"), {"s": symbol}
        ).scalar_one_or_none()

        params = {
            "legal_name_vi": row["legal_name_vi"],
            "short_name": row["short_name"],
            "sector_id": sector_ids.get(row["sector"]),
        }
        if existing is None:
            company_id = int(
                session.execute(
                    text(
                        "INSERT INTO company (legal_name_vi, short_name, sector_id) "
                        "VALUES (:legal_name_vi, :short_name, :sector_id) RETURNING id"
                    ),
                    params,
                ).scalar_one()
            )
        else:
            company_id = int(existing)
            session.execute(
                text(
                    "UPDATE company SET legal_name_vi = :legal_name_vi, "
                    "short_name = :short_name, sector_id = :sector_id, updated_at = now() "
                    "WHERE id = :id"
                ),
                {**params, "id": company_id},
            )

        session.execute(
            text(
                "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered, coverage) "
                "VALUES (:symbol, :company_id, :exchange, :is_covered, :coverage) "
                "ON CONFLICT (symbol) DO UPDATE SET company_id = EXCLUDED.company_id, "
                "exchange_code = EXCLUDED.exchange_code, is_covered = EXCLUDED.is_covered, "
                "coverage = EXCLUDED.coverage, updated_at = now()"
            ),
            {
                "symbol": symbol,
                "company_id": company_id,
                "exchange": row["ticker"]["exchange"],
                "is_covered": symbol in covered,
                "coverage": covered.get(symbol, "shallow"),
            },
        )
    counts["company"] = len(companies)
    counts["ticker"] = len(companies)

    users = data.get("users", [])
    for row in users:
        session.execute(
            text(
                "INSERT INTO app_user (slug, display_name, role) "
                "VALUES (:slug, :display_name, :role) "
                "ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name, "
                "role = EXCLUDED.role"
            ),
            {
                "slug": row["slug"],
                "display_name": row["display_name"],
                "role": row.get("role", "reader"),
            },
        )
    counts["app_user"] = len(users)
    return counts


def seed_sources(session: Session, settings: Settings) -> int:
    """Provider rows. `robots_note` carries the access justification with the code."""
    data = _load(settings.seeds_dir / "reference.yaml")
    rows = data.get("sources", [])
    for row in rows:
        session.execute(
            text(
                "INSERT INTO source (code, kind, name, base_url, rate_limit_per_min, "
                " robots_note) "
                "VALUES (:code, :kind, :name, :base_url, :rate_limit_per_min, :robots_note) "
                "ON CONFLICT (code) DO UPDATE SET kind = EXCLUDED.kind, "
                "name = EXCLUDED.name, base_url = EXCLUDED.base_url, "
                "rate_limit_per_min = EXCLUDED.rate_limit_per_min, "
                "robots_note = EXCLUDED.robots_note"
            ),
            {
                "code": row["code"],
                "kind": row["kind"],
                "name": row["name"],
                "base_url": row.get("base_url"),
                "rate_limit_per_min": row.get("rate_limit_per_min", 20),
                "robots_note": (row.get("robots_note") or "").strip() or None,
            },
        )
    return len(rows)


def seed_ticker_aliases(session: Session, settings: Settings) -> int:
    """Alias rows for headline attribution, derived from names already verified at seed
    time, plus any the owner has reviewed by hand.

    Only `company.legal_name_vi` is derived automatically. A legal name is long and
    specific, so an exact token-boundary match on it cannot land on the wrong company.

    `company.short_name` is deliberately NOT derived, even though it is right there and
    the temptation is obvious. A trading nickname's false-positive risk varies per
    company, and one of the nine covered names demonstrates it: ANV's short name is
    "Nam Việt", which normalises to `nam viet` — a token-boundary substring of
    `nam viet nam`. Any headline about southern Vietnam would have been attributed to a
    named company, which is the exact failure this product exists to prevent. Nicknames
    therefore go in the `aliases:` block of `seeds/reference.yaml`, where a person has
    looked at each one.

    `alias_norm` is computed by Postgres's `unaccent(lower(...))` rather than in Python,
    because that is what `extract/news.py` normalises headlines with. Two definitions of
    "normalised" on the two sides of a comparison is how an exact match stops being exact.
    """
    data = _load(settings.seeds_dir / "reference.yaml")

    derived = len(
        session.execute(
            text(
                "INSERT INTO ticker_alias (ticker_id, alias, alias_norm, alias_kind) "
                "SELECT t.id, c.legal_name_vi, unaccent(lower(c.legal_name_vi)), "
                "       'legal_name' "
                "FROM ticker t JOIN company c ON c.id = t.company_id "
                "WHERE c.legal_name_vi <> '' "
                "ON CONFLICT (alias_norm, ticker_id) DO UPDATE SET "
                " alias = EXCLUDED.alias, alias_kind = EXCLUDED.alias_kind "
                "RETURNING id"
            )
        )
        .scalars()
        .all()
    )

    reviewed = data.get("aliases") or []
    for row in reviewed:
        session.execute(
            text(
                "INSERT INTO ticker_alias (ticker_id, alias, alias_norm, alias_kind) "
                "SELECT t.id, :alias, unaccent(lower(:alias)), :kind "
                "FROM ticker t WHERE t.symbol = :symbol "
                "ON CONFLICT (alias_norm, ticker_id) DO UPDATE SET "
                " alias = EXCLUDED.alias, alias_kind = EXCLUDED.alias_kind"
            ),
            {"symbol": row["symbol"], "alias": row["alias"], "kind": row["alias_kind"]},
        )

    return derived + len(reviewed)


def seed_news_sources(session: Session, settings: Settings) -> int:
    """`source` rows for the RSS feeds declared in `seeds/news_sources.yaml`.

    Read from that file rather than copied into `reference.yaml`, because
    `rate_limit_per_min` and `robots_note` are read at fetch time from the same file. A
    threshold typed into two files drifts the first time it is tuned — which has already
    happened once in this repo — so the feed list has exactly one home.
    """
    from quantumedge.ingest.rss_news import load_feed_specs  # noqa: PLC0415

    specs = load_feed_specs(settings)
    for spec in specs:
        session.execute(
            text(
                "INSERT INTO source (code, kind, name, base_url, rate_limit_per_min, "
                " robots_note) "
                "VALUES (:code, 'rss', :name, :base_url, :rate_limit_per_min, :robots_note) "
                "ON CONFLICT (code) DO UPDATE SET kind = EXCLUDED.kind, "
                "name = EXCLUDED.name, base_url = EXCLUDED.base_url, "
                "rate_limit_per_min = EXCLUDED.rate_limit_per_min, "
                "robots_note = EXCLUDED.robots_note"
            ),
            {
                "code": spec.code,
                "name": spec.name,
                "base_url": spec.base_url,
                "rate_limit_per_min": spec.rate_limit_per_min,
                "robots_note": spec.robots_note.strip(),
            },
        )
    return len(specs)


def seed_fact_codes(session: Session, settings: Settings) -> int:
    data = _load(settings.rules_dir / "fact_codes.yaml")
    rows = data.get("fact_codes", [])
    for row in rows:
        session.execute(
            text(
                "INSERT INTO financial_fact_def "
                "(code, label_vi, label_en, definition_vi, unit, statement, is_derived, "
                " formula_text) "
                "VALUES (:code, :label_vi, :label_en, :definition_vi, :unit, :statement, "
                ":is_derived, :formula_text) "
                "ON CONFLICT (code) DO UPDATE SET label_vi = EXCLUDED.label_vi, "
                "label_en = EXCLUDED.label_en, definition_vi = EXCLUDED.definition_vi, "
                "unit = EXCLUDED.unit, statement = EXCLUDED.statement, "
                "is_derived = EXCLUDED.is_derived, formula_text = EXCLUDED.formula_text"
            ),
            {
                "code": row["code"],
                "label_vi": row["label_vi"],
                "label_en": row.get("label_en"),
                "definition_vi": row["definition_vi"].strip(),
                "unit": row["unit"],
                "statement": row["statement"],
                "is_derived": row.get("is_derived", False),
                "formula_text": row.get("formula_text"),
            },
        )
    return len(rows)


def seed_metrics(session: Session, settings: Settings) -> int:
    data = _load(settings.rules_dir / "metrics.yaml")
    rows = data.get("metrics", [])
    for row in rows:
        session.execute(
            text(
                "INSERT INTO metric_def "
                "(code, label_vi, label_en, definition_vi, formula_text, unit, category, "
                " precision, requires_facts, is_headline) "
                "VALUES (:code, :label_vi, :label_en, :definition_vi, :formula_text, :unit, "
                ":category, :precision, :requires_facts, :is_headline) "
                "ON CONFLICT (code) DO UPDATE SET label_vi = EXCLUDED.label_vi, "
                "label_en = EXCLUDED.label_en, definition_vi = EXCLUDED.definition_vi, "
                "formula_text = EXCLUDED.formula_text, unit = EXCLUDED.unit, "
                "category = EXCLUDED.category, precision = EXCLUDED.precision, "
                "requires_facts = EXCLUDED.requires_facts, is_headline = EXCLUDED.is_headline"
            ),
            {
                "code": row["code"],
                "label_vi": row["label_vi"],
                "label_en": row.get("label_en"),
                "definition_vi": row["definition_vi"].strip(),
                "formula_text": row["formula_text"],
                "unit": row["unit"],
                "category": row["category"],
                "precision": row.get("precision", 2),
                "requires_facts": row.get("requires_facts", []),
                "is_headline": row.get("is_headline", False),
            },
        )
    return len(rows)


def _render_rule_prose(row: dict[str, Any]) -> dict[str, str]:
    """Substitute the rule's own numbers into its Vietnamese prose.

    A threshold typed twice - once in `expr`, once in the sentence describing it - drifts
    the first time it is tuned. That happened: raising thin-liquidity from 1 tỷ to 2 tỷ
    left every page still reading "dưới 1 tỷ" while the rule tested 2 tỷ, which is a page
    stating a false threshold. Templating means the number exists once.

    Placeholders, all optional:
      {threshold}     scaled and idiomatic   - "2 tỷ"
      {threshold_vnd} scaled with currency    - "2 tỷ đồng"
      {threshold_raw} grouped digits          - "2.000.000.000"
      {threshold_pct} a ratio as a percent    - "5%"
      {threshold_abs} magnitude, sign dropped - "50%" from -0.5
      {multiple}      a benchmark multiple    - "0,25"
      {within_months} an event window         - "24"
    """
    expr = row.get("expr") or {}
    values: dict[str, str] = {}

    if "value" in expr:
        values["threshold"] = vn_scaled(expr["value"])
        values["threshold_vnd"] = vn_money(expr["value"])
        values["threshold_raw"] = vn_decimal(expr["value"])
        # A rule whose metric is a ratio states its threshold as a percentage, because
        # "hơn 5% số phiên" is how a reader thinks about it, not "hơn 0,05".
        as_pct = Decimal(str(expr["value"])) * 100
        values["threshold_pct"] = vn_decimal(as_pct, 2).rstrip("0").rstrip(",") + "%"
        # A drawdown threshold is stored negative (-0.5 = 50% below the high) but reads
        # naturally as a magnitude: "thấp hơn đỉnh trên 50%". The sign is carried by the
        # surrounding words, so repeating it would be wrong, not redundant.
        values["threshold_abs"] = vn_decimal(abs(as_pct), 2).rstrip("0").rstrip(",") + "%"
    if "within_months" in expr:
        values["within_months"] = vn_decimal(expr["within_months"])
    if "multiple" in expr:
        values["multiple"] = vn_decimal(expr["multiple"], 2).rstrip("0").rstrip(",")

    out: dict[str, str] = {}
    for field in ("name_vi", "name_en", "explanation_vi"):
        text_value = row.get(field)
        if not isinstance(text_value, str):
            continue
        for key, rendered in values.items():
            text_value = text_value.replace("{" + key + "}", rendered)
        out[field] = text_value
    return out


def seed_flags(session: Session, settings: Settings) -> int:
    """Mirror flags.yaml into flag_def, stamped with the current rule_spec_version."""
    data = _load(settings.rules_dir / "flags.yaml")
    spec_version = rule_spec_version(settings)
    rows = data.get("flags", [])
    for row in rows:
        prose = _render_rule_prose(row)
        session.execute(
            text(
                "INSERT INTO flag_def "
                "(code, name_vi, name_en, category, severity, rule_kind, rule_expr, "
                " requires_metrics, on_missing, explanation_vi, rule_version, "
                " rule_spec_version, is_active) "
                "VALUES (:code, :name_vi, :name_en, :category, :severity, :rule_kind, "
                "CAST(:rule_expr AS jsonb), :requires_metrics, :on_missing, :explanation_vi, "
                ":rule_version, :rule_spec_version, true) "
                "ON CONFLICT (code) DO UPDATE SET name_vi = EXCLUDED.name_vi, "
                "name_en = EXCLUDED.name_en, category = EXCLUDED.category, "
                "severity = EXCLUDED.severity, rule_kind = EXCLUDED.rule_kind, "
                "rule_expr = EXCLUDED.rule_expr, requires_metrics = EXCLUDED.requires_metrics, "
                "on_missing = EXCLUDED.on_missing, explanation_vi = EXCLUDED.explanation_vi, "
                "rule_version = EXCLUDED.rule_version, "
                "rule_spec_version = EXCLUDED.rule_spec_version, updated_at = now()"
            ),
            {
                "code": row["code"],
                "name_vi": prose["name_vi"],
                "name_en": prose.get("name_en"),
                "category": row["category"],
                "severity": row["severity"],
                "rule_kind": row["rule_kind"],
                "rule_expr": json.dumps(row["expr"]),
                "requires_metrics": row.get("requires_metrics", []),
                "on_missing": row.get("on_missing", "unknown"),
                "explanation_vi": prose["explanation_vi"].strip(),
                "rule_version": row.get("rule_version", "v1"),
                "rule_spec_version": spec_version,
            },
        )
    return len(rows)


def _ticker_id(session: Session, symbol: str, path: Path) -> int:
    """Resolve a symbol, refusing to continue if it is unknown.

    A typo in a seed file must not silently drop a row. These files are small and
    hand-written, which is exactly the case where a quiet skip is unrecoverable: the
    event would simply never appear and nothing would say why.
    """
    row = session.execute(
        text("SELECT id FROM ticker WHERE symbol = :s"), {"s": symbol}
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"{path.name}: no such ticker {symbol!r} - seed reference.yaml first")
    return int(row)


def seed_events(session: Session, settings: Settings) -> dict[str, int]:
    """Hand-curated disclosure events, and the coverage assertions behind them.

    Reference data rather than ingest output: these rows come from a person reading
    disclosures, not from a source module fetching bytes, so they load here with
    `extraction_method='manual'`. Agent 1 will later write the same two tables from
    `ingest/`, with its model and prompt version, and nothing downstream changes.

    `event_coverage` is the half that carries the weight. An `event_presence` rule finding
    nothing can only mean "nothing was disclosed" where someone recorded that they looked;
    otherwise it means "we do not know", and those are different claims about a named
    company. Both blocks ship empty for that reason - see the comment at the top of
    `seeds/events.yaml`.
    """
    path = settings.seeds_dir / "events.yaml"
    data = _load(path)
    counts = {"event_coverage": 0, "event": 0}

    for row in data.get("coverage") or []:
        event_type = str(row["event_type"])
        if event_type == "none":
            raise ValueError(
                f"{path.name}: coverage for {row['symbol']} claims event_type 'none', which is "
                "the enum's sentinel for no event, not a category anyone can review"
            )
        session.execute(
            text(
                "INSERT INTO event_coverage (ticker_id, event_type, reviewed_through, "
                " reviewed_by, source_note, extraction_method) "
                "VALUES (:ticker_id, CAST(:event_type AS event_type), :reviewed_through, "
                " :reviewed_by, :source_note, CAST(:method AS extraction_method)) "
                "ON CONFLICT (ticker_id, event_type) DO UPDATE SET "
                " reviewed_through = EXCLUDED.reviewed_through, "
                " reviewed_by = EXCLUDED.reviewed_by, "
                " source_note = EXCLUDED.source_note, "
                " reviewed_at = now()"
            ),
            {
                "ticker_id": _ticker_id(session, row["symbol"], path),
                "event_type": event_type,
                "reviewed_through": row["reviewed_through"],
                "reviewed_by": row["reviewed_by"],
                "source_note": row["source_note"].strip(),
                "method": row.get("extraction_method", "manual"),
            },
        )
        counts["event_coverage"] += 1

    for row in data.get("events") or []:
        session.execute(
            text(
                "INSERT INTO event (ticker_id, event_type, event_date, severity, source_url, "
                " excerpt, extraction_method) "
                "VALUES (:ticker_id, CAST(:event_type AS event_type), :event_date, "
                " CAST(:severity AS flag_severity), :source_url, :excerpt, "
                " CAST(:method AS extraction_method)) "
                "ON CONFLICT (ticker_id, event_type, event_date, source_url) DO UPDATE SET "
                " severity = EXCLUDED.severity, excerpt = EXCLUDED.excerpt"
            ),
            {
                "ticker_id": _ticker_id(session, row["symbol"], path),
                "event_type": str(row["event_type"]),
                "event_date": row["event_date"],
                "severity": row["severity"],
                "source_url": row["source_url"],
                "excerpt": row["excerpt"].strip(),
                "method": row.get("extraction_method", "manual"),
            },
        )
        counts["event"] += 1

    return counts


def seed_all(session: Session, settings: Settings) -> dict[str, int]:
    counts = seed_reference(session, settings)
    counts["ticker_alias"] = seed_ticker_aliases(session, settings)
    counts["source"] = seed_sources(session, settings) + seed_news_sources(session, settings)
    counts["financial_fact_def"] = seed_fact_codes(session, settings)
    counts["metric_def"] = seed_metrics(session, settings)
    counts["flag_def"] = seed_flags(session, settings)
    counts.update(seed_events(session, settings))
    return counts
