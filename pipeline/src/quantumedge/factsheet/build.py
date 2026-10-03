"""Assemble a FactSheet from what the calc layer produced.

All formatting happens here, once. The web app receives pre-formatted display strings and
makes no numeric decisions of its own, which is what makes it impossible for the page to
disagree with the pipeline.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.liquidity import FRAMING_NOTE_VI, PARTICIPATION_RATE
from quantumedge.factsheet.comparisons import build_comparison, load_comparisons
from quantumedge.factsheet.schema import (
    NO_DISCLOSURE_FOUND,
    NOT_DISCLOSED,
    CompanyRef,
    Comparison,
    DisclosureGap,
    ExitEstimate,
    FactSheet,
    Figure,
    FlagItem,
    Lineage,
    LiquidityBlock,
    Provenance,
    TickerRef,
)
from quantumedge.runs import git_commit

LIQUIDITY_CATEGORIES = {"liquidity_market", "liquidity_balance_sheet"}

STATE_LABEL_VI = {
    "active": "Đang có",
    "inactive": "Không có",
    # Not "no flag": we cannot assess it, which is a different statement.
    "unknown": NO_DISCLOSURE_FOUND,
}


def _format(value: Decimal | None, unit: str, precision: int) -> str:
    """Pre-format for display, so the page never formats a number itself."""
    if value is None:
        return NOT_DISCLOSED
    if unit == "pct":
        return f"{value * 100:,.2f}%"
    if unit == "VND":
        return f"{value:,.0f} đ"
    if unit == "share":
        return f"{value:,.0f}"
    if unit == "x":
        return f"{value:,.2f} lần"
    if unit == "session":
        return f"{value:,.1f} phiên"
    if unit == "ratio":
        return f"{value:,.{precision}f}"
    return f"{value:,.{precision}f}"


def _figures(session: Session, ticker_id: int, as_of: date) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT mv.id, mv.metric_code, mv.value, mv.is_available, mv.unavailable_reason,
                   md.label_vi, md.definition_vi, md.unit, md.category, md.precision,
                   md.is_headline, mv.inputs
            FROM metric_value mv
            JOIN metric_def md ON md.code = mv.metric_code
            WHERE mv.ticker_id = :t AND mv.as_of_date = CAST(:d AS date)
            ORDER BY md.is_headline DESC, md.category, md.code
            """
        ),
        {"t": ticker_id, "d": as_of},
    ).mappings()
    return [dict(r) for r in rows]


def _to_figure(row: dict[str, Any], comparison: Comparison | None = None) -> Figure:
    return Figure(
        code=row["metric_code"],
        label_vi=row["label_vi"],
        definition_vi=row["definition_vi"],
        value=row["value"] if row["is_available"] else None,
        unit=row["unit"],
        display=_format(
            row["value"] if row["is_available"] else None, row["unit"], row["precision"]
        ),
        available=row["is_available"],
        unavailable_reason=row["unavailable_reason"],
        provenance=[
            Provenance(
                kind="metric",
                ref=f"metric_value:{row['id']}",
                label=f"{row['metric_code']} @ {row.get('inputs', {}).get('window_end', 'as_of')}",
            )
        ]
        if row["is_available"]
        else [],
        comparison=comparison,
    )


