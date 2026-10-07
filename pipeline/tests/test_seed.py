"""Seeding is the normal way a rule edit reaches the database, so it must converge."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.config import load_settings
from quantumedge.db.seed import rule_spec_version, seed_all


def _counts(session: Session) -> dict[str, int]:
    tables = (
        "exchange",
        "sector",
        "company",
        "ticker",
        "app_user",
        "financial_fact_def",
        "metric_def",
        "flag_def",
    )
    return {t: int(session.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in tables}


def test_seeding_twice_changes_nothing(session: Session) -> None:
    """Invariant 7: re-running a stage converges rather than duplicating."""
    settings = load_settings()
    seed_all(session, settings)
    first = _counts(session)
    seed_all(session, settings)
    assert _counts(session) == first


def test_every_covered_ticker_is_marked_covered(session: Session) -> None:
    settings = load_settings()
    seed_all(session, settings)
    uncovered = (
        session.execute(text("SELECT symbol FROM ticker WHERE NOT is_covered")).scalars().all()
    )
    assert not uncovered, f"coverage.yaml listed these but they are not covered: {uncovered}"


def test_sectors_have_enough_members_for_a_median(session: Session) -> None:
    """A median of one or two peers is not a median; coverage.yaml sets the floor at 3."""
    settings = load_settings()
    seed_all(session, settings)
    thin = session.execute(
        text(
            "SELECT s.code, count(*) AS n FROM ticker t "
            "JOIN company c ON c.id = t.company_id "
            "JOIN sector s ON s.id = c.sector_id "
            "WHERE t.is_covered GROUP BY s.code HAVING count(*) < 3"
        )
    ).all()
    assert not thin, f"sectors below the benchmark sample floor: {thin}"


def test_every_flag_rule_names_its_required_metrics(session: Session) -> None:
    """A threshold rule that declares no inputs cannot report WHICH input was missing,
    so it could never honestly produce `unknown`."""
    settings = load_settings()
    seed_all(session, settings)
    bad = (
        session.execute(
            text(
                "SELECT code FROM flag_def "
                "WHERE rule_kind IN ('metric_threshold', 'metric_vs_benchmark') "
                "AND cardinality(requires_metrics) = 0"
            )
        )
        .scalars()
        .all()
    )
    assert not bad, f"metric rules with no declared inputs: {bad}"


def test_flag_required_metrics_all_exist(session: Session) -> None:
    """A rule referencing a metric that was never defined would silently never fire."""
    settings = load_settings()
    seed_all(session, settings)
    missing = (
        session.execute(
            text(
                "SELECT DISTINCT m FROM flag_def f, unnest(f.requires_metrics) AS m "
                "WHERE m NOT IN (SELECT code FROM metric_def)"
            )
        )
        .scalars()
        .all()
    )
    assert not missing, f"flag rules reference undefined metrics: {missing}"


def test_every_metric_carries_real_glossary_text(session: Session) -> None:
    """`metric_def.definition_vi` is rendered on the ticker page as the glossary entry, so
    a placeholder ships straight to a reader. The floor is deliberately low - a short
    definition can be a complete one ("giá khớp lệnh cuối cùng của phiên gần nhất" needs
    no elaboration) - so what this really catches is a stub left behind by a metric added
    in a hurry."""
    settings = load_settings()
    seed_all(session, settings)
    thin = (
        session.execute(
            text(
                "SELECT code FROM metric_def "
                "WHERE length(trim(definition_vi)) < 40 "
                "   OR definition_vi ILIKE '%todo%' OR definition_vi ILIKE '%tbd%' "
                "   OR definition_vi ILIKE '%placeholder%' "
                "ORDER BY code"
            )
        )
        .scalars()
        .all()
    )
    assert not thin, f"metrics whose glossary text is a placeholder or too short: {thin}"


def test_every_metric_requires_only_defined_fact_codes(session: Session) -> None:
    """A metric declaring a line item the registry never defines can only ever be
    unavailable - an honest-looking row that no extractor improvement will ever fill."""
    settings = load_settings()
    seed_all(session, settings)
    unknown = (
        session.execute(
            text(
                "SELECT DISTINCT f FROM metric_def m, unnest(m.requires_facts) AS f "
                "WHERE f NOT IN (SELECT code FROM financial_fact_def)"
            )
        )
        .scalars()
        .all()
    )
    assert not unknown, f"metrics require fact codes that do not exist: {unknown}"


def test_every_threshold_rule_carries_a_complete_expression(session: Session) -> None:
    """`metric_threshold` is evaluated from three keys. A rule missing one evaluates to
    `unknown` forever, which looks like missing data rather than a broken rule."""
    settings = load_settings()
    seed_all(session, settings)
    incomplete = (
        session.execute(
            text(
                "SELECT code FROM flag_def WHERE rule_kind = 'metric_threshold' "
                "AND NOT (rule_expr ? 'metric' AND rule_expr ? 'op' AND rule_expr ? 'value')"
            )
        )
        .scalars()
        .all()
    )
    assert not incomplete, f"threshold rules missing metric/op/value: {incomplete}"


def test_rule_spec_version_tracks_content(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Editing a rule file must change the version, or staleness detection cannot work."""
    settings = load_settings()
    before = rule_spec_version(settings)

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "flags.yaml").write_text("flags: []\n", encoding="utf-8")
    monkeypatch.setattr(settings, "rules_dir", rules)
    changed = rule_spec_version(settings)
    assert changed != before

    (rules / "flags.yaml").write_text("flags: []\n# an edit\n", encoding="utf-8")
    assert rule_spec_version(settings) != changed
