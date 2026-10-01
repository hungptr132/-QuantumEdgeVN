"""Closed sets, defined once here and materialised as native Postgres enums.

The spec is emphatic that free-text categories break everything downstream, so these
are deliberately rigid: adding a value requires a migration, which is the point.

The one exception is `exchange`, which is a lookup *table* rather than an enum — the
USP spans HOSE, HNX and UPCOM, and extending a table does not need ALTER TYPE.
"""

from enum import StrEnum


class TickerStatus(StrEnum):
    LISTED = "listed"
    WARNING = "warning"
    CONTROL = "control"
    SUSPENDED = "suspended"
    DELISTED = "delisted"


class CoverageDepth(StrEnum):
    """The spec's barbell: shallow automated breadth, deep hand-reviewed flagships."""

    SHALLOW = "shallow"
    DEEP = "deep"


class SourceKind(StrEnum):
    MARKET_DATA = "market_data"
    RSS = "rss"
    DISCLOSURE = "disclosure"
    IR = "ir"
    MANUAL = "manual"


class PipelineStage(StrEnum):
    INGEST = "ingest"
    EXTRACT = "extract"
    CALC = "calc"
    FACTSHEET = "factsheet"
    REVIEW = "review"
    PUBLISH = "publish"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class PeriodType(StrEnum):
    QUARTER = "Q"
    HALF = "H"
    YEAR = "Y"
    TTM = "TTM"


class UnitCode(StrEnum):
    VND = "VND"
    SHARE = "share"
    PCT = "pct"
    RATIO = "ratio"
    TIMES = "x"
    SESSION = "session"
    DAY = "day"
    COUNT = "count"


class MetricCategory(StrEnum):
    VALUATION = "valuation"
    LEVERAGE = "leverage"
    PROFITABILITY = "profitability"
    GROWTH = "growth"
    LIQUIDITY_MARKET = "liquidity_market"
    LIQUIDITY_BALANCE_SHEET = "liquidity_balance_sheet"
    OWNERSHIP = "ownership"
    PRICE = "price"


class FlagCategory(StrEnum):
    LEVERAGE = "leverage"
    LIQUIDITY = "liquidity"
    DILUTION = "dilution"
    GOVERNANCE = "governance"
    DISCLOSURE = "disclosure"
    PROFITABILITY = "profitability"
    VALUATION = "valuation"


class FlagSeverity(StrEnum):
    INFO = "info"
    WATCH = "watch"
    HIGH = "high"


class FlagState(StrEnum):
    """Three-valued on purpose.

    A boolean cannot distinguish "the rule did not fire" from "the inputs were missing",
    which would make absent data render as *clean* — the exact failure the spec forbids.
    UNKNOWN is what makes a page say "không tìm thấy công bố tương ứng".
    """

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class FlagRuleKind(StrEnum):
    METRIC_THRESHOLD = "metric_threshold"
    METRIC_VS_BENCHMARK = "metric_vs_benchmark"
    EVENT_PRESENCE = "event_presence"
    DATA_ABSENCE = "data_absence"
    COMPOSITE = "composite"


class BenchmarkScope(StrEnum):
    SECTOR = "sector"
    TICKER_HISTORY = "ticker_history"
    EXCHANGE = "exchange"


class ExtractionMethod(StrEnum):
    LLM_AGENT1 = "llm_agent1"
    MANUAL = "manual"
    RULE = "rule"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"
    SKIPPED = "skipped"


class FactSheetStatus(StrEnum):
    """SUPERSEDED is required as a terminal state for a previously published version.

    Without it, the one-published-per-ticker unique index cannot be satisfied when a
    newer version publishes, except by destroying the older row's review stamp.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EventType(StrEnum):
    """The spec's closed enum, verbatim. Agent 1 may emit nothing outside this set."""

    DILUTION = "dilution"
    AUDITOR_CHANGE = "auditor_change"
    INSIDER_SALE = "insider_sale"
    RELATED_PARTY = "related_party"
    PROFIT_WARNING = "profit_warning"
    BOARD_CHANGE = "board_change"
    SHARE_PLEDGE = "share_pledge"
    NONE = "none"