def build_fact_sheet(session: Session, symbol: str, as_of: date, calc_run_id: int) -> FactSheet:
    """Gather every computed input for one ticker into the single artifact."""
    ref = (
        session.execute(
            text(
                """
            SELECT t.id, t.symbol, t.exchange_code, t.status, t.listing_date,
                   t.par_value_vnd, t.shares_outstanding, t.coverage,
                   c.legal_name_vi, c.short_name, c.description_vi, c.website, c.ir_url,
                   s.code AS sector_code, s.name_vi AS sector_name_vi
            FROM ticker t
            JOIN company c ON c.id = t.company_id
            LEFT JOIN sector s ON s.id = c.sector_id
            WHERE t.symbol = :s
            """
            ),
            {"s": symbol},
        )
        .mappings()
        .first()
    )
    if ref is None:
        raise LookupError(f"unknown ticker '{symbol}'")

    rows = _figures(session, ref["id"], as_of)
    if not rows:
        raise LookupError(f"no metrics for {symbol} at {as_of} - run: qe calc liquidity")

    # One query for every benchmark on this page, rather than one per figure.
    benchmarks = load_comparisons(session, ref["id"], as_of)

    headline, other, liquidity_figures = [], [], []
    for row in rows:
        comparison = build_comparison(
            session,
            value=row["value"] if row["is_available"] else None,
            benchmarks=benchmarks.get(row["metric_code"], []),
            formatter=lambda v, _r=row: _format(v, _r["unit"], _r["precision"]),
        )
        figure = _to_figure(row, comparison)
        if row["category"] in LIQUIDITY_CATEGORIES:
            liquidity_figures.append(figure)
        elif row["is_headline"]:
            headline.append(figure)
        else:
            other.append(figure)

    # The exit ladder lives in sessions_to_exit's inputs; surface it only when the metric
    # was actually computable.
    exit_estimates: list[ExitEstimate] = []
    participation: str | None = None
    for row in rows:
        if row["metric_code"] == "sessions_to_exit" and row["is_available"]:
            ladder = (row["inputs"] or {}).get("sessions_to_exit", {})
            exit_estimates = [
                ExitEstimate(position_label=k, sessions=str(v)) for k, v in sorted(ladder.items())
            ]
            participation = str((row["inputs"] or {}).get("participation_rate", PARTICIPATION_RATE))

    flags = _flags(session, ref["id"], as_of)
    gaps = _gaps(ref, rows, flags)

    sheet_as_of = as_of
    figures_all = [*headline, *other, *liquidity_figures]

    return FactSheet(
        as_of_date=sheet_as_of,
        generated_at=datetime.now(UTC),
        ticker=TickerRef(
            symbol=ref["symbol"],
            exchange=ref["exchange_code"],
            status=ref["status"],
            listing_date=ref["listing_date"],
            par_value_vnd=ref["par_value_vnd"],
            shares_outstanding=ref["shares_outstanding"],
            coverage=ref["coverage"],
        ),
        company=CompanyRef(
            legal_name_vi=ref["legal_name_vi"],
            short_name=ref["short_name"],
            sector_code=ref["sector_code"],
            sector_name_vi=ref["sector_name_vi"],
            description_vi=ref["description_vi"],
            website=ref["website"],
            ir_url=ref["ir_url"],
        ),
        headline_metrics=headline,
        other_metrics=other,
        liquidity=LiquidityBlock(
            figures=liquidity_figures,
            exit_estimates=exit_estimates,
            participation_rate=participation,
            framing_note_vi=FRAMING_NOTE_VI,
        ),
        flags=flags,
        disclosure_gaps=gaps,
        allowed_numbers=collect_allowed_numbers(figures_all, flags),
        lineage=Lineage(
            rule_spec_version=_spec_version(session, ref["id"], as_of),
            calc_run_id=calc_run_id,
            git_commit=git_commit(),
            code_version=_code_version(),
            data_sources=_sources(session, ref["id"]),
            as_of_date=sheet_as_of,
        ),
    )


def _flags(session: Session, ticker_id: int, as_of: date) -> list[FlagItem]:
    rows = session.execute(
        text(
            """
            SELECT fi.flag_code, fi.state, fi.severity, fi.triggered_values, fi.evidence,
                   fi.reason, fd.name_vi, fd.category, fd.explanation_vi, fd.rule_expr
            FROM flag_instance fi
            JOIN flag_def fd ON fd.code = fi.flag_code
            WHERE fi.ticker_id = :t AND fi.as_of_date = CAST(:d AS date)
            ORDER BY CASE fi.state WHEN 'active' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                     fi.severity DESC, fi.flag_code
            """
        ),
        {"t": ticker_id, "d": as_of},
    ).mappings()

    items = []
    for r in rows:
        expr = r["rule_expr"] or {}
        summary = (
            f"{expr.get('metric', expr.get('event_type', expr.get('absent', '?')))} "
            f"{expr.get('op', '')} {expr.get('value', expr.get('within_months', ''))}".strip()
        )
        items.append(
            FlagItem(
                code=r["flag_code"],
                name_vi=r["name_vi"],
                category=r["category"],
                severity=r["severity"],
                state=r["state"],
                state_label_vi=STATE_LABEL_VI[r["state"]],
                explanation_vi=r["explanation_vi"],
                rule_summary=summary,
                triggered_values={k: str(v) for k, v in (r["triggered_values"] or {}).items()},
                evidence=[
                    Provenance(
                        kind=e.get("kind", "manual"),
                        ref=e.get("ref", ""),
                        label=e.get("label", ""),
                        source_url=e.get("source_url"),
                    )
                    for e in (r["evidence"] or [])
                ],
                reason=r["reason"],
            )
        )
    return items


