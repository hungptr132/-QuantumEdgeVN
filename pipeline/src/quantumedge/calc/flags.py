"""Apply flag rules to tickers and persist the outcome, including state transitions.

Every rule produces a row for every covered ticker, including `inactive` and `unknown`
ones. That is deliberate: the set of rules that were *considered* is part of the record. A
page that shows three flags should be able to say how many rules were checked to find them,
and an absent row could not distinguish "did not fire" from "was never evaluated".
"""

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.rule_engine import RuleContext, evaluate
from quantumedge.db.enums import FlagState

log = structlog.get_logger(__name__)


@dataclass
class FlagOutcome:
    written: int = 0
    active: int = 0
    inactive: int = 0
    unknown: int = 0
    transitions: int = 0
    notes: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "flags_written": self.written,
            "active": self.active,
            "inactive": self.inactive,
            "unknown": self.unknown,
            "state_changes": self.transitions,
        }
        if self.notes:
            stats["active_flags"] = self.notes[:40]
        return stats


def _rules(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT code, category, severity, rule_kind, rule_expr, requires_metrics, "
            "       on_missing, rule_version, rule_spec_version "
            "FROM flag_def WHERE is_active ORDER BY code"
        )
    ).mappings()
    return [dict(r) for r in rows]


def _tickers(session: Session, symbols: list[str] | None) -> list[dict[str, Any]]:
    clause = "AND symbol = ANY(:symbols)" if symbols else ""
    params: dict[str, Any] = {"symbols": symbols} if symbols else {}
    rows = session.execute(
        text(f"SELECT id, symbol FROM ticker WHERE is_covered {clause} ORDER BY symbol"),
        params,
    ).mappings()
    return [dict(r) for r in rows]


class StaleRuleDefinitions(RuntimeError):
    """`flag_def` does not match `rules/*.yaml`, so evaluating would use old thresholds."""


def _refuse_if_definitions_are_stale(session: Session) -> None:
    """Refuse to evaluate flags against rule definitions the YAML has moved past.

    `flag_def` is a mirror of `rules/flags.yaml`, refreshed only by `qe db seed`. Nothing
    in the calc stage reloads it, and `flag_instance` inherits its `rule_spec_version` from
    the row it was evaluated against — so editing a threshold and running `qe calc flags`
    produced flags computed from the OLD threshold, stamped with the OLD version, and no
    error anywhere.

    It was visible only because `qe doctor` reported the flags stale *after* a recompute
    that was supposed to clear it, and the remediation it printed (`qe calc flags`) could
    not have worked. `qe run` did not seed either.

    Refusing rather than auto-seeding: seeding writes to reference tables, which is not
    the calc stage's business, and a silent reload would hide that the rules changed from
    whoever is about to look at the output.
    """
    from quantumedge.config import load_settings  # noqa: PLC0415
    from quantumedge.db.seed import rule_spec_version  # noqa: PLC0415

    expected = rule_spec_version(load_settings())
    stored = sorted(
        {
            str(v)
            for v in session.execute(
                text("SELECT DISTINCT rule_spec_version FROM flag_def WHERE is_active")
            ).scalars()
        }
    )
    if stored and stored != [expected]:
        raise StaleRuleDefinitions(
            "flag_def was loaded from a different rules/*.yaml than the one on disk "
            f"(stored {', '.join(s[:12] for s in stored)}…, expected {expected[:12]}…). "
            "Evaluating now would apply the old thresholds and stamp the result with the "
            "old version. Run `qe db seed` first."
        )


def evaluate_flags(
    session: Session,
    calc_run_id: int,
    as_of: date,
    *,
    symbols: list[str] | None = None,
) -> FlagOutcome:
    outcome = FlagOutcome()
    rules = _rules(session)
    if not rules:
        raise LookupError("no active flag rules - run: qe db seed")

    _refuse_if_definitions_are_stale(session)

    for ticker in _tickers(session, symbols):
        ctx = RuleContext(
            session=session, ticker_id=ticker["id"], symbol=ticker["symbol"], as_of=as_of
        )
        for rule in rules:
            result = evaluate(ctx, rule)

            # `on_missing` lets a rule choose what an absent input means. It defaults to
            # unknown; a rule may never map it to `inactive`, because that would present
            # missing data as a clean result.
            if result.state is FlagState.UNKNOWN and rule["on_missing"] != FlagState.UNKNOWN.value:
                log.warning(
                    "calc.flags.on_missing_ignored",
                    flag=rule["code"],
                    requested=rule["on_missing"],
                )

            previous = session.execute(
                text(
                    "SELECT state FROM flag_instance WHERE ticker_id = :t AND flag_code = :c "
                    "AND as_of_date < CAST(:d AS date) ORDER BY as_of_date DESC LIMIT 1"
                ),
                {"t": ticker["id"], "c": rule["code"], "d": as_of},
            ).scalar_one_or_none()

            session.execute(
                text(
                    """
                    INSERT INTO flag_instance
                        (ticker_id, flag_code, as_of_date, state, severity, triggered_values,
                         evidence, reason, calc_run_id, rule_spec_version)
                    VALUES
                        (:ticker_id, :code, :as_of, CAST(:state AS flag_state),
                         CAST(:severity AS flag_severity), CAST(:triggered AS jsonb),
                         CAST(:evidence AS jsonb), :reason, :run, :spec)
                    ON CONFLICT (ticker_id, flag_code, as_of_date) DO UPDATE SET
                        state = EXCLUDED.state,
                        severity = EXCLUDED.severity,
                        triggered_values = EXCLUDED.triggered_values,
                        evidence = EXCLUDED.evidence,
                        reason = EXCLUDED.reason,
                        calc_run_id = EXCLUDED.calc_run_id,
                        rule_spec_version = EXCLUDED.rule_spec_version,
                        last_evaluated_at = now()
                    """
                ),
                {
                    "ticker_id": ticker["id"],
                    "code": rule["code"],
                    "as_of": as_of,
                    "state": result.state.value,
                    "severity": rule["severity"],
                    "triggered": json.dumps(result.triggered_values, ensure_ascii=False),
                    "evidence": json.dumps(result.evidence, ensure_ascii=False),
                    "reason": result.reason,
                    "run": calc_run_id,
                    "spec": rule["rule_spec_version"],
                },
            )
            outcome.written += 1

            if result.state is FlagState.ACTIVE:
                outcome.active += 1
                outcome.notes.append(f"{ticker['symbol']}: {rule['code']}")
            elif result.state is FlagState.INACTIVE:
                outcome.inactive += 1
            else:
                outcome.unknown += 1

            # A transition is what the alerting layer will care about: the spec's own point
            # is that the state-change event is more interesting than the snapshot.
            if previous is not None and previous != result.state.value:
                session.execute(
                    text(
                        "INSERT INTO flag_state_change (ticker_id, flag_code, changed_on, "
                        " from_state, to_state, calc_run_id) "
                        "VALUES (:t, :c, :d, CAST(:f AS flag_state), CAST(:to AS flag_state), :r) "
                        "ON CONFLICT (ticker_id, flag_code, changed_on) DO NOTHING"
                    ),
                    {
                        "t": ticker["id"],
                        "c": rule["code"],
                        "d": as_of,
                        "f": previous,
                        "to": result.state.value,
                        "r": calc_run_id,
                    },
                )
                outcome.transitions += 1

    log.info(
        "calc.flags",
        written=outcome.written,
        active=outcome.active,
        inactive=outcome.inactive,
        unknown=outcome.unknown,
        transitions=outcome.transitions,
    )
    return outcome
