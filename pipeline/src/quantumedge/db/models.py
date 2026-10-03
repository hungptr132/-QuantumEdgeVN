"""Layer 0 schema — the single source of truth every other module derives from.

Ordered by dataflow: reference → raw ingest → extraction → calc → fact sheet → review.

Conventions, applied without exception:
  * surrogate `bigint` primary keys
  * every timestamp is `timestamptz`, stored UTC
  * money is `numeric(24,4)` — VND trillions overflow lesser types, and float is never
    acceptable for money
  * every derived row records the run that produced it, so "why did this change" is a join
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from quantumedge.db import enums


class Base(DeclarativeBase):
    pass


def _enum(py_enum: type, name: str) -> Enum:
    """Native Postgres enum from a Python StrEnum, stored by value."""
    return Enum(
        py_enum,
        name=name,
        native_enum=True,
        create_type=False,  # created explicitly in the migration, before any table
        values_callable=lambda e: [m.value for m in e],
    )


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ───────────────────────── reference ─────────────────────────


class Exchange(Base):
    """Lookup table, deliberately not an enum: coverage must extend to HNX and UPCOM
    without an ALTER TYPE migration."""

    __tablename__ = "exchange"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Sector(Base):
    __tablename__ = "sector"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_vi: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    icb_code: Mapped[str | None] = mapped_column(Text)
    icb_level: Mapped[int | None] = mapped_column(SmallInteger)
    parent_sector_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sector.id"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    legal_name_vi: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name_en: Mapped[str | None] = mapped_column(Text)
    short_name: Mapped[str] = mapped_column(Text, nullable=False)
    tax_code: Mapped[str | None] = mapped_column(Text, unique=True)
    sector_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sector.id"))
    # Human-written, 2-3 sentences. Never model-generated: the spec reserves prose about
    # a named company for a person.
    description_vi: Mapped[str | None] = mapped_column(Text)
    description_source: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    ir_url: Mapped[str | None] = mapped_column(Text)
    founded_year: Mapped[int | None] = mapped_column(SmallInteger)
    charter_capital_vnd: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class Ticker(Base):
    __tablename__ = "ticker"
    __table_args__ = (
        CheckConstraint(
            "symbol = upper(symbol) AND symbol ~ '^[A-Z0-9]{3,10}$'",
            name="ticker_symbol_format",
        ),
        Index("ticker_covered_idx", "is_covered", postgresql_where=text("is_covered")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("company.id"), nullable=False)
    # Exchange sits on the ticker, not the company: listing venue is a property of the
    # listing, and tickers do move between venues (ROS: HOSE then forced delisting).
    exchange_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("exchange.code"), nullable=False
    )
    status: Mapped[enums.TickerStatus] = mapped_column(
        _enum(enums.TickerStatus, "ticker_status"),
        nullable=False,
        server_default=enums.TickerStatus.LISTED.value,
    )
    listing_date: Mapped[date | None] = mapped_column(Date)
    delisted_date: Mapped[date | None] = mapped_column(Date)
    par_value_vnd: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, server_default=text("10000")
    )
    shares_outstanding: Mapped[int | None] = mapped_column(BigInteger)
    free_float_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    is_covered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    coverage: Mapped[enums.CoverageDepth] = mapped_column(
        _enum(enums.CoverageDepth, "coverage_depth"),
        nullable=False,
        server_default=enums.CoverageDepth.SHALLOW.value,
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class TickerAlias(Base):
    """Feeds Layer 3 search and Agent 3 tip parsing: forum nicknames, diacritic-free
    spellings, common typos all resolve to one ticker."""

    __tablename__ = "ticker_alias"
    __table_args__ = (
        UniqueConstraint("alias_norm", "ticker_id", name="ticker_alias_unique"),
        Index(
            "ticker_alias_trgm",
            "alias_norm",
            postgresql_using="gin",
            postgresql_ops={"alias_norm": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ticker.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_norm: Mapped[str] = mapped_column(Text, nullable=False)
    alias_kind: Mapped[str] = mapped_column(Text, nullable=False)


# ───────────────────────── raw ingest ─────────────────────────


class Source(Base):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[enums.SourceKind] = mapped_column(
        _enum(enums.SourceKind, "source_kind"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    rate_limit_per_min: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("20")
    )
    robots_note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class IngestRun(Base):
    """The run manifest. `git_commit` is the reproducibility anchor — it is what makes
    "same ticker + same data ⇒ same conclusion" an auditable claim."""

    __tablename__ = "ingest_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stage: Mapped[enums.PipelineStage] = mapped_column(
        _enum(enums.PipelineStage, "pipeline_stage"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("source.id"))
    as_of_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[enums.RunStatus] = mapped_column(
        _enum(enums.RunStatus, "run_status"),
        nullable=False,
        server_default=enums.RunStatus.RUNNING.value,
    )
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    git_commit: Mapped[str] = mapped_column(Text, nullable=False)
    code_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_spec_version: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)


class RawDocument(Base):
    """Append-only. A trigger (see migration) rejects UPDATE and DELETE, so
    "ingest raw, never re-fetch, never rewrite" is a property of the system rather
    than a rule someone has to remember.

    The unique key over (source, doc_type, symbol, period_key, content_hash) is the
    idempotency guarantee: re-fetching unchanged content inserts nothing.
    """

    __tablename__ = "raw_document"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "doc_type",
            "symbol",
            "period_key",
            "content_hash",
            name="raw_document_dedupe",
        ),
        CheckConstraint(
            "payload IS NOT NULL OR payload_path IS NOT NULL",
            name="raw_document_has_body",
        ),
        Index("raw_document_lookup_idx", "symbol", "doc_type", text("fetched_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("source.id"), nullable=False)
    ingest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )
    ticker_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ticker.id"))
    # '' sentinel rather than NULL, so the dedupe unique constraint actually applies
    # (NULLs would compare unequal and let duplicates through).
    symbol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    period_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = _ts()
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload_path: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _ts()


class NewsItem(Base):
    """The copyright line encoded as schema: there is deliberately no body column.
    Headline, link, publisher, timestamp — plus our own one-line note, which is the
    only prose about an article we may publish."""

    __tablename__ = "news_item"
    __table_args__ = (CheckConstraint("char_length(headline) <= 400", name="news_headline_len"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )
    ticker_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ticker.id"))
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lang: Mapped[str] = mapped_column(String(2), nullable=False, server_default=text("'vi'"))
    editor_note_vi: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class Filing(Base):
    __tablename__ = "filing"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    raw_document_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("raw_document.id"))
    filing_type: Mapped[str] = mapped_column(Text, nullable=False)
    title_vi: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[enums.ExtractionStatus] = mapped_column(
        _enum(enums.ExtractionStatus, "extraction_status"),
        nullable=False,
        server_default=enums.ExtractionStatus.PENDING.value,
    )
    extracted_text_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class Event(Base):
    """Agent 1's output table, built now and populated later.

    It exists before its producer because `event_presence` and `data_absence` flag rules
    query it. With zero rows those rules evaluate to UNKNOWN with a reason — rendering as
    "không tìm thấy công bố tương ứng" rather than as "no auditor change". That is the
    spec's rule, reachable today. Rows may also be hand-seeded with method='manual'.
    """

    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint(
            "ticker_id", "event_type", "event_date", "source_url", name="event_dedupe"
        ),
        # Copyright: an excerpt, never a body.
        CheckConstraint("char_length(excerpt) <= 500", name="event_excerpt_len"),
        CheckConstraint("num_nonnulls(filing_id, news_item_id) <= 1", name="event_one_origin"),
        Index("event_ticker_type_date_idx", "ticker_id", "event_type", text("event_date DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    event_type: Mapped[enums.EventType] = mapped_column(
        _enum(enums.EventType, "event_type"), nullable=False
    )
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    severity: Mapped[enums.FlagSeverity] = mapped_column(
        _enum(enums.FlagSeverity, "flag_severity"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    filing_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("filing.id"))
    news_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("news_item.id"))
    extraction_method: Mapped[enums.ExtractionMethod] = mapped_column(
        _enum(enums.ExtractionMethod, "extraction_method"),
        nullable=False,
        server_default=enums.ExtractionMethod.MANUAL.value,
    )
    extractor_model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    extracted_at: Mapped[datetime] = _ts()
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


# ───────────────────── deterministic extraction ─────────────────────


class FinancialFactDef(Base):
    """Registry of statement line items, and the glossary the ticker page links to.

    Long-format facts against this registry rather than a wide statement table: source
    column names differ per provider and change without notice, and line items differ by
    sector. A wide table needs a migration per rename; this absorbs it in a mapping file.
    """

    __tablename__ = "financial_fact_def"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label_vi: Mapped[str] = mapped_column(Text, nullable=False)
    label_en: Mapped[str | None] = mapped_column(Text)
    definition_vi: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[enums.UnitCode] = mapped_column(_enum(enums.UnitCode, "unit_code"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    formula_text: Mapped[str | None] = mapped_column(Text)


class FinancialPeriod(Base):
    __tablename__ = "financial_period"
    __table_args__ = (
        # COALESCE in the key so annual periods (no quarter) still dedupe correctly.
        Index(
            "financial_period_unique",
            "ticker_id",
            "period_type",
            "fiscal_year",
            text("COALESCE(fiscal_quarter, 0)"),
            unique=True,
        ),
        CheckConstraint(
            "fiscal_quarter IS NULL OR fiscal_quarter BETWEEN 1 AND 4",
            name="financial_period_quarter_range",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    period_type: Mapped[enums.PeriodType] = mapped_column(
        _enum(enums.PeriodType, "period_type"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(SmallInteger)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL means unknown, never False. "Not known to be audited" and "known to be
    # unaudited" are different claims and the page must be able to say so.
    is_audited: Mapped[bool | None] = mapped_column(Boolean)
    is_restated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source_raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )
    created_at: Mapped[datetime] = _ts()


class FinancialFact(Base):
    """One number, with the exact field it came from.

    `source_field_path` is NOT NULL on purpose: it is what makes the "no unsourced
    numbers" QA rule mechanically checkable instead of aspirational.
    """

    __tablename__ = "financial_fact"
    __table_args__ = (
        UniqueConstraint("financial_period_id", "fact_code", name="financial_fact_unique"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    financial_period_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("financial_period.id", ondelete="CASCADE"), nullable=False
    )
    fact_code: Mapped[str] = mapped_column(
        Text, ForeignKey("financial_fact_def.code"), nullable=False
    )
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))  # NULL = không công bố
    unit: Mapped[enums.UnitCode] = mapped_column(_enum(enums.UnitCode, "unit_code"), nullable=False)
    source_raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )
    source_field_path: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    extract_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )
    created_at: Mapped[datetime] = _ts()


class MarketCalendar(Base):
    """Earns its own table: without it, a zero-trade session is indistinguishable from a
    failed ingest — and that distinction is the whole substance of Agent 5."""

    __tablename__ = "market_calendar"

    exchange_code: Mapped[str] = mapped_column(
        String(10), ForeignKey("exchange.code"), primary_key=True
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class SecurityIdentifier(Base):
    """A ticker's identifiers under external schemes, one row per scheme.

    Long format rather than columns on `ticker`, for the same reason `financial_fact` is
    long: a new scheme is a new row, not a migration. It also keeps provenance per
    identifier — a HOSE internal id and an ISIN come from the same document today, but
    they will not always, and an identifier used as a join key for fetching statements
    needs to name where it came from as much as any figure does.
    """

    __tablename__ = "security_identifier"
    __table_args__ = (
        UniqueConstraint("ticker_id", "scheme", name="security_identifier_unique"),
        CheckConstraint("value <> ''", name="security_identifier_value_nonempty"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    # Free text rather than an enum: schemes accumulate (hnx_security_id, upcom_*), and a
    # closed type would make each addition a migration for no gain in safety.
    scheme: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )
    source_field_path: Mapped[str] = mapped_column(Text, nullable=False)
    extract_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )


class PriceBar(Base):
    __tablename__ = "price_bar"

    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    adjusted_close: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # 0 is meaningful and must never be dropped or interpolated: it is a real
    # zero-trade session. Observed on AGM.
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnover_vnd: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    source_raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )
    extract_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )


class QuoteSnapshot(Base):
    """Bid-ask input for Agent 5. Only obtainable from a live price board, not from
    history endpoints — so until an in-session capture exists this stays empty and
    spread reports as không công bố, which exercises the missing-data path honestly."""

    __tablename__ = "quote_snapshot"
    __table_args__ = (UniqueConstraint("ticker_id", "captured_at", name="quote_snapshot_unique"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    bid_volume: Mapped[int | None] = mapped_column(BigInteger)
    ask_volume: Mapped[int | None] = mapped_column(BigInteger)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ceiling_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    floor_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    source_raw_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_document.id"), nullable=False
    )


# ───────────────────────── calc layer ─────────────────────────


class MetricDef(Base):
    __tablename__ = "metric_def"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label_vi: Mapped[str] = mapped_column(Text, nullable=False)
    label_en: Mapped[str | None] = mapped_column(Text)
    definition_vi: Mapped[str] = mapped_column(Text, nullable=False)
    formula_text: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[enums.UnitCode] = mapped_column(_enum(enums.UnitCode, "unit_code"), nullable=False)
    category: Mapped[enums.MetricCategory] = mapped_column(
        _enum(enums.MetricCategory, "metric_category"), nullable=False
    )
    precision: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("2"))
    requires_facts: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    is_headline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class MetricValue(Base):
    __tablename__ = "metric_value"
    __table_args__ = (
        UniqueConstraint("ticker_id", "metric_code", "as_of_date", name="metric_value_unique"),
        # Either a value, or a stated reason there isn't one. Never silently empty.
        CheckConstraint(
            "(is_available AND value IS NOT NULL) "
            "OR (NOT is_available AND unavailable_reason IS NOT NULL)",
            name="metric_value_consistent",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(Text, ForeignKey("metric_def.code"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    financial_period_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("financial_period.id")
    )
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    # Exact inputs used, so a number can be re-derived without guessing.
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    calc_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )
    calc_version: Mapped[str] = mapped_column(Text, nullable=False)
    computed_at: Mapped[datetime] = _ts()


class MetricBenchmark(Base):
    """Sector median and the stock's own multi-year range.

    `member_ticker_ids` is recorded because at this universe size a sector median is
    three companies — publishing it without being able to say which three would be
    indefensible.
    """

    __tablename__ = "metric_benchmark"
    __table_args__ = (
        Index(
            "metric_benchmark_unique",
            "metric_code",
            "as_of_date",
            "scope",
            text("COALESCE(sector_id, 0)"),
            text("COALESCE(ticker_id, 0)"),
            "window_label",
            unique=True,
        ),
        CheckConstraint(
            "(scope = 'sector' AND sector_id IS NOT NULL) "
            "OR (scope = 'ticker_history' AND ticker_id IS NOT NULL) "
            "OR (scope = 'exchange')",
            name="benchmark_scope_target",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    metric_code: Mapped[str] = mapped_column(Text, ForeignKey("metric_def.code"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    scope: Mapped[enums.BenchmarkScope] = mapped_column(
        _enum(enums.BenchmarkScope, "benchmark_scope"), nullable=False
    )
    sector_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sector.id"))
    ticker_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ticker.id"))
    window_label: Mapped[str] = mapped_column(Text, nullable=False)
    median: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    p25: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    p75: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    min_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    max_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)
    member_ticker_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default=text("'{}'")
    )
    calc_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )


class FlagDef(Base):
    """Loaded from rules/flags.yaml; never hand-edited in SQL. The file's content hash
    becomes `rule_spec_version`, which is what drives regeneration."""

    __tablename__ = "flag_def"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name_vi: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    category: Mapped[enums.FlagCategory] = mapped_column(
        _enum(enums.FlagCategory, "flag_category"), nullable=False
    )
    severity: Mapped[enums.FlagSeverity] = mapped_column(
        _enum(enums.FlagSeverity, "flag_severity"), nullable=False
    )
    rule_kind: Mapped[enums.FlagRuleKind] = mapped_column(
        _enum(enums.FlagRuleKind, "flag_rule_kind"), nullable=False
    )
    rule_expr: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requires_metrics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    on_missing: Mapped[enums.FlagState] = mapped_column(
        _enum(enums.FlagState, "flag_state"),
        nullable=False,
        server_default=enums.FlagState.UNKNOWN.value,
    )
    explanation_vi: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    rule_spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    updated_at: Mapped[datetime] = _ts()


class FlagInstance(Base):
    __tablename__ = "flag_instance"
    __table_args__ = (
        UniqueConstraint("ticker_id", "flag_code", "as_of_date", name="flag_instance_unique"),
        # The defamation guardrail as a constraint: a negative statement about a named
        # company cannot be recorded unless evidence is attached to it.
        CheckConstraint(
            "state <> 'active' OR jsonb_array_length(evidence) > 0",
            name="flag_active_needs_evidence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    flag_code: Mapped[str] = mapped_column(Text, ForeignKey("flag_def.code"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[enums.FlagState] = mapped_column(
        _enum(enums.FlagState, "flag_state"), nullable=False
    )
    severity: Mapped[enums.FlagSeverity] = mapped_column(
        _enum(enums.FlagSeverity, "flag_severity"), nullable=False
    )
    triggered_values: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    calc_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )
    rule_spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    first_triggered_at: Mapped[datetime] = _ts()
    last_evaluated_at: Mapped[datetime] = _ts()


class FlagStateChange(Base):
    """Layer 5 alerting reads this. The spec's insight: the state-change event is more
    interesting than the static snapshot."""

    __tablename__ = "flag_state_change"
    __table_args__ = (
        UniqueConstraint("ticker_id", "flag_code", "changed_on", name="flag_state_change_unique"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    flag_code: Mapped[str] = mapped_column(Text, ForeignKey("flag_def.code"), nullable=False)
    changed_on: Mapped[date] = mapped_column(Date, nullable=False)
    from_state: Mapped[enums.FlagState] = mapped_column(
        _enum(enums.FlagState, "flag_state"), nullable=False
    )
    to_state: Mapped[enums.FlagState] = mapped_column(
        _enum(enums.FlagState, "flag_state"), nullable=False
    )
    detected_at: Mapped[datetime] = _ts()
    calc_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )


# ───────────────────── fact sheet and review ─────────────────────


class AppUser(Base):
    """Not deferrable: `fact_sheet.reviewed_by` needs a real foreign key, because an
    audit stamp pointing at free text is not an audit stamp. Deliberately carries no
    credential columns — there is no auth yet, and Layer 4 will add them without
    disturbing anything above."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'reader'"))
    created_at: Mapped[datetime] = _ts()


