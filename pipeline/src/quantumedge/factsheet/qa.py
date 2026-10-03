"""Machine validation. No model involved — these are the checks that make a human review
worth something, by catching mechanically everything a reviewer should not have to.

The regulatory screen is the one most worth noting: "education, never recommendations" and
"never say false or scam" become a failing test rather than a habit someone has to remember
under deadline.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.factsheet.build import _numeric_tokens
from quantumedge.factsheet.schema import NOT_DISCLOSED, FactSheet

# Phrases a verification tool may never publish. Recommendations and price targets are
# SSC-licensed territory; "scam" and "lừa đảo" are defamation exposure. An unmatched claim
# is "no corresponding disclosure found", never a verdict.
FORBIDDEN_PATTERNS = (
    r"nên mua",
    r"nên bán",
    r"khuyến nghị",
    r"mục tiêu giá",
    r"cam kết",
    r"đảm bảo lợi nhuận",
    r"chắc chắn tăng",
    r"lừa đảo",
    r"\bscam\b",
    r"\bkhống\b",
)


# The spec's own commitment: the ticker page leads with five metrics.
MIN_HEADLINE_METRICS = 5


@dataclass
class QaReport:
    passed: bool = True
    checks: list[dict[str, Any]] = field(default_factory=list)

    def record(self, code: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": code, "passed": ok, "detail": detail})
        if not ok:
            self.passed = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "failed": [c["check"] for c in self.checks if not c["passed"]],
        }


def validate(session: Session, sheet: FactSheet, expected_symbol: str) -> QaReport:
    report = QaReport()

    # 1. Ticker identity. A sheet rendered under the wrong symbol is the worst failure here.
    report.record(
        "symbol_matches",
        sheet.ticker.symbol == expected_symbol,
        f"{sheet.ticker.symbol} vs {expected_symbol}",
    )

    # 2. Every available figure must cite something that resolves to a real row.
    unsourced = [f.code for f in _all_figures(sheet) if f.available and not f.provenance]
    report.record("every_figure_has_provenance", not unsourced, ",".join(unsourced))

    dangling = _dangling_refs(session, sheet)
    report.record("provenance_refs_resolve", not dangling, ",".join(dangling))

    # 3. Required sections present.
    report.record("has_liquidity_block", bool(sheet.liquidity.figures))
    # The spec commits the page to leading with five metrics. This was specified and never
    # implemented, and the gap was invisible: a sheet leading with three figures passed QA.
    # Presence is checked, NOT availability - requiring five *available* figures would
    # refuse to publish an honest sheet about a company that discloses little, which
    # inverts the product's own rule that an absent figure is cheaper than a wrong one.
    report.record(
        "headline_has_at_least_five_metrics",
        len(sheet.headline_metrics) >= MIN_HEADLINE_METRICS,
        f"{len(sheet.headline_metrics)} of {MIN_HEADLINE_METRICS}",
    )
    report.record("has_flags_section", sheet.flags is not None)
    report.record("has_disclosure_gaps_section", sheet.disclosure_gaps is not None)
    report.record("has_lineage", bool(sheet.lineage.git_commit))

    # 4. An unavailable figure must say so in the display string, not render as blank or 0.
    wrong_display = [
        f.code for f in _all_figures(sheet) if not f.available and f.display != NOT_DISCLOSED
    ]
    report.record(
        "unavailable_figures_display_not_disclosed", not wrong_display, ",".join(wrong_display)
    )

    missing_reason = [
        f.code for f in _all_figures(sheet) if not f.available and not f.unavailable_reason
    ]
    report.record(
        "unavailable_figures_state_a_reason", not missing_reason, ",".join(missing_reason)
    )

    # 5. Flag integrity: active needs evidence, unknown needs a reason.
    no_evidence = [f.code for f in sheet.flags if f.state == "active" and not f.evidence]
    report.record("active_flags_carry_evidence", not no_evidence, ",".join(no_evidence))

    no_reason = [f.code for f in sheet.flags if f.state == "unknown" and not f.reason]
    report.record("unknown_flags_state_a_reason", not no_reason, ",".join(no_reason))

    # 6. One rule-spec version per sheet: a sheet mixing versions is not reproducible.
    report.record("single_rule_spec_version", bool(sheet.lineage.rule_spec_version))

    # 7. Price freshness. A stale page is a misleading page.
    stale = _staleness(session, sheet)
    report.record("price_is_fresh", stale <= 5, f"{stale} sessions behind as_of")

    # 8. Regulatory language screen over every human-authored string in the payload.
    offending = _forbidden_language(sheet)
    report.record("no_prohibited_language", not offending, ",".join(offending))

    # 9. Self-consistency: every numeric token in prose must be in allowed_numbers. This is
    #    the check Agent 2 will reuse verbatim; running it on our own strings now means it
    #    is tested before it has a customer.
    leaked = _unlisted_numbers(sheet)
    report.record("prose_numbers_are_all_allowed", not leaked, ",".join(sorted(leaked)[:10]))

    # 10. allowed_numbers must not be empty, or check 9 would pass trivially.
    report.record("allowed_numbers_populated", len(sheet.allowed_numbers) > 1)

    return report


def _all_figures(sheet: FactSheet) -> list[Any]:
    return [*sheet.headline_metrics, *sheet.other_metrics, *sheet.liquidity.figures]


def _dangling_refs(session: Session, sheet: FactSheet) -> list[str]:
    """Check that `metric_value:<id>` style references point at rows that exist."""
    refs: set[str] = set()
    for figure in _all_figures(sheet):
        refs.update(p.ref for p in figure.provenance)
    for flag in sheet.flags:
        refs.update(p.ref for p in flag.evidence)

    dangling: list[str] = []
    for ref in sorted(refs):
        table, _, raw_id = ref.partition(":")
        if table not in {"metric_value", "metric_benchmark", "event", "price_bar"}:
            continue
        if not raw_id.isdigit():
            dangling.append(ref)
            continue
        exists = session.execute(
            text(f"SELECT exists (SELECT 1 FROM {table} WHERE id = :i)"),  # noqa: S608
            {"i": int(raw_id)},
        ).scalar_one()
        if not exists:
            dangling.append(ref)
    return dangling


def _staleness(session: Session, sheet: FactSheet) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM market_calendar mc "
                "WHERE mc.exchange_code = CAST(:e AS varchar(10)) AND mc.is_trading_day "
                "  AND mc.trade_date > ("
                "      SELECT coalesce(max(pb.trade_date), CAST(:d AS date)) FROM price_bar pb "
                "      JOIN ticker t ON t.id = pb.ticker_id WHERE t.symbol = :s) "
                "  AND mc.trade_date <= CAST(:d AS date)"
            ),
            {"e": sheet.ticker.exchange, "d": sheet.as_of_date, "s": sheet.ticker.symbol},
        ).scalar_one()
    )


def _human_strings(sheet: FactSheet) -> list[str]:
    """Every piece of prose in the sheet. Used for the language screen, which must cover
    authored glossary text too - a prohibited phrase is prohibited wherever it appears."""
    out = [sheet.liquidity.framing_note_vi, *sheet.what_would_have_to_be_true]
    if sheet.company.description_vi:
        out.append(sheet.company.description_vi)
    for figure in _all_figures(sheet):
        out.extend([figure.label_vi, figure.definition_vi, figure.display])
    for flag in sheet.flags:
        out.extend([flag.name_vi, flag.explanation_vi, flag.state_label_vi])
        out.extend(p.label for p in flag.evidence)
    for gap in sheet.disclosure_gaps:
        out.extend([gap.label_vi, gap.display])
    return out


def _claim_strings(sheet: FactSheet) -> list[str]:
    """Only prose making a numeric claim ABOUT THIS COMPANY.

    This is the narrower set the number check applies to, and the distinction matters.
    Glossary definitions and flag explanations are human-authored constants that describe a
    rule - "khả năng trả lãi dưới 2 lần", "mệnh giá 10.000 đồng" - and their numbers are
    part of the rule, not an assertion about this ticker. Screening them would flag the
    definition of par value as an unsourced figure, which is noise.

    What the check is actually guarding against is a generated sentence claiming this
    company's turnover was some number that appears nowhere in its data. That is what Agent
    2 will produce, and it is exactly this set.
    """
    out: list[str] = list(sheet.what_would_have_to_be_true)
    if sheet.company.description_vi:
        out.append(sheet.company.description_vi)
    for figure in _all_figures(sheet):
        out.append(figure.display)
    for flag in sheet.flags:
        out.extend(p.label for p in flag.evidence)
        out.extend(flag.triggered_values.values())
    return out


def _forbidden_language(sheet: FactSheet) -> list[str]:
    offending: list[str] = []
    for value in _human_strings(sheet):
        lowered = value.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, lowered):
                offending.append(pattern)
    return sorted(set(offending))


def _unlisted_numbers(sheet: FactSheet) -> set[str]:
    allowed = set(sheet.allowed_numbers)
    leaked: set[str] = set()
    for value in _claim_strings(sheet):
        for token in _numeric_tokens(value):
            # Single digits appear inside ordinary prose ("20 phiên") and in glossary text;
            # they carry no claim about this company, so they are not treated as figures.
            if len(token) > 1 and token not in allowed:
                leaked.add(token)
    return leaked
