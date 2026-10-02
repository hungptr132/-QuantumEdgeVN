# Data sources

Verified by direct probe on **2026-10-01**. Re-verify before assuming any row still holds —
that is the whole lesson of the vnstock row below.

## In use

| Source | Purpose | Access basis | Status |
|---|---|---|---|
| **VNDirect dchart** `dchart-api.vndirect.com.vn/dchart/history` | Daily OHLCV history → `price_bar`, `market_calendar`, Agent 5 | Public endpoint, no credentials, no challenge | **Working.** Verified returning real bars for SMC and AGM |
| **HOSE** `hsx.vn` + `api.hsx.vn` | Disclosures and financial statement attachments → `filing`, `financial_fact` | `robots.txt` = `User-agent: * / Disallow:` — **empty Disallow, full crawl permission explicitly granted** | Reachable. Site is a **client-side SPA**; backend API paths must be discovered by observing real network traffic (milestone M8), not guessed |
| **Company IR pages** | Statement attachments, preferring XLSX over PDF | Per-site; record each site's robots position here as it is added | Not yet surveyed |
| Vietnamese finance RSS (CafeF, VietStock, VnExpress) | Headlines only → `news_item` | Public feeds | Deferred (optional, post-Arc B) |

### Response format notes

**VNDirect dchart** returns TradingView UDF format — parallel arrays, not records:

```json
{"t":[1790208000,...], "o":[2.1,...], "h":[2.1,...], "l":[2.0,...], "c":[2.1,...], "v":[0,54631,...]}
```

- `t` is a Unix timestamp per session; prices are in **thousands of VND** (`2.1` = 2,100 VND) and must be scaled on extraction.
- `v` can legitimately be **`0`** — a real zero-trade session, not missing data. Observed on AGM. This is precisely the distinction `market_calendar` exists to preserve, and it must never be silently dropped or interpolated.

## Rejected, with reasons

| Source | Why not |
|---|---|
| **`vnstock`** (named in the product spec) | **Unusable.** `vnstock` does not exist on PyPI (404); the distribution is `vnstock3`, and **every version of it depends on `vnai`**, which is not published on PyPI and has no public GitHub repo — so no resolver can install it. The library itself is maintained (repo pushed 2026-09-27, 1.4k stars), but `vnstock-hq` now hosts `licensing` and `vnstock-api` repos, indicating a move to a licensed API model with `vnai` as a private licensing/telemetry dependency. Revisit only if `vnai` is published or a licence is obtained. |
| **TCBS** `apipubaws.tcbs.com.vn` (what vnstock wraps) | Behind a **Cloudflare managed challenge**. This is a deliberate access control and is not to be worked around. |
| **SSI FastConnect Data** (`ssi-fc-data`, official SDK) | **Market data only** — securities lists, prices, streaming. No income statement, balance sheet, or cash flow. Does not solve fundamentals, which was the only reason to adopt it. Still a legitimate option if a second price source is ever wanted. |
| **VNDirect `finfo-api`** (fundamentals) | Resolves to **10.210.100.8**, a private RFC1918 address — internal-only, not publicly reachable. |
| `vnquant` | Installable, but scrapes CafeF/VNDirect — the same fragility class that just broke vnstock. Not adopted while an authoritative path exists. |

## HOSE API — discovery status (M8, partial)

Recovered by reading the SPA's own bundle (`https://www.hsx.vn/static/js/main.*.js`, ~2.4 MB),
not by guesswork. `robots.txt` grants full crawl permission, so reading the bundle the site
serves to every visitor is within bounds.

### What is confirmed

The app is a React SPA talking to a set of prefixed API bases, declared in its own config:

| Base | Purpose (inferred from use) |
|---|---|
| `https://api.hsx.vn/l/api/v1` | listed securities — carries `/securities/finance/{id}` |
| `https://api.hsx.vn/n/api/v1` | news and disclosures |
| `https://api.hsx.vn/m/api/v1` | datafeed and `/mediafiles/{type}/{id}` (attachments) |
| `https://api.hsx.vn/c/api/v1` | content |
| `https://api.hsx.vn/i/api/v1`, `/s/`, `/a/`, `/mk/` | info, secorgs, auctions, market |
| `https://api.hsx.vn/q/api/v1/search` | Elasticsearch behind the public search box |

Endpoint paths extracted from the bundle, with their real call signatures:

```
GET {l}/securities/finance/{id}?time=01{period}     # three parallel calls: time=01, 02, 03
GET {n}/news/securities/{id}/{type}?pageIndex&pageSize&startDate&endDate
GET {n}/news/newstype/{id}/{type}?pageIndex&pageSize&startDate&endDate
GET {m}/mediafiles/{type}/{id}?pageIndex&pageSize&year
GET {s}/secorgs/{id}
```

### What is NOT yet resolved

`{id}` is a numeric internal security id, not the ticker symbol. Verified: the finance
endpoint returns `{"data":[],"success":true}` for `AGM`, `VHC`, and for deliberate nonsense
alike — it accepts any path segment and returns an empty set, so a symbol is not a valid id
and probing cannot distinguish a wrong id from an empty period. The news and mediafiles
endpoints 404 on guessed numeric ids.

The `time=01{period}` encoding is produced by a minified helper that could not be recovered
by reading the bundle.

**Deliberately stopped here rather than brute-forcing.** Resolving the id space and the period
encoding needs observation of real traffic — load a company's financial-statement page in a
browser and read the requests it actually issues. That is one bounded session of work, and it
is what the M8 plan anticipated. Firing dozens of speculative requests at someone else's API
to reverse-engineer it is not a reasonable substitute.

### Also found, worth keeping

`/danh-sach-vi-pham-cbtt` — HOSE publishes a list of companies **violating disclosure
obligations**. That is a governance signal of exactly the kind this product exists to
surface, and a far more direct source for the `no_audited_annual_statement_18m` flag than
inferring absence ourselves. Worth pursuing alongside statement extraction.

The bundle also ships Elasticsearch basic-auth credentials (`internet_user`/`internet_user`)
for its public search box. They are shipped to every visitor and are not secret, but they
belong to a search cluster rather than a JSON API, so they are noted and not used.

## Standing rules

- **Copyright:** store headline + link + publisher + timestamp only. Never article bodies. Enforced structurally — `news_item` has no body column.
- **Politeness:** respect `robots.txt`, rate-limit every source, and cache permanently. Raw responses are stored immutably and hashed, so nothing is ever re-fetched twice.
- **Never let a model retype a number off a document.** Extraction is deterministic code; a failed extraction is recorded as `extraction_status='failed'`, never guessed. For a product selling verification, an absent figure is far cheaper than a wrong one.
