"""Persist fact sheets with hash-gated versioning.

The behaviour that matters: an identical rebuild writes nothing and **preserves the existing
review stamp**. Without that, every re-run would invalidate human review and a rule change
would silently alter what the public sees. With it, a rebuild that genuinely changes nothing
is a no-op, and one that does change something produces a new draft version requiring fresh
review — which is the correct cost.
"""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.factsheet.qa import validate
from quantumedge.factsheet.schema import FactSheet
from quantumedge.hashing import hash_payload
from quantumedge.runs import git_commit

log = structlog.get_logger(__name__)


@dataclass
class StoreOutcome:
    symbol: str
    fact_sheet_id: int
    version: int
    created: bool
    qa_passed: bool
    payload_hash: str
    note: str = ""


def payload_of(sheet: FactSheet) -> dict[str, Any]:
    """JSON-ready payload. `generated_at` is excluded from hashing (below), not from the
    payload: readers want to know when it was built."""
    payload: dict[str, Any] = json.loads(sheet.model_dump_json())
    return payload


# Fields that change on every build regardless of the data, and so must not contribute to
# the content hash. Including any of them makes every rebuild look like a data change, which
# silently defeats hash gating and destroys a review stamp on every run.
VOLATILE_TOP_LEVEL = ("generated_at",)
# Run metadata, excluded from the content hash. `rule_spec_version` belongs here for a
# less obvious reason than the rest: it is *provenance*, not content.
#
# It is already stored in `fact_sheet.rule_spec_version`, a column of its own, so nothing
# is lost by keeping it out of the hash. Leaving it in meant that editing one threshold
# changed the payload of every ticker - so a rule affecting three companies created nine
# new drafts, discarded nine review stamps, and made `git diff factsheets/` useless for
# seeing what actually moved. At eighty tickers that is not a diff anyone reads.
#
# A sheet whose figures, flags and gaps are identical says the same thing regardless of
# which rules file version computed it. It stays the same version, and keeps its stamp.
VOLATILE_LINEAGE = ("git_commit", "calc_run_id", "code_version", "rule_spec_version")


def hash_of(payload: dict[str, Any]) -> str:
    """Hash the CONTENT of a sheet: its figures, flags, gaps and identity.

    Deliberately excludes run metadata. `generated_at` moves every second, `calc_run_id`
    every build, and `git_commit` on every commit - none of which says anything about
    whether the numbers changed.
    """
    material = {k: v for k, v in payload.items() if k not in VOLATILE_TOP_LEVEL}
    material["lineage"] = {
        k: v for k, v in (material.get("lineage") or {}).items() if k not in VOLATILE_LINEAGE
    }
    return hash_payload(material)


def store_fact_sheet(
    session: Session,
    sheet: FactSheet,
    calc_run_id: int,
    *,
    as_of: date | None = None,
) -> StoreOutcome:
    symbol = sheet.ticker.symbol
    as_of_date = as_of or sheet.as_of_date

    ticker_id = session.execute(
        text("SELECT id FROM ticker WHERE symbol = :s"), {"s": symbol}
    ).scalar_one()

    payload = payload_of(sheet)
    digest = hash_of(payload)
    report = validate(session, sheet, symbol)

    latest = (
        session.execute(
            text(
                "SELECT id, version, payload_hash, status FROM fact_sheet "
                "WHERE ticker_id = :t ORDER BY version DESC LIMIT 1"
            ),
            {"t": ticker_id},
        )
        .mappings()
        .first()
    )

    if latest and latest["payload_hash"] == digest:
        # Nothing moved. Leave the row, and its review stamp, exactly as they are.
        log.info("factsheet.unchanged", symbol=symbol, version=latest["version"])
        return StoreOutcome(
            symbol=symbol,
            fact_sheet_id=latest["id"],
            version=latest["version"],
            created=False,
            qa_passed=report.passed,
            payload_hash=digest,
            note=f"unchanged (status={latest['status']})",
        )

    version = (latest["version"] + 1) if latest else 1
    new_id = int(
        session.execute(
            text(
                """
                INSERT INTO fact_sheet
                    (ticker_id, as_of_date, version, schema_version, payload, payload_hash,
                     status, qa_passed, qa_report, calc_run_id, rule_spec_version, git_commit)
                VALUES
                    (:t, :as_of, :version, :schema_version, CAST(:payload AS jsonb), :hash,
                     'draft', :qa_passed, CAST(:qa_report AS jsonb), :run, :spec, :commit)
                RETURNING id
                """
            ),
            {
                "t": ticker_id,
                "as_of": as_of_date,
                "version": version,
                "schema_version": sheet.schema_version,
                "payload": json.dumps(payload, ensure_ascii=False),
                "hash": digest,
                "qa_passed": report.passed,
                "qa_report": json.dumps(report.as_dict(), ensure_ascii=False),
                "run": calc_run_id,
                "spec": sheet.lineage.rule_spec_version,
                "commit": git_commit(),
            },
        ).scalar_one()
    )

    log.info("factsheet.stored", symbol=symbol, version=version, qa_passed=report.passed)
    return StoreOutcome(
        symbol=symbol,
        fact_sheet_id=new_id,
        version=version,
        created=True,
        qa_passed=report.passed,
        payload_hash=digest,
        note="new draft" if version == 1 else f"v{version} (content changed)",
    )
