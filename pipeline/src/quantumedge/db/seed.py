"""Seed loader: YAML files into reference tables.

Every write is an upsert keyed on the natural key, so running this repeatedly converges
rather than duplicating or failing. That matters because the rule files change often and
re-seeding is the normal way to apply an edit.

The rule YAML is the source of truth; these tables are a queryable mirror of it.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.config import Settings
from quantumedge.hashing import hash_files


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


def seed_flags(session: Session, settings: Settings) -> int:
    """Mirror flags.yaml into flag_def, stamped with the current rule_spec_version."""
    data = _load(settings.rules_dir / "flags.yaml")
    spec_version = rule_spec_version(settings)
    rows = data.get("flags", [])
    for row in rows:
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
                "name_vi": row["name_vi"],
                "name_en": row.get("name_en"),
                "category": row["category"],
                "severity": row["severity"],
                "rule_kind": row["rule_kind"],
                "rule_expr": json.dumps(row["expr"]),
                "requires_metrics": row.get("requires_metrics", []),
                "on_missing": row.get("on_missing", "unknown"),
                "explanation_vi": row["explanation_vi"].strip(),
                "rule_version": row.get("rule_version", "v1"),
                "rule_spec_version": spec_version,
            },
        )
    return len(rows)


def seed_all(session: Session, settings: Settings) -> dict[str, int]:
    counts = seed_reference(session, settings)
    counts["financial_fact_def"] = seed_fact_codes(session, settings)
    counts["metric_def"] = seed_metrics(session, settings)
    counts["flag_def"] = seed_flags(session, settings)
    return counts
