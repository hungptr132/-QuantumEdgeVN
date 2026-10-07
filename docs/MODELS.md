# The models — where each one lives, and how it works

A map of every model in QuantumEdge: its directory, its mechanism, and the architectural
reason it is shaped that way. `ARCHITECTURE.md` says where a change belongs; this says what
is there and why it holds together.

"Model" here means a thing that decides something. Four kinds: the **schema model** (what
may be stored), the **extraction models** (document → figure), the **computation models**
(figure → metric → conclusion), and the **publication model** (conclusion → page). There is
no statistical or machine-learned model anywhere in Phase 1, by decision — see the one rule
at the top of `CLAUDE.md`.

State at 2026-10-08: 31 tables, 17 enums, 493 tests, 9,592 lines of pipeline Python.

---

## 0. Schema model — what may exist at all

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/db/` |
| **Files** | `models.py` (918 lines, the single definition) · `enums.py` (161) · `seed.py` (512) · `session.py` |
| **Migrations** | `pipeline/migrations/versions/` — `0001_initial_schema`, `5c77d43f28ec_security_identifier`, `7a1c4e2b9d03_event_coverage` |
| **Live** | 31 tables, 17 native enums, 1 view, 1 trigger |

**Mechanism.** SQLAlchemy 2.0 declarative models are the source of truth; Alembic
revisions are generated from them and **hand-checked**, because autogenerate does not
reliably emit enums, partial indexes, `CHECK`s, triggers, views or `GRANT`s — and this
schema's guarantees are made of exactly those. Enums are declared `create_type=False` in
the models and created explicitly in the migration, so a type shared by several tables is
created once and exists before any table references it.

**Architecture.** The database is the enforcement layer, not a passive store. Six of the
eight invariants in `CLAUDE.md` are constraints rather than conventions:

- `raw_document` is append-only via a trigger that raises on `UPDATE`/`DELETE`.
- `financial_fact.source_field_path` is `NOT NULL` — no figure may exist without naming the
  exact field it came from.
- `flag_active_needs_evidence` is the defamation guardrail as a `CHECK`: a negative
  statement about a named company cannot be recorded without something attached.
- `fs_publish_requires_stamp` makes the human review gate unbypassable.
- `fact_sheet_one_published_per_ticker` is a partial unique index, which is why
  `fact_sheet_status` needs a `superseded` terminal state.
- `news_item` **has no body column**. The copyright rule is expressed as the absence of a
  place to put the text.

`db/seed.py` is a stage in its own right (see the stage table in `CLAUDE.md`): it loads
`rules/*.yaml` and `seeds/*.yaml` into reference tables, every write an upsert on the
natural key so re-seeding converges. It is also the only writer of `event` and
`event_coverage` in Phase 1, because those hold what a person read, not what a fetcher
fetched.

---

## 1. Ingest models — bytes, exactly as received

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/ingest/` |
| **Files** | `base.py` (127) · `vndirect.py` (158) · `hose.py` (174) · `filings.py` (221) · `rss_news.py` (305) |
| **Writes** | `raw_document`, `filing`, `ingest_run` |
| **Live** | 8 sources, 170 runs |

**Mechanism.** Every source funnels through one guarded writer in `base.py`: build a
canonical payload → `sha256` → check `UNIQUE (source_id, doc_type, symbol, period_key,
content_hash)` → insert, or count as `unchanged`. Idempotency has exactly one
implementation, so no source can get it wrong privately.

**Architecture.** One module per external source, and this is load-bearing rather than
tidy: when a provider breaks the blast radius must be a single file — and one already did,
which is why `vnstock` is not in the critical path (`docs/data-sources.md`). Ingest makes
**no parsing decisions**: VNDirect serves prices in thousands of VND and the ×1000 happens
in extract, because bytes stored as received can be re-extracted indefinitely as the
extractor improves.

Three outcomes are kept distinct and never collapsed: a document, a legitimately **empty**
answer, and a **failure**. A run that reached three of four sources finishes `partial`, not
`succeeded`.

`filings.py` is the human-in-the-loop path: a person drops a file into
`pipeline/data/filings/<SYMBOL>/`, named `<year>-<period>-<description>`, and its bytes are
archived under their hash.

---

## 2. Extraction models — document → figure

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/extract/` |
| **Files** | `statements.py` (696) · `news.py` (578) · `mapping/vas.py` (310) · `prices.py` (120) · `identifiers.py` (116) · `calendar.py` (91) · `base.py` (69) |
| **Mapping rules** | `pipeline/rules/vas_lines.yaml`, `pipeline/rules/fact_codes.yaml` |
| **Reads** | `raw_document` only |
| **Live** | 6,722 price bars · 189 headlines · 0 financial facts (no real filing yet) |

### 2a. The VAS statement model — the hardest one, and the differentiator

**Mechanism.** Vietnamese statements under Circular 200/2014/TT-BTC carry a **Mã số**, a
fixed numeric line code, in a form laid out `Chỉ tiêu | Mã số | Thuyết minh | Số cuối năm`.
`mapping/vas.py` folds diacritics (`đ` → `d` explicitly), and a row matches only when the
**code and the label both agree** — never one alone. Amounts parse Vietnamese convention:
dot for thousands, comma for decimals, parentheses for negative, dash for absent.

Then the statement validates itself against **VAS identities**: total assets = total
liabilities + equity; revenue − COGS = gross profit. A document failing an identity is
recorded as a failure, not stored.

**Architecture.** Facts are stored **long-format** against a 22-code registry
(`financial_fact_def`), not as a wide statement table: line items differ by sector and
provider column names change without notice, so a wide table needs a migration per rename
while a long one absorbs it in a mapping file. All 13 mappings in `vas_lines.yaml` are
`status: assumed` until someone checks a figure against a real filing, and each run reports
how many facts came from unverified mappings.

Six defects a review found here are in `docs/DECISIONS.md`; the load-bearing ones:

- **Zero identities is a refusal, not a pass.** A filing missing its totals produced an
  empty check list, no failures, and every figure written with nothing verifying it.
- **A statement not denominated in đồng is refused, never rescaled.** Every identity is
  scale-invariant, so a `triệu đồng` statement balances perfectly and stores a million
  times too small. **This is the one error the self-validation provably cannot catch** —
  written down here because an honest limit is worth more stated than discovered.
- **An amount must clear a magnitude floor.** Many filings number the note column with
  bare integers, so the first number after a code was a note reference. Every mapped fact
  is a VND amount and VND has no small numbers, so a floor separates them where position
  cannot.

### 2b. The news model — headline only

**Mechanism.** RSS 2.0, RSS 1.0/RDF and Atom are parsed explicitly with stdlib
`ElementTree` rather than `feedparser`, which folds `<description>` into `entry.summary` as
a matter of course — the one field that must never be in reach. Attribution matches the
headline against `ticker_alias.alias_norm` and the bare symbol, **exact token-boundary
match only**. `pg_trgm` is on that column for the future search layer, never for deciding
which named company a negative headline is about. No match means `ticker_id` NULL.

An item whose date will not parse is skipped and counted — never defaulted to `now()`,
which would silently date a stale article today.

**Architecture.** Alias rows derive from `company.legal_name_vi`, which is long enough that
an exact match cannot land on the wrong company. **Short names are deliberately not
derived**: ANV's "Nam Việt" normalises to `nam viet`, a token-boundary substring of
`nam viet nam`, so every headline about southern Vietnam would have been attributed to a
named company. Nicknames go in a hand-reviewed block that ships empty.

---

## 3. Computation models — figure → metric → conclusion

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/calc/` |
| **Files** | `periods.py` (603) · `liquidity.py` (570) · `benchmarks.py` (518) · `fundamentals.py` (483) · `rule_engine.py` (427) · `registry.py` (238) · `flags.py` (221) |
| **Rules** | `pipeline/rules/metrics.yaml`, `pipeline/rules/flags.yaml` |
| **Live** | 32 metric definitions → 2,379 values (124 available at the latest date) · 5,292 benchmarks · 17 flag definitions → 1,450 instances |

### 3a. The metric registry — the model that makes absence first-class

**Mechanism.** `@metric(code, requires_facts=(...))` plus a runner that **pre-checks every
declared input before calling the function**, then turns a missing one into
`is_available=False` with a reason instead of an exception, and records every id it used in
`metric_value.inputs`.

**Architecture.** Pre-checking rather than reading lazily is deliberate: lazy reads report
whichever input happened to be read first, while a pre-check reports all of them —
`missing_facts:ebit,interest_expense` tells a reader what to go and find. Metrics are
implemented **before their data source exists**, because a metric that is never attempted
writes no row, and then the page cannot even say *không công bố* for it: a reader could not
tell "we have no statements" from "we never looked".

A `metric_value` row satisfies
`(is_available AND value IS NOT NULL) OR (NOT is_available AND unavailable_reason IS NOT NULL)`
as a `CHECK`. There is no third state.

### 3b. The TTM period model — `calc/periods.py`

**Mechanism.** Trailing twelve months resolved **at read time** from stored quarterly
facts. A **flow** is the sum of exactly four consecutive quarters; a gap is unavailable,
naming the quarter. A **stock** is the window's closing balance, never a sum. An audited
annual covering the window wins over four unaudited quarters and records that it did.

**Architecture.** Not materialised as a `financial_period` row, for two reasons that both
point the same way: `financial_period` is extract-stage and calc may not write it, and a
stored TTM row would be a derived figure with no source document — it could not satisfy
invariant 2. The flow/stock split is **declared**, cross-checked against
`financial_fact_def.statement`, and a new fact code arriving unclassified fails the suite,
because defaulting either way produces a wrong number: 4× a balance sheet, or a quarter
reported as a year.

`ebit` and `ebitda` are **derived here**, not stored, for the same provenance reason —
`source_field_path` is `NOT NULL` and a derived figure has no field to point at. EBIT is
`profit_before_tax + interest_expense` (Mã số 50 is before tax but after interest, so
interest goes back on; the opposite sign roughly doubles coverage for a leveraged company,
which is the direction that flatters).

### 3c. Agent 5, liquidity — `calc/liquidity.py`

**Mechanism.** From `price_bar` + `market_calendar` + `quote_snapshot`: 20-session average
and **median** turnover, zero-trade sessions and ratio over 63 sessions, missing-bar
sessions counted **separately** from zero-volume ones, 52-week range and drawdown, bid-ask
spread, and `sessions_to_exit` for stated position sizes at an explicit
`participation_rate = 0.20` printed in the output.

**Architecture.** It lives in `calc/`, not `agents/`, because it is deterministic code and
its output embeds in the page without passing a model. Two decisions carry the weight:

- **Windows count calendar trading sessions, never rows of price data.** Dividing by bars
  present would shorten the window for exactly the illiquid stocks these metrics describe.
- **Exit capacity uses the median session, not the mean.** Found in live data: POM had 16
  of 20 sessions with zero volume and four block trades. The mean implied the position
  could be exited inside one session; there is no such session. A zero median reports
  "cannot estimate".

`market_calendar` exists so that a zero-trade session is distinguishable from a failed
ingest. Without it the entire model is unreadable.

### 3d. The benchmark model — `calc/benchmarks.py`

**Mechanism.** Three scopes: **sector** median/p25/p75, the ticker's **own history**, and
the **exchange**. `min_benchmark_sample = 3`; below it, the row is still written with
`is_available=false, unavailable_reason='sample_below_minimum'`, so a rule can say
"benchmark unavailable" rather than "benchmark not computed" — two states a reader is
entitled to tell apart. Every row records `member_ticker_ids`.

**Architecture.** Members are named because at this universe size a "sector median" is
three companies, and a comparison nobody can audit is one they must take on trust. The
subject **is** included in its own median, deliberately: leave-one-out would publish a
different "trung vị nhóm ngành thép" on SMC's page than on NKG's under the same label, and
at n=3 would drop every sector below the minimum.

Exchange scope is **per venue, not whole-market**: HOSE, HNX and UPCOM impose different
listing, free-float and disclosure regimes, so a median across their union describes none
of them. It is also the only cross-section available to a company with no sector assigned.

p25/p75 at n < 5 are flagged `quartiles_are_interpolated`. Under the R-7 convention at
n=3, p25 is exactly (min+median)/2 — no information beyond three numbers in the same row,
and "25th percentile" overclaims when the fraction below it is 1/3. They are kept rather
than suppressed, because the banding rule that reads them is more conservative than the
median-only alternative; the defect was the label, so the label is what moved.

### 3e. The flag model — `calc/rule_engine.py` + `calc/flags.py`

**Mechanism.** `flag_def.rule_expr` is JSON evaluated into `(state, evidence, reason)`.
Four rule kinds are implemented — `metric_threshold`, `metric_vs_benchmark`,
`event_presence`, `data_absence`. `composite` exists in the `flag_rule_kind` enum and is
not implemented; a rule using it evaluates to `unknown` with
`rule_kind_not_implemented:composite` rather than to anything that looks like an answer.
**A row is written for every rule × every covered ticker**,
including `inactive` and `unknown` ones, because the set of rules that were *considered* is
part of the record — an absent row could not distinguish "did not fire" from "never
evaluated".

**Architecture.** `flag_state` is three-valued, and that is the product. A boolean makes a
missing input indistinguishable from a clean result, and `unknown` is what lets a page say
*không tìm thấy công bố tương ứng*. A rule's `on_missing` may never map to `inactive`.

Two refusals live here:

- **`StaleRuleDefinitions`.** `flag_def` mirrors `rules/flags.yaml` and only `qe db seed`
  refreshes it, while `flag_instance` inherits its `rule_spec_version` from the definition
  it was evaluated against. So editing a threshold and recomputing applied the **old**
  threshold and stamped the **old** version, silently — and `qe doctor`'s own remediation
  could not clear it. Calc now refuses rather than auto-reloading: a silent reload would
  hide from the reader that the rules moved.
- **Absence needs `event_coverage`.** An `event_presence` rule used to ask whether the
  `event` table held *any* row, globally — so one hand-seeded event about one company would
  have flipped every other company's answer from "we have no way to know" to "no auditor
  change was disclosed". A coverage row is the positive assertion that a person looked:
  ticker, event type, read through this date, by this reviewer, from this source. Both
  `event` and `event_coverage` are empty, so every event rule honestly reads `unknown`.

`rule_spec_version` — the content hash of `rules/*.yaml` — is stamped on `flag_def`,
`flag_instance`, `ingest_run` and `fact_sheet`, and is what drives regeneration.

---

## 4. The fact sheet model — the contract everything downstream reads

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/factsheet/` |
| **Files** | `build.py` (510) · `comparisons.py` (296) · `schema.py` (270) · `qa.py` (240) · `store.py` (159) · `export.py` (98) |
| **Contract** | `contracts/factsheet.v1.schema.json` (generated, committed) → `web/src/lib/generated/factsheet.v1.ts` |
| **Snapshots** | `factsheets/<SYMBOL>/<date>__v<n>.json` (committed) |
| **Live** | 147 sheets, 1 published |

**Mechanism.** Pydantic v2, `extra="forbid"`, `frozen=True`. **Every number is a `Figure`,
never a bare float**: code, Vietnamese label, glossary definition, value, unit, a
**pre-formatted `display`** string, `available`, `unavailable_reason`, and a list of
`Provenance` each naming its ref, source and field path. `store.py` versions by payload
hash: identical hash writes nothing and **preserves the existing review stamp**; a
different hash opens a new `draft`.

**Architecture.** The page contains no business logic. Every label, formatted value,
*không công bố* string, glossary definition and position descriptor is precomputed here, so
the web app **cannot disagree with the pipeline**. Review state lives in columns outside the
hashed payload, so approving a sheet cannot look like a data change.

`allowed_numbers` is the field worth pointing at: `build.py` walks every figure, benchmark
and display string and emits the flat set of numeric strings that legitimately appear. When
Agent 2 exists, its validator is "tokenise the draft, assert every numeric token is in this
set" — a function call rather than a project. It is already exercised today against the
sheet's own static strings, so the validator has test coverage before it has a customer.

`qa.py` runs 17 machine checks with no model involved, each writing a coded pass/fail into
`qa_report`. Two encode law rather than taste: `every_figure_has_provenance` rejects any
available figure without a resolvable source, and `no_prohibited_language` screens every
static string against `nên mua`, `nên bán`, `khuyến nghị`, `mục tiêu giá`, `cam kết`,
`đảm bảo lợi nhuận`, `chắc chắn tăng`, `lừa đảo`, `scam` and `khống` — turning
"education, never recommendations" and "never say scam" into failing tests rather than
habits. `prose_numbers_are_all_allowed` is the `allowed_numbers` check turned on the
sheet's own text.

`comparisons.py` bands a figure `above` only past p75 and `below` only under p25; anything
between is `in_line`. Comparing against the median alone would announce a figure one dong
above the midpoint as "above the sector", which with three peers is noise reported as a
finding. The vocabulary is **positional, never evaluative** — `above` means a larger
number, and several metrics here are ones where larger is plainly worse.

---

## 5. The review and publication model

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/review/service.py` (249) |
| **Boundary** | the `published_ticker_page` view + the `qe_web` role |
| **Web** | `web/src/lib/queries/published.ts`, `web/src/app/co-phieu/[symbol]/page.tsx` |

**Mechanism.** A state machine — `draft → approved → published`, plus `rejected` and
`superseded` — with every transition logged to `fact_sheet_review_log` including the
rejection reason.

**Architecture.** Publication is gated three independent ways, and all three must agree:
the view's `WHERE status = 'published'`; `qe_web` holding **exactly one privilege** —
`SELECT` on that view, verified, and nothing on any base table; and the Next.js page
setting `dynamicParams = false` with paths from `generateStaticParams()` reading the view,
so an unpublished symbol is a build-time 404. A view runs with its owner's privileges, so a
query written against `fact_sheet` directly produces a permission error rather than a
leaked draft.

The web app also validates the payload's **shape** at runtime before rendering
(`web/src/lib/factsheet.ts`) and throws rather than degrading, because `payload` is `jsonb`
and the type annotation was an assertion the compiler could not check. A cross-language
drift test lives in the Python suite, on the side that owns the contract.

---

## 6. The agent models — contracts without implementations

| | |
|---|---|
| **Directory** | `pipeline/src/quantumedge/agents/` |
| **Contents** | `README.md` plus one directory per agent, each holding its contract |

| Agent | Status | Where it plugs in |
|---|---|---|
| 1 — events | not built (needs an LLM) | writes `event` + `event_coverage`; its tables exist and are queried today |
| 2 — expression | not built | reads `fact_sheet.payload` **only** — never the database, never a raw document. Its validator already exists and is tested |
| 3 — tip parser | not built | reuses `ticker_alias` for symbol resolution |
| 4 — portfolio | not built | reads published sheets |
| **5 — liquidity** | **built** | `calc/liquidity.py`, because it is deterministic code |

**Architecture.** Agent 5 is classified with the deterministic calc blocks, not with the
agents, and that placement is the whole design in miniature: **code computes, models only
ever express.** Phase 1 contains no LLM client, no API key and no prompt file. Each unbuilt
agent's directory holds its input contract so the boundary is fixed before anything is
written against it — and Agent 2's constraint (its only input is the payload) is what keeps
a future model from reaching around the fact sheet to invent a number.

---

## What is not built, and what blocks it

| Model | Blocked on |
|---|---|
| Statement extraction producing real facts | **One real filing.** The code, mapping and roundtrip harness are done and tested on synthetic Circular 200 workbooks; `financial_fact` is 0 because no filing has been obtained. Drop files into `pipeline/data/filings/<SYMBOL>/`, or resolve the HOSE source question |
| `ebitda`-dependent metrics (3) | `depreciation_amortisation`, unmapped — it is in the cash-flow statement |
| Debt-dependent metrics | `short_term_debt` / `long_term_debt`, deliberately unmapped: the circular numbers them differently across revisions and inverting them would invert `net_debt_ebitda` |
| Event rules leaving `unknown` (2) | A human recording what they read, in `seeds/events.yaml` |
| `bid_ask_spread_pct` | An in-session capture job; `quote_snapshot` is empty by design and the metric says so |
| Company descriptions | Human-written by decision; currently NULL and reported as a gap |
| HNX / UPCOM coverage | An authoritative source for those companies' legal Vietnamese names. Prices work; nothing structural blocks it |
| Agents 1–4 | An LLM, which Phase 1 excludes |

Two known limitations, recorded rather than hidden:

1. **A document scaled by 1000 passes every identity.** All of them are scale-invariant.
   This is the limit of self-validating extraction and no amount of further checking inside
   the document will close it.
2. **Headline attribution is decided at first sighting.** Adding an alias later does not
   revisit stored headlines. Checked against the 189 currently stored: none would attribute
   differently, so this has no present consequence — but a `--reattribute` pass is the fix
   when the alias list grows.
