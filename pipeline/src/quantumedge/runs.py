"""Run manifests — every stage records what it did, with what code, against what data.

This is the reproducibility anchor. `git_commit` plus `rule_spec_version` on a run row is
what turns "same ticker + same data produces the same conclusion" from a slogan into
something you can verify by joining two tables.

Shared helper at package root: stages import this, never the reverse.
"""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge import __version__
from quantumedge.db.enums import PipelineStage, RunStatus


def git_commit() -> str:
    """Current HEAD, suffixed `-dirty` when the tree has uncommitted changes.

    The suffix matters: a run produced from modified working files is not reproducible
    from any commit, and the manifest should say so rather than imply a clean provenance.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"

    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def make_run_key(stage: PipelineStage, label: str, when: datetime | None = None) -> str:
    """e.g. 20261002T084312Z:ingest:vndirect_prices — sortable and self-describing."""
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}:{stage.value}:{label}"


def start_run(
    session: Session,
    stage: PipelineStage,
    label: str,
    *,
    source_code: str | None = None,
    as_of: date | None = None,
    params: dict[str, Any] | None = None,
    rule_spec_version: str | None = None,
) -> int:
    """Open a run row and return its id."""
    source_id = None
    if source_code is not None:
        source_id = session.execute(
            text("SELECT id FROM source WHERE code = :c"), {"c": source_code}
        ).scalar_one_or_none()
        if source_id is None:
            raise LookupError(f"unknown source '{source_code}' - add it to seeds/reference.yaml")

    return int(
        session.execute(
            text(
                "INSERT INTO ingest_run (run_key, stage, source_id, as_of_date, status, "
                " git_commit, code_version, rule_spec_version, params) "
                "VALUES (:run_key, :stage, :source_id, :as_of, 'running', :git_commit, "
                " :code_version, :rule_spec_version, CAST(:params AS jsonb)) RETURNING id"
            ),
            {
                "run_key": make_run_key(stage, label),
                "stage": stage.value,
                "source_id": source_id,
                "as_of": as_of,
                "git_commit": git_commit(),
                "code_version": __version__,
                "rule_spec_version": rule_spec_version,
                "params": _json(params or {}),
            },
        ).scalar_one()
    )


def finish_run(
    session: Session,
    run_id: int,
    status: RunStatus,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    session.execute(
        text(
            "UPDATE ingest_run SET status = :status, finished_at = now(), "
            " stats = CAST(:stats AS jsonb), error = :error WHERE id = :id"
        ),
        {
            "id": run_id,
            "status": status.value,
            "stats": _json(stats or {}),
            "error": error,
        },
    )


@contextmanager
def run_scope(
    session: Session,
    stage: PipelineStage,
    label: str,
    *,
    source_code: str | None = None,
    as_of: date | None = None,
    params: dict[str, Any] | None = None,
    rule_spec_version: str | None = None,
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Open a run, hand back (run_id, stats) to fill in, and close it on the way out.

    A raised exception marks the run failed and re-raises, so a half-finished run is
    never left looking like it succeeded.
    """
    run_id = start_run(
        session,
        stage,
        label,
        source_code=source_code,
        as_of=as_of,
        params=params,
        rule_spec_version=rule_spec_version,
    )
    # Commit the open row immediately: if the body fails we roll back, and an uncommitted
    # run row would vanish along with the work - leaving no record that anything was tried.
    session.commit()
    stats: dict[str, Any] = {}
    try:
        yield run_id, stats
    except Exception as exc:
        # Roll back FIRST. A failing statement leaves the transaction aborted, so without
        # this the manifest UPDATE is itself rejected and InFailedSqlTransaction masks the
        # error that actually mattered. The run row survives because it was committed on
        # open, below.
        session.rollback()
        finish_run(session, run_id, RunStatus.FAILED, stats, error=str(exc)[:2000])
        session.commit()
        raise
    finish_run(session, run_id, RunStatus.SUCCEEDED, stats)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
