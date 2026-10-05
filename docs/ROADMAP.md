# Where the build is

Updated 2026-10-04. `docs/DECISIONS.md` says *why* for each choice below;
`docs/data-sources.md` says what each source does and what is still unknown about it.

## Done, and verified on real data

| # | Milestone | Evidence it works |
|---|---|---|
| M0 | Repo, git, toolchain | Python 3.12 via `uv`, 18 deps importing, `qe` CLI |
| M1 | Portable Postgres | PG 17.6 on :55432, ICU `vi-VN`, UTC, `qe_web` privilege separation asserted by a test |
| M2 | Schema v1 + seeds | 29 tables, 17 enums, append-only trigger, published view, grants |
| M3 | Price ingest | VNDirect dchart, 6,722 bars over 3 years, re-run reports `unchanged` |
| M4 | Price extract + calendar | ×1000 scaling, `volume = 0` preserved, 749 calendar days |
| M5 | **Agent 5 — liquidity** | 97 available metrics; exit capacity on the *median* session, not the mean |
| M6 | Flags, fact sheet, review gate | 10 machine QA checks; publishing without a human stamp raises `IntegrityError` |
| M7 | Next.js read slice | `next build` prerenders only the published ticker; a draft URL is a build-time 404 |
| M8 | HOSE identity | All 9 tickers → internal id + ISIN + FIGI, via the exchange's own public search. **Statements still gated** — see below |
| M12 | Fundamentals + benchmarks | 6 statement metrics implemented and data-gated; benchmarks across sector and own-history scopes |
| — | Risk metrics from price data | `turnover_concentration_3m` (AGM 52,21% vs CTF 6,69%), `max_drawdown_52w`, `sessions_since_52w_high`. All Vietnamese-formatted |
| M9–M11 | Statement extraction | VAS Mã số matching with a label guard; a statement failing its own balance identity is refused in full; 44 tests against a generated filing-shaped workbook. **Awaiting files** |
| M13 | Reproducibility | Rebuild with no change: 0 of 90 snapshots written, empty `git diff`. A threshold change moves only the threshold text and the flag that flipped |

Current state of the nine covered tickers: **19 active flags** carrying evidence,
**54 `unknown`** each naming what it waits for, datable flag transitions across 2024-03
to 2026-10, and 16 disclosure gaps stated rather than left blank. Each page leads with
**7 headline metrics**, 5 of them available without any statement data.

## The remaining gap

**No statements on disk yet.** The extraction path is built and tested (M9-M11); what it
needs is files. Six metrics - `pe`, `ev_ebitda`, `interest_coverage`, `net_debt_ebitda`,
`roe`, `shares_cagr_3y` - and six flag rules are implemented and correct, each naming the
exact line item it waits for.

**To unblock it:** download the nine companies' statements from their IR pages or HOSE,
prefer XLSX over PDF, name them `<year>-<period>-<description>`, and drop them in
`pipeline/data/filings/<SYMBOL>/`. Then `qe run`. That folder has a README with the
details. Roughly twenty minutes of downloading.

Two refusals are deliberate, not bugs: a statement that fails the VAS balance identity is
rejected in full, and a scanned PDF is reported unreadable rather than OCR'd.

Every mapping in `rules/vas_lines.yaml` is `status: assumed` until someone checks a figure
against a real filing, and each run prints how many facts came from unverified mappings.
`short_term_debt` and `long_term_debt` are deliberately unmapped - the circular numbers
them differently across revisions and inverting them would invert `net_debt_ebitda`.

**Alternatives, if the folder route is unwanted:** sending HOSE's `type` header (the
owner's call, since it replays the exchange's own front-end token), or the
disclosure-violation lists, which need the same browser observation as the attachment
route.

## Not started, in dependency order

### Breadth to HNX / UPCOM is blocked on a name source

Probed and ruled out 2026-10-04: VNDirect dchart serves HNX and UPCOM prices fine (TNG,
CEO, BSR, VGT all return real bars), but there is **no authoritative source for those
companies' legal Vietnamese names** — `api.hsx.vn`'s `secorgs` and `securities` endpoints
both 404, and its search covers HOSE only. Seeding them would mean typing real companies'
legal names from memory, and a wrong legal name on a page naming a real company is the
failure this product exists to prevent. Needs an HNX/UPCOM reference source, or names
entered by a person from filings.

| Work | Blocked on | Note |
|---|---|---|
| Verifying the VAS mappings | one real filing | Promote `assumed` to `verified` in `rules/vas_lines.yaml`, and map the two debt lines |
| Company descriptions (`description_vi`) | nothing | 9 rows, human-written by decision. Currently NULL and reported as a gap |
| `quote_snapshot` capture | a scheduled in-session job | Until then `bid_ask_spread_pct` is honestly `no_intraday_snapshot` |
| News/RSS (headlines only) | nothing | `news_item` has no body column by design |
| Agent 1 — events | an LLM, which Phase 1 excludes | Its `event` table exists and is queried today; rows can be hand-seeded with `extraction_method='manual'` |
| Agent 2 — expression | Agent 1 | Its validator already exists and is tested: `allowed_numbers` + `factsheet/qa.py` |
| Agents 3–4 | Agent 2 | See `pipeline/src/quantumedge/agents/` for each contract |
| HNX / UPCOM coverage | a legal-name source (above) | Nothing *structural* blocks it: `exchange` is per-ticker, every query filters `is_covered`, and the calendar is already derived per exchange. Note the calendar rule (a date is a trading day if any covered ticker traded) weakens with few tickers, so seed enough per exchange before trusting it |

## Backups

`scriptsackup.ps1` mirrors git and dumps the database to `D:` (NTFS, internal), and
verifies both. Run it at the end of a session. `docs/BACKUP.md` says what is not covered
and how to recover it. This exists because `E:` disconnected mid-session on 2026-10-04
while holding the only copy.

## Standing constraints

- **Nothing is published without a human stamp.** Enforced by `CHECK` constraints and by
  `qe_web` holding exactly one privilege: `SELECT` on `published_ticker_page`.
- **Nothing leaves this machine.** No cloud accounts, no deployment.
- **No LLM anywhere in Phase 1.**
- One published Claude Artifact from an early session still exists online
  (`6hBgrXMBzbqGtPpEU66rSf`, the ROS/FLC post-mortem). Deleting it was declined when
  offered; it stays unless the owner says otherwise.
