"""The review state machine.

The "reviewed by X on Y" stamp is the product. Automating the drafting is fine; automating
the verifying is the thing that would destroy the trust being sold, so the only way a sheet
reaches `published` is a person naming themselves here.

Legal transitions:

    draft ──submit──> in_review ──approve──> approved ──publish──> published
      │                   │                                            │
      └───────────────────┴──────reject──────> rejected          superseded

Every transition is logged, rejections included — the spec is explicit that the rejection
log is how the process improves.
"""

import json
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import FactSheetStatus

log = structlog.get_logger(__name__)

LEGAL: dict[FactSheetStatus, set[FactSheetStatus]] = {
    FactSheetStatus.DRAFT: {FactSheetStatus.APPROVED, FactSheetStatus.REJECTED},
    FactSheetStatus.APPROVED: {FactSheetStatus.PUBLISHED, FactSheetStatus.REJECTED},
    FactSheetStatus.PUBLISHED: {FactSheetStatus.SUPERSEDED},
    FactSheetStatus.REJECTED: {FactSheetStatus.DRAFT},
    FactSheetStatus.SUPERSEDED: set(),
}


class ReviewError(RuntimeError):
    pass


@dataclass
class ReviewResult:
    fact_sheet_id: int
    symbol: str
    version: int
    from_status: str
    to_status: str


