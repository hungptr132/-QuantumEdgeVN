"""Write published-ready fact sheets to disk, under version control.

This is the piece that makes the reproducibility claim checkable rather than asserted.
Sheets live in Postgres as the source of truth, but a committed JSON snapshot per ticker
means that changing a flag threshold produces a **readable diff of exactly which numbers
moved on which pages**, before anything is published.

That diff is two things at once: the review artifact a person looks at, and the regression
baseline. `git diff factsheets/` after a rule change is the whole M13 proof.

Volatile fields are stripped. A snapshot that changed because `generated_at` moved would
make every run look like a data change and the diff would be worthless — the same reasoning
as the content hash in `store.py`, applied to the file on disk.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.factsheet.store import VOLATILE_LINEAGE, VOLATILE_TOP_LEVEL

log = structlog.get_logger(__name__)


@dataclass
class ExportOutcome:
    written: int = 0
    unchanged: int = 0
    paths: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        return {"snapshots_written": self.written, "snapshots_unchanged": self.unchanged}


def stable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with run metadata removed, so a diff shows only real changes."""
    out = {k: v for k, v in payload.items() if k not in VOLATILE_TOP_LEVEL}
    if "lineage" in out and isinstance(out["lineage"], dict):
        out["lineage"] = {k: v for k, v in out["lineage"].items() if k not in VOLATILE_LINEAGE}
    return out


def export_snapshots(session: Session, out_dir: Path) -> ExportOutcome:
    """One file per ticker's current sheet: factsheets/<SYMBOL>/<as_of>__v<n>.json.

    Every version is kept rather than overwritten, so the history of what a page said on a
    given date stays inspectable — which is what "same data, same conclusion" needs to be
    verifiable after the fact.
    """
    rows = (
        session.execute(
            text(
                """
                SELECT t.symbol, fs.as_of_date, fs.version, fs.status, fs.payload
                FROM fact_sheet fs
                JOIN ticker t ON t.id = fs.ticker_id
                WHERE fs.status <> 'rejected'
                ORDER BY t.symbol, fs.version
                """
            )
        )
        .mappings()
        .all()
    )

    outcome = ExportOutcome()
    for row in rows:
        folder = out_dir / row["symbol"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{row['as_of_date']}__v{row['version']}.json"

        # Sorted keys and a trailing newline: a snapshot is only useful as a diff if its
        # formatting is identical every time.
        content = (
            json.dumps(
                stable_payload(row["payload"]),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

        if path.exists() and path.read_text(encoding="utf-8") == content:
            outcome.unchanged += 1
            continue

        path.write_text(content, encoding="utf-8")
        outcome.written += 1
        outcome.paths.append(str(path.relative_to(out_dir)))

    log.info("factsheet.export", written=outcome.written, unchanged=outcome.unchanged)
    return outcome
