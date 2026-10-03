"""The Fact Sheet contract — one artifact, read by the web app and by every future agent.

Two design rules carry most of the weight:

**Every number is a `Figure`, never a bare float.** A Figure carries its own label,
glossary definition, pre-formatted display string, availability, and provenance. That is
what lets the web app render a page without a single formatting or business decision of its
own — it cannot disagree with the pipeline because it is given no opportunity to compute.

**`allowed_numbers` is built now, with no model present.** It is the flat set of numeric
strings that legitimately appear anywhere in this sheet. When Agent 2 eventually writes
Vietnamese prose from a fact sheet, "reject any draft containing a number that is not in
this set" becomes a function call rather than a project. It costs a few lines today and the
check gets test coverage before it has a customer.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "factsheet/v1"

NOT_DISCLOSED = "không công bố"
NO_DISCLOSURE_FOUND = "không tìm thấy công bố tương ứng"


class Strict(BaseModel):
    """Forbid unknown fields and freeze instances.

    `extra="forbid"` matters: a typo in a field name should fail loudly here rather than
    silently produce a sheet missing a section the page expects.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(Strict):
    kind: Literal[
        "metric", "benchmark", "price_bar", "financial_fact", "event", "filing", "news", "manual"
    ]
    ref: str
    label: str
    source_url: str | None = None


class BenchmarkRef(Strict):
    """One distribution a figure can be read against.

    Carries `members` — the symbols that produced a sector median — because at this
    universe size the median is three companies, and a comparison a reader cannot audit
    is a comparison they have to take on trust.
    """

    scope: Literal["sector", "ticker_history", "exchange"]
    window_label: str
    label_vi: str
    median: Decimal | None
    p25: Decimal | None
    p75: Decimal | None
    min_value: Decimal | None
    max_value: Decimal | None
    display_median: str
    display_range: str
    sample_size: int
    available: bool
    unavailable_reason: str | None = None
    members: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)


class Comparison(Strict):
    """Where a figure sits relative to its peers and its own past.

    Every field here is **positional, never evaluative**. `above` means a larger number,
    not a better company: turnover above the sector median is more liquid, while a price
    above par is neither good nor bad on its own. The product describes; the reader judges.
    That is a licensing boundary as much as an editorial one.
    """

    sector: BenchmarkRef | None = None
    own_history: list[BenchmarkRef] = Field(default_factory=list)

    # Banded against the interquartile range rather than the median alone, so a figure a
    # hair above the midpoint is not announced as "above the sector" — with a sample of
    # three, that would be noise reported as a finding.
    position_vs_sector: Literal["above", "below", "in_line", "unknown"] = "unknown"
    position_vs_sector_label_vi: str = "không công bố"

    position_vs_own_range: Literal["near_high", "near_low", "mid_range", "unknown"] = "unknown"
    position_vs_own_range_label_vi: str = "không công bố"


class Figure(Strict):
    """One number, or an explicit statement that there isn't one."""

    code: str
    label_vi: str
    definition_vi: str
    value: Decimal | None
    unit: str
    display: str
    available: bool
    unavailable_reason: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    # Optional because not every figure has peers worth comparing against: a count of
    # missing bars has no meaningful sector median, and inventing one would be noise.
    comparison: Comparison | None = None


class FlagItem(Strict):
    code: str
    name_vi: str
    category: str
    severity: Literal["info", "watch", "high"]
    state: Literal["active", "inactive", "unknown"]
    state_label_vi: str
    explanation_vi: str
    rule_summary: str
    triggered_values: dict[str, str] = Field(default_factory=dict)
    evidence: list[Provenance] = Field(default_factory=list)
    reason: str | None = None


class ExitEstimate(Strict):
    position_label: str
    sessions: str


class LiquidityBlock(Strict):
    """Agent 5's output. `framing_note_vi` travels with it so the numbers cannot be
    presented as a trading signal by omission."""

    figures: list[Figure]
    exit_estimates: list[ExitEstimate] = Field(default_factory=list)
    participation_rate: str | None = None
    framing_note_vi: str


class DisclosureGap(Strict):
    """Something a complete page would show and this one cannot.

    Gaps are first-class content, not an error state. The spec is explicit that saying what
    is missing is part of the value.
    """

    field: str
    label_vi: str
    reason: str
    display: str = NOT_DISCLOSED


class TickerRef(Strict):
    symbol: str
    exchange: str
    status: str
    listing_date: date | None = None
    par_value_vnd: Decimal
    shares_outstanding: int | None = None
    coverage: Literal["shallow", "deep"]


class CompanyRef(Strict):
    legal_name_vi: str
    short_name: str
    sector_code: str | None = None
    sector_name_vi: str | None = None
    # None by decision: the schema reserves this prose for a person, so it surfaces as a
    # disclosure gap until a human writes it.
    description_vi: str | None = None
    website: str | None = None
    ir_url: str | None = None


class Lineage(Strict):
    """Enough to answer "why did this number change" with a join rather than a guess."""

    rule_spec_version: str
    calc_run_id: int
    git_commit: str
    code_version: str
    data_sources: list[str]
    as_of_date: date


class FactSheet(Strict):
    schema_version: Literal["factsheet/v1"] = "factsheet/v1"
    as_of_date: date
    generated_at: datetime

    ticker: TickerRef
    company: CompanyRef

    headline_metrics: list[Figure]
    other_metrics: list[Figure] = Field(default_factory=list)
    liquidity: LiquidityBlock
    flags: list[FlagItem]

    # Empty until Agent 1 exists. Present in the contract so the page and its types do not
    # change shape when events start arriving.
    recent_events: list[Provenance] = Field(default_factory=list)
    news: list[Provenance] = Field(default_factory=list)

    disclosure_gaps: list[DisclosureGap]
    what_would_have_to_be_true: list[str] = Field(default_factory=list)

    allowed_numbers: list[str]
    lineage: Lineage

    # Deliberately absent: status, reviewed_by, reviewed_at. Review state lives in columns
    # outside the hashed payload, so approving a sheet cannot change its payload_hash.
