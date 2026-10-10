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
    """Where one figure or conclusion came from.

    `data_absence` is the odd one and the important one: a `data_absence` rule's evidence
    IS the absence — "no audited annual statement within 18 months of this date" — so it
    has a ref and a label but no document and no URL. The kind was missing from this
    Literal while `rule_engine._data_absence` already emitted it, which did not fail only
    because no ticker had statements yet, so the rule always returned `unknown` and never
    reached evidence. The first ticker to gain statements *and* a disclosure gap would
    have had its fact sheet refuse to build — the sheet for the company the gap is about.
    """

    kind: Literal[
        "metric",
        "benchmark",
        "price_bar",
        "financial_fact",
        "event",
        "filing",
        "news",
        "manual",
        "data_absence",
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

    # True when p25/p75 are interpolations between order statistics rather than data
    # points in the sample. Under the R type 7 convention, at n=3 p25 lands exactly at
    # (min + median)/2 and p75 at (median + max)/2 — so they carry no information beyond
    # three numbers already in this row, and calling them percentiles overclaims: the
    # fraction of a three-point sample below p25 is 1/3, not 1/4. n=5 is the first size
    # where both quartiles are real order statistics.
    #
    # Carried rather than suppressed because the banding rule that reads them is strictly
    # more conservative than the median-only alternative. Withholding them would push the
    # page back onto "a dong above the midpoint is above the sector", which is worse. The
    # defect is the label, so the label is what this fixes.
    quartiles_are_interpolated: bool = False
    quartile_note_vi: str | None = None


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

    # Per exchange, not whole-market: HOSE, HNX and UPCOM impose different listing,
    # free-float and disclosure regimes, so a median across their union summarises two or
    # three populations and describes none. It is also the only cross-section available to
    # a company with no sector assigned, which the sector query filters out entirely.
    exchange: BenchmarkRef | None = None
    position_vs_exchange: Literal["above", "below", "in_line", "unknown"] = "unknown"
    position_vs_exchange_label_vi: str = "không công bố"


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


class StateChange(Strict):
    """One recorded transition of a flag."""

    changed_on: date
    from_state: Literal["active", "inactive", "unknown"]
    to_state: Literal["active", "inactive", "unknown"]
    label_vi: str


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

    # How long this has been true, and how it has moved. A flag that has been active
    # for two years and one that turned active last quarter are different situations,
    # and a static snapshot cannot tell them apart. Empty until metrics have been
    # computed at more than one as_of date.
    active_since: date | None = None
    active_since_label_vi: str | None = None
    history: list[StateChange] = Field(default_factory=list)


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


class NewsRef(Strict):
    """One headline, and nothing more than a headline.

    `news_item` has no body column — the copyright rule is expressed as the absence of a
    place to put the text — and this mirrors it exactly: headline, link, publisher,
    timestamp. There is no summary field and none may be added. A reader who wants the
    article follows the link to the publisher, which is also the only arrangement that
    keeps the publisher's traffic theirs.

    `display_date` is preformatted here because the web layer holds no formatting logic;
    `editor_note_vi` is the one line of prose we are entitled to publish, written by a
    person and never generated.
    """

    headline: str
    url: str
    publisher: str
    published_at: datetime
    display_date: str
    editor_note_vi: str | None = None


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

    # Only headlines attributed to *this* ticker, by exact alias or whole-symbol match.
    # An unattributed market headline on a company's page would be an implicit claim that
    # it is about them, which is precisely the claim the attribution rule refuses to make.
    news: list[NewsRef] = Field(default_factory=list)

    disclosure_gaps: list[DisclosureGap]
    what_would_have_to_be_true: list[str] = Field(default_factory=list)

    allowed_numbers: list[str]
    lineage: Lineage

    # Deliberately absent: status, reviewed_by, reviewed_at. Review state lives in columns
    # outside the hashed payload, so approving a sheet cannot change its payload_hash.