def _gaps(ref: Any, rows: list[dict[str, Any]], flags: list[FlagItem]) -> list[DisclosureGap]:
    """Everything a complete page would show and this one cannot, stated plainly."""
    gaps: list[DisclosureGap] = []

    if not ref["description_vi"]:
        gaps.append(
            DisclosureGap(
                field="company.description_vi",
                label_vi="Mô tả hoạt động kinh doanh",
                reason="requires_human_authorship",
            )
        )
    if ref["shares_outstanding"] is None:
        gaps.append(
            DisclosureGap(
                field="ticker.shares_outstanding",
                label_vi="Số cổ phiếu đang lưu hành",
                reason="not_ingested",
            )
        )

    for row in rows:
        if not row["is_available"]:
            gaps.append(
                DisclosureGap(
                    field=f"metric.{row['metric_code']}",
                    label_vi=row["label_vi"],
                    reason=row["unavailable_reason"] or "unavailable",
                )
            )

    for flag in flags:
        if flag.state == "unknown":
            gaps.append(
                DisclosureGap(
                    field=f"flag.{flag.code}",
                    label_vi=flag.name_vi,
                    reason=flag.reason or "not_assessable",
                    display=NO_DISCLOSURE_FOUND,
                )
            )
    return gaps


def collect_allowed_numbers(figures: list[Figure], flags: list[FlagItem]) -> list[str]:
    """Every numeric string that legitimately appears in this sheet.

    Agent 2's future validator tokenises a Vietnamese draft and asserts each numeric token
    is in this set. Both raw and formatted forms are included, because a model writing prose
    will naturally reproduce the formatted one.
    """
    allowed: set[str] = {NOT_DISCLOSED}

    for figure in figures:
        if figure.value is not None:
            allowed.add(str(figure.value))
            allowed.add(str(figure.value.normalize()))
        allowed.add(figure.display)
        # Bare numeric runs out of the display string, so "1,234 đ" also permits "1,234".
        for token in _numeric_tokens(figure.display):
            allowed.add(token)

        # Benchmark statistics are on the page, so they are citable. Omitting them would
        # make Agent 2's validator reject a sector median the sheet itself displays -
        # treating a legitimate figure as a hallucination.
        if figure.comparison is None:
            continue
        for ref in [figure.comparison.sector, *figure.comparison.own_history]:
            if ref is None or not ref.available:
                continue
            for stat in (ref.median, ref.p25, ref.p75, ref.min_value, ref.max_value):
                if stat is not None:
                    allowed.add(str(stat))
                    allowed.add(str(stat.normalize()))
            for display in (ref.display_median, ref.display_range):
                allowed.add(display)
                for token in _numeric_tokens(display):
                    allowed.add(token)
            # The sample size is quoted as "trung vị của 3 mã", so it is citable too.
            allowed.add(str(ref.sample_size))

    for flag in flags:
        # Both sides of the comparison: a value, and the threshold it was tested against.
        # Both appear in evidence labels, so both are legitimately citable.
        for value in flag.triggered_values.values():
            allowed.add(value)
            for token in _numeric_tokens(value):
                allowed.add(token)
        for provenance in flag.evidence:
            for token in _numeric_tokens(provenance.label):
                allowed.add(token)

    return sorted(allowed)


def _numeric_tokens(textval: str) -> list[str]:
    out, current = [], ""
    for char in textval:
        if char.isdigit() or char in ",.-":
            current += char
        else:
            if any(c.isdigit() for c in current):
                out.append(current.strip(",.-"))
            current = ""
    if any(c.isdigit() for c in current):
        out.append(current.strip(",.-"))
    return out


def _spec_version(session: Session, ticker_id: int, as_of: date) -> str:
    return str(
        session.execute(
            text(
                "SELECT rule_spec_version FROM flag_instance "
                "WHERE ticker_id = :t AND as_of_date = CAST(:d AS date) LIMIT 1"
            ),
            {"t": ticker_id, "d": as_of},
        ).scalar_one_or_none()
        or "none"
    )


def _sources(session: Session, ticker_id: int) -> list[str]:
    return [
        str(s)
        for s in session.execute(
            text(
                "SELECT DISTINCT s.code FROM raw_document rd JOIN source s ON s.id = rd.source_id "
                "WHERE rd.ticker_id = :t ORDER BY s.code"
            ),
            {"t": ticker_id},
        ).scalars()
    ]


def _code_version() -> str:
    from quantumedge import __version__

    return __version__