class FactSheet(Base):
    """The one artifact every downstream consumer reads.

    Review state lives in columns, outside the hashed payload, so approving a sheet
    cannot change `payload_hash` — otherwise every approval would look like a data change
    and the "did anything actually move?" check would be useless.
    """

    __tablename__ = "fact_sheet"
    __table_args__ = (
        UniqueConstraint("ticker_id", "version", name="fact_sheet_version_unique"),
        CheckConstraint(
            "status <> 'published' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND qa_passed AND published_at IS NOT NULL)",
            name="fs_publish_requires_stamp",
        ),
        CheckConstraint(
            "status <> 'approved' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND qa_passed)",
            name="fs_approve_requires_stamp",
        ),
        CheckConstraint(
            "status <> 'rejected' OR rejected_reason IS NOT NULL",
            name="fs_reject_requires_reason",
        ),
        # At most one published sheet per ticker, enforced by the database.
        Index(
            "fact_sheet_one_published_per_ticker",
            "ticker_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[enums.FactSheetStatus] = mapped_column(
        _enum(enums.FactSheetStatus, "fact_sheet_status"),
        nullable=False,
        server_default=enums.FactSheetStatus.DRAFT.value,
    )
    qa_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    qa_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewed_by: Mapped[str | None] = mapped_column(Text, ForeignKey("app_user.slug"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    position_disclosure: Mapped[str | None] = mapped_column(Text)
    calc_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingest_run.id"), nullable=False
    )
    rule_spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    git_commit: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()


class FactSheetReviewLog(Base):
    """Every transition, including rejections. The spec is explicit that the rejection
    log is how the process improves."""

    __tablename__ = "fact_sheet_review_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fact_sheet_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fact_sheet.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[enums.FactSheetStatus] = mapped_column(
        _enum(enums.FactSheetStatus, "fact_sheet_status"), nullable=False
    )
    to_status: Mapped[enums.FactSheetStatus] = mapped_column(
        _enum(enums.FactSheetStatus, "fact_sheet_status"), nullable=False
    )
    actor: Mapped[str] = mapped_column(Text, ForeignKey("app_user.slug"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    qa_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts()


class Watchlist(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "name", name="watchlist_unique"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    created_at: Mapped[datetime] = _ts()


class WatchlistItem(Base):
    __tablename__ = "watchlist_item"

    watchlist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("watchlist.id", ondelete="CASCADE"), primary_key=True
    )
    ticker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ticker.id"), primary_key=True)
    added_at: Mapped[datetime] = _ts()
