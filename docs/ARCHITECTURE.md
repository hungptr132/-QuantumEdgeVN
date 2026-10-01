# Architecture — where things are, and where fixes go

Two codebases, one Postgres database. `pipeline/` is the only writer; `web/` only reads,
through a single view, with a role that holds no privilege on any base table.

```
sources ──> ingest ──> raw_document ──> extract ──> price_bar / financial_fact
                        (immutable)                        │
                                                           v
                                              calc ──> metric_value
                                                       metric_benchmark
                                                       flag_instance
                                                           │
                                                           v
                                              factsheet ──> fact_sheet (draft)
                                                           │
                                                      review (human stamp)
                                                           │
                                                           v
                                              published_ticker_page (view) ──> web
```

Data only ever flows one direction. A stage reads the previous stage's tables and writes
its own. Nothing skips a stage, and nothing reads backwards — which is why a wrong number
can always be bisected by stage.

## Symptom → location

Start here when something is wrong. The column that matters is the last one: it tells you
what to query to confirm the diagnosis before changing code.

| Symptom | Stage | Files | Confirm with |
|---|---|---|---|
| A number is wrong on a page | extract | `extract/vnstock_parse.py`, `extract/mapping/` | `SELECT value, source_field_path, source_raw_document_id FROM financial_fact WHERE …` — the path names the exact source field |
| A number is wrong but extraction looks right | calc | `calc/metrics.py` | `SELECT value, inputs FROM metric_value WHERE …` — `inputs` lists every fact id and value used |
| A metric says *không công bố* unexpectedly | calc | `calc/metrics.py` | `SELECT is_available, unavailable_reason FROM metric_value WHERE …` |
| A flag fires when it should not (or doesn't) | calc | `pipeline/rules/flags.yaml` first, then `calc/rule_engine.py` | `SELECT state, reason, triggered_values, evidence FROM flag_instance WHERE …` |
| A flag reads `unknown` | calc | usually correct — an input is missing | `reason` column names the missing input |
| Liquidity figures look wrong | calc | `calc/liquidity.py` (Agent 5) | compare against `price_bar` + `market_calendar`; check zero-volume vs absent-bar handling |
| A page renders stale data | factsheet | `factsheet/builder.py`, `factsheet/store.py` | `SELECT version, generated_at, rules_hash, is_current FROM fact_sheet WHERE …` |
| A page renders nothing / 404s | review | not a bug — unpublished by design | `SELECT status, reviewed_by FROM fact_sheet WHERE …` |
| Page formatting is wrong | **not the web app** | `factsheet/schema.py` — `display` strings are precomputed | the web layer contains no formatting logic |
| A source returns nothing | ingest | one file in `ingest/` | `SELECT stats, error FROM ingest_run ORDER BY id DESC LIMIT 5` |
| Duplicate rows appeared | ingest | `ingest/raw_store.py` | the `UNIQUE (source_url, content_hash)` guard should make a re-run a no-op |
| Values changed and nobody knows why | any | — | join `calc_run_id` → `ingest_run` for `git_commit` + `rule_spec_version` |

## The three traceability anchors

Every derived row carries enough to reconstruct its own history. This is the reason
"why did this change on 3 October" is a query rather than an afternoon.

1. **`source_raw_document_id` + `source_field_path`** — a figure points at the exact field
   of the exact stored document. The document is immutable, so the claim stays verifiable.
2. **`calc_run_id` + `rule_spec_version`** — which run and which version of the rules
   produced this value.
3. **`git_commit`** in `ingest_run` — which code version was in play.

## Optimizing a branch of code

- **Thresholds and rules:** edit `pipeline/rules/*.yaml`, never Python. The file's content
  hash becomes `rule_spec_version`, so `qe doctor` can list every ticker now stale and
  `--rebuild` regenerates exactly those. Committed fact sheets under `factsheets/` then show
  a diff of precisely which numbers moved.
- **A new metric:** add a `metric_def` row plus a function in `calc/metrics.py` declaring
  its required facts via the registry. Declared inputs are what let the fact sheet mark it
  unavailable with a reason instead of crashing when a fact is missing.
- **A new source:** one new module in `ingest/`, one mapping table in `extract/mapping/`.
  Nothing downstream changes, because downstream reads tables, not sources.
- **A broken source:** replace its one `ingest/` module. Already-captured raw payloads stay
  valid, so extraction and everything after it can be re-run without re-fetching.
- **A better extractor:** re-run `extract` against stored raw documents. Because raw data is
  immutable and never re-fetched, extraction quality can improve indefinitely against bytes
  already on disk. This is what makes shipping an imperfect first parser safe.

## Reference

- Decisions and their reversal conditions: `DECISIONS.md`
- What data comes from where, and what does not work: `data-sources.md`
- Product spec and pipeline diagram: `spec/`
- Conventions and invariants: `../CLAUDE.md`
