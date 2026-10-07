"""Is what a reader sees still true?

`qe doctor`'s environment checks answer "can this machine run the pipeline". This answers
the question that actually bites, and it is a different question: a page can render
perfectly, pass every QA check, and still be wrong because the world moved and nothing
re-ran.

Two ways that happens, neither visible anywhere else:

**Stale rules.** `rule_spec_version` is the content hash of `rules/*.yaml`. Change a
threshold and every stored flag keeps the old hash until flags are recomputed — so the page
goes on showing conclusions drawn under a rule set that no longer exists. Found in practice:
the published AGM page was two rule revisions behind and nothing said so.

**Stale prices.** If the pipeline has not run for a fortnight the page still renders, dated
a fortnight ago. The sheet shows its `as_of_date` so a reader *can* see it, but nobody
operating the thing would notice.

This module only reads and reports. It lives at the package root rather than in a stage
because it reads across all of them, which is exactly what a stage may not do.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# The same tolerance the fact sheet's QA uses for price freshness, so the two cannot
# disagree about what counts as stale.
MAX_SESSIONS_BEHIND = 5


@dataclass
class Health:
    """What the pipeline currently holds, measured against what it should hold."""

    rule_spec_version: str

    latest_price_date: date | None = None
    sessions_behind: int = 0

    published: int = 0
    stale_published: list[str] = field(default_factory=list)

    stale_flag_rows: int = 0

    covered: int = 0
    unpublished: int = 0

    @property
    def prices_are_fresh(self) -> bool:
        return self.latest_price_date is not None and self.sessions_behind <= MAX_SESSIONS_BEHIND

    @property
    def rules_are_current(self) -> bool:
        return not self.stale_published and self.stale_flag_rows == 0

    def warnings(self) -> list[str]:
        """Each warning paired with the command that fixes it.

        A warning nobody can act on gets ignored, and then so does the next one.
        """
        out: list[str] = []

        if self.latest_price_date is None:
            out.append("No price data at all.  Fix: qe ingest prices && qe extract prices")
        elif not self.prices_are_fresh:
            out.append(
                f"Prices are {self.sessions_behind} trading sessions behind. Published pages "
                f"are dated {self.latest_price_date}.  Fix: qe run"
            )

        if self.stale_flag_rows:
            # `qe db seed` first, and not optionally. flag_def mirrors rules/flags.yaml and
            # only seeding refreshes it, so `qe calc flags` alone re-evaluated against the
            # old definitions and left the staleness exactly as it was. This warning used
            # to name only the second half, which is how the gap was found: the remediation
            # it printed did not clear what it was reporting.
            out.append(
                f"{self.stale_flag_rows} flag rows were evaluated under an older rule spec.  "
                "Fix: qe db seed && qe calc flags"
            )

        if self.stale_published:
            shown = ", ".join(self.stale_published[:6])
            out.append(
                f"{len(self.stale_published)} published page(s) were built under an older rule "
                f"spec ({shown}). A reader is seeing conclusions from rules that no longer "
                "exist.  Fix: qe run, then qe review approve + publish"
            )

        return out


def assess(session: Session, rule_spec_version: str) -> Health:
    """Gather health facts. Reads only; decides nothing."""
    health = Health(rule_spec_version=rule_spec_version)

    latest, behind = session.execute(
        text(
            """
            SELECT max(pb.trade_date),
                   (SELECT count(*) FROM market_calendar mc
                     WHERE mc.is_trading_day
                       AND mc.trade_date > (SELECT max(trade_date) FROM price_bar))
            FROM price_bar pb
            """
        )
    ).one()
    health.latest_price_date = latest
    health.sessions_behind = int(behind or 0)

    health.published = int(
        session.execute(
            text("SELECT count(*) FROM fact_sheet WHERE status = 'published'")
        ).scalar_one()
    )

    health.stale_published = list(
        session.execute(
            text(
                """
                SELECT t.symbol FROM fact_sheet f
                JOIN ticker t ON t.id = f.ticker_id
                WHERE f.status = 'published' AND f.rule_spec_version <> :spec
                ORDER BY t.symbol
                """
            ),
            {"spec": rule_spec_version},
        ).scalars()
    )

    # Only the newest as_of matters: older dates were correctly evaluated under whatever
    # the rules were then, and rewriting history would destroy the flag-transition record.
    health.stale_flag_rows = int(
        session.execute(
            text(
                """
                SELECT count(*) FROM flag_instance
                WHERE as_of_date = (SELECT max(as_of_date) FROM flag_instance)
                  AND rule_spec_version <> :spec
                """
            ),
            {"spec": rule_spec_version},
        ).scalar_one()
    )

    covered, unpublished = session.execute(
        text(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM fact_sheet f
                       WHERE f.ticker_id = t.id AND f.status = 'published'))
            FROM ticker t WHERE t.is_covered
            """
        )
    ).one()
    health.covered = int(covered)
    health.unpublished = int(unpublished)

    return health
