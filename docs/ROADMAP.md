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
| M12 | Fundamentals + benchmarks | 6 statement metrics implemented and data-gated; 4,536 benchmarks across sector and own-history scopes |
| M13 | Reproducibility | Rebuild with no change: 0 of 90 snapshots written, empty `git diff`. A threshold change moves only the threshold text and the flag that flipped |

Current state of the nine covered tickers: **17 active flags** carrying evidence,
**54 `unknown`** each naming what it waits for, **41 datable flag transitions** across
2024-03 to 2026-10, and **16 disclosure gaps** stated rather than left blank.

## The one real blocker

**No fundamentals source.** Six metrics — `pe`, `ev_ebitda`, `interest_coverage`,
`net_debt_ebitda`, `roe`, `shares_cagr_3y` — and six flag rules are implemented and
correct, but have no data. Each reports the exact line item it is waiting for.

Three ways past it, and the first needs a decision rather than code:

1. **Send HOSE's `type` header.** Found, documented, deliberately not sent: it would be
   replaying the exchange's own front-end access token so the API treats this pipeline as
   its web client. The project owner's call. If taken, the statements endpoint and the
   `mediafiles` attachment route both likely open, and M9–M11 proceed as planned.
2. **The disclosure-violation lists** (`/danh-sach-vi-pham-cbtt`, `/danh-sach-vi-pham-cktv`).
   The exchange publishing who breached disclosure and insider-reporting duties is a
   governance signal of exactly this product's kind, and a better source for
   `no_audited_annual_statement_18m` than inferring absence. Routes through the news base,
   independent of the gate above.
3. **Hand-seeded fundamentals** for nine tickers, from filings read by a person. Already
   the recorded kill-criterion fallback. Unblocks everything immediately; does not scale.

## Not started, in dependency order

| Work | Blocked on | Note |
|---|---|---|
| M9–M11 statement ingest + extraction | the blocker above | Prefer XLSX over PDF; store bytes immutably so extraction is re-runnable |
| Company descriptions (`description_vi`) | nothing | 9 rows, human-written by decision. Currently NULL and reported as a gap |
| `quote_snapshot` capture | a scheduled in-session job | Until then `bid_ask_spread_pct` is honestly `no_intraday_snapshot` |
| News/RSS (headlines only) | nothing | `news_item` has no body column by design |
| Agent 1 — events | an LLM, which Phase 1 excludes | Its `event` table exists and is queried today; rows can be hand-seeded with `extraction_method='manual'` |
| Agent 2 — expression | Agent 1 | Its validator already exists and is tested: `allowed_numbers` + `factsheet/qa.py` |
| Agents 3–4 | Agent 2 | See `pipeline/src/quantumedge/agents/` for each contract |
| HNX / UPCOM coverage | nothing structural | `exchange` is per-ticker and every query filters `is_covered`; adding a name is a row in `coverage.yaml` |

## Standing constraints

- **Nothing is published without a human stamp.** Enforced by `CHECK` constraints and by
  `qe_web` holding exactly one privilege: `SELECT` on `published_ticker_page`.
- **Nothing leaves this machine.** No cloud accounts, no deployment.
- **No LLM anywhere in Phase 1.**
- One published Claude Artifact from an early session still exists online
  (`6hBgrXMBzbqGtPpEU66rSf`, the ROS/FLC post-mortem). Deleting it was declined when
  offered; it stays unless the owner says otherwise.