def _current(session: Session, symbol: str, version: int | None) -> dict[str, Any]:
    clause = "AND fs.version = :v" if version else ""
    row = (
        session.execute(
            text(
                f"""
            SELECT fs.id, fs.version, fs.status, fs.qa_passed, t.symbol
            FROM fact_sheet fs JOIN ticker t ON t.id = fs.ticker_id
            WHERE t.symbol = :s {clause}
            ORDER BY fs.version DESC LIMIT 1
            """
            ),
            {"s": symbol, "v": version},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ReviewError(f"no fact sheet for {symbol}" + (f" v{version}" if version else ""))
    return dict(row)


def _log(
    session: Session,
    fact_sheet_id: int,
    from_status: str,
    to_status: str,
    actor: str,
    note: str | None,
) -> None:
    session.execute(
        text(
            "INSERT INTO fact_sheet_review_log "
            " (fact_sheet_id, from_status, to_status, actor, note) "
            "VALUES (:fs, CAST(:f AS fact_sheet_status), CAST(:t AS fact_sheet_status), :a, :n)"
        ),
        {"fs": fact_sheet_id, "f": from_status, "t": to_status, "a": actor, "n": note},
    )


def _require_reviewer(session: Session, slug: str) -> None:
    """The stamp needs a real person. A free-text name would not be an audit trail."""
    exists = session.execute(
        text("SELECT exists (SELECT 1 FROM app_user WHERE slug = :s)"), {"s": slug}
    ).scalar_one()
    if not exists:
        raise ReviewError(f"unknown reviewer '{slug}' - add them to seeds/reference.yaml")


def _check_transition(current: str, target: FactSheetStatus) -> None:
    allowed = LEGAL[FactSheetStatus(current)]
    if target not in allowed:
        raise ReviewError(
            f"illegal transition {current} -> {target.value} "
            f"(allowed: {', '.join(sorted(a.value for a in allowed)) or 'none'})"
        )


def approve(
    session: Session,
    symbol: str,
    reviewer: str,
    *,
    note: str | None = None,
    version: int | None = None,
) -> ReviewResult:
    row = _current(session, symbol, version)
    _require_reviewer(session, reviewer)
    _check_transition(row["status"], FactSheetStatus.APPROVED)

    # Machine QA gates human review rather than replacing it: a reviewer should spend their
    # attention on judgement, not on catching an unsourced number a regex can find.
    if not row["qa_passed"]:
        raise ReviewError(
            f"{symbol} v{row['version']} failed machine QA - inspect qa_report before approving"
        )

    session.execute(
        text(
            "UPDATE fact_sheet SET status = 'approved', reviewed_by = :r, reviewed_at = now(), "
            " review_note = :n WHERE id = :i"
        ),
        {"r": reviewer, "n": note, "i": row["id"]},
    )
    _log(session, row["id"], row["status"], "approved", reviewer, note)
    log.info("review.approved", symbol=symbol, version=row["version"], reviewer=reviewer)
    return ReviewResult(row["id"], symbol, row["version"], row["status"], "approved")


def publish(
    session: Session, symbol: str, actor: str, *, version: int | None = None
) -> ReviewResult:
    """Publishing names a person, not "system".

    The review log's actor is a foreign key to app_user, which is the schema refusing to let
    a publication be attributed to nobody. That is the right constraint: putting a page in
    front of readers is an act someone performs, and the stamp is the product.
    """
    row = _current(session, symbol, version)
    _require_reviewer(session, actor)
    _check_transition(row["status"], FactSheetStatus.PUBLISHED)

    # Exactly one published version per ticker, so the previous one is superseded first.
    # Its review stamp is left intact — superseding is not unreviewing.
    previous = (
        session.execute(
            text(
                "SELECT fs.id, fs.version FROM fact_sheet fs JOIN ticker t ON t.id = fs.ticker_id "
                "WHERE t.symbol = :s AND fs.status = 'published'"
            ),
            {"s": symbol},
        )
        .mappings()
        .first()
    )
    if previous:
        session.execute(
            text("UPDATE fact_sheet SET status = 'superseded' WHERE id = :i"),
            {"i": previous["id"]},
        )
        # Attributed to whoever is publishing: they caused the supersede. The older row
        # keeps its own reviewed_by, because superseding is not unreviewing.
        _log(
            session,
            previous["id"],
            "published",
            "superseded",
            actor,
            f"replaced by v{row['version']}",
        )

    session.execute(
        text("UPDATE fact_sheet SET status = 'published', published_at = now() WHERE id = :i"),
        {"i": row["id"]},
    )
    _log(session, row["id"], row["status"], "published", actor, None)
    log.info("review.published", symbol=symbol, version=row["version"])
    return ReviewResult(row["id"], symbol, row["version"], row["status"], "published")


def reject(
    session: Session, symbol: str, reviewer: str, reason: str, *, version: int | None = None
) -> ReviewResult:
    if not reason.strip():
        raise ReviewError("a rejection must state a reason - the log is how the process improves")
    row = _current(session, symbol, version)
    _require_reviewer(session, reviewer)
    _check_transition(row["status"], FactSheetStatus.REJECTED)

    session.execute(
        text(
            "UPDATE fact_sheet SET status = 'rejected', rejected_reason = :why, "
            " reviewed_by = :r, reviewed_at = now() WHERE id = :i"
        ),
        {"why": reason, "r": reviewer, "i": row["id"]},
    )
    _log(session, row["id"], row["status"], "rejected", reviewer, reason)
    log.info("review.rejected", symbol=symbol, version=row["version"], reviewer=reviewer)
    return ReviewResult(row["id"], symbol, row["version"], row["status"], "rejected")


def queue(
    session: Session, status: str | None = None, *, all_versions: bool = False
) -> list[dict[str, Any]]:
    """What needs a decision.

    By default this shows only what is actionable: the newest version per ticker, plus
    anything currently approved or published. Superseded versions and older drafts are
    history, not work - listing every version turns a nine-ticker queue into dozens of rows
    and buries the one sheet actually waiting on a person. Pass all_versions to see them.
    """
    filters = []
    params: dict[str, Any] = {}
    if status:
        filters.append("fs.status = CAST(:st AS fact_sheet_status)")
        params["st"] = status
    if not all_versions:
        filters.append(
            "(fs.version = (SELECT max(f2.version) FROM fact_sheet f2 "
            " WHERE f2.ticker_id = fs.ticker_id) "
            " OR fs.status IN ('approved', 'published'))"
        )
    clause = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = session.execute(
        text(
            f"""
            SELECT t.symbol, fs.version, fs.status, fs.qa_passed, fs.as_of_date,
                   fs.reviewed_by, fs.reviewed_at,
                   jsonb_array_length(coalesce(fs.payload -> 'flags', '[]'::jsonb)) AS flags,
                   coalesce(fs.qa_report -> 'failed', '[]'::jsonb) AS qa_failed
            FROM fact_sheet fs JOIN ticker t ON t.id = fs.ticker_id
            {clause}
            ORDER BY fs.status, t.symbol, fs.version DESC
            """  # noqa: S608
        ),
        params,
    ).mappings()
    return [dict(r) | {"qa_failed": json.loads(json.dumps(r["qa_failed"]))} for r in rows]
