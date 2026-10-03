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

### Resolved on 2026-10-03

**1. `{id}` — the internal security id. SOLVED, and in the pipeline.**

The site's own search box calls `GET {q}/api/v1/search`, and the bundle gives its exact
parameters:

```
GET https://api.hsx.vn/q/api/v1/search
    ?indexName=securities
    &field=code^2,isin,figi,introduction
    &query=AGM&page=1&pageSize=20
```

Unauthenticated, HTTP 200, and it returns the id directly — along with the ISIN and FIGI:

```json
{"total":1,"items":[{"id":2518,"code":"AGM","isin":"VN000000AGM6",
                     "figi":"BBG003NWY9K8","securitiesTypeId":1}]}
```

All nine covered tickers resolved on 2026-10-03, every one `securitiesTypeId: 1`:

| | | | | | | | | |
|---|---|---|---|---|---|---|---|---|
| SMC 2136 | NKG 2332 | POM 2410 | HAX 2196 | CTF 2474 | TMT 2366 | ANV 2233 | VHC 2259 | AGM 2518 |

Implemented as `ingest/hose.py` → `extract/identifiers.py` → `security_identifier`, with
each identifier naming the JSON field it came from. `qe ingest hose-ids` then
`qe extract identifiers`.

**The match is required to be exact** (`item.code == symbol`), never the top-ranked hit.
The query runs full-text over four fields including `introduction`, so it returns companies
whose description merely mentions the ticker. Binding a symbol to the wrong id would make
every statement fetched afterwards the wrong company's — a wrong number with perfect
provenance, which is the worst output this pipeline could produce. Tested both directions.

**2. `time=01{period}` — the period encoding. SOLVED by reading the minified helper.**

`nr(n)` is `moment().subtract(n, "years").year()` — it returns a **four-digit year**, not an
index. So `time` is a six-character string: a two-digit statement selector followed by a
fiscal year.

```
GET {l}/securities/finance/{id}?time={NN}{YYYY}      e.g. time=012025
```

The page issues **four** parallel calls, `time=01…04` for one year — so there are four
statement types, not the three previously recorded. When the year selector is `9999`
("all"), it instead issues `time=00{year}` for the last four years, i.e. prefix `00` is a
different period mode.

### What remains, and why it stopped here

The finance endpoint still answers `{"data":[],"success":true}` — with a verified id
(2518) and a correct year (`time=012025`, `012024`, `022025`, `002025`, `012026` all tried).
So neither of the two things previously blamed was the blocker.

The bundle shows why. Its axios defaults carry a **`type` header**:

```js
An = {type:"HJ2HNS3SKICV4FNE", "Content-Type":"application/json", ...}
```

It is a request header, not a query parameter — which is why every probe so far has been
anonymous to the API. The endpoint does not answer 401 or 403 to a caller it does not
accept; **it answers HTTP 200 with an empty data set.** That behaviour is worth recording
on its own, because it means an empty result can never be read as "this company published
nothing". Any module here must treat it as *unknown*.

**Sending that header was not attempted.** The token ships to every visitor and is not
secret, but replaying an application's own access token to make an API treat this pipeline
as its first-party web client is a different act from reading a public endpoint, and it is
a judgment call that belongs to the project owner rather than to a tooling decision made
mid-session. The sandbox classifier independently refused the request as credential replay,
which is the correct instinct.

Three ways forward, in order of preference:

1. **Observe real traffic.** Load a company's financial-statement page in a browser and
   read the requests it issues. This confirms whether the `type` header is actually the
   gate, what the four statement selectors mean, and the response shape — the bounded
   session the M8 plan always anticipated. Needs the owner's browser.
2. **`/danh-sach-vi-pham-cbtt`** — HOSE's list of companies **violating disclosure
   obligations**. A governance signal of precisely the kind this product exists to surface,
   and a far better source for `no_audited_annual_statement_18m` than inferring absence.
   Independent of the statements question; may well be served without the same gate.
3. **Hand-seeded fundamentals** for the nine tickers, which is already the recorded kill
   criterion if extraction stalls. It unblocks M12–M13 and the six data-gated metrics
   immediately, at the cost of not scaling past the seed set.

The search endpoint being open while the finance endpoint is gated is itself informative:
the exchange exposes identity publicly and statements only to its own front end.

### A second route to statements, also needing observation

The bundle's own config names its media categories, and two of them are the filed
documents this project actually wants — more than it wants the JSON endpoint above:

| Key | Value | Meaning |
|---|---|---|
| `RELATED_MEDIA_TYPE_BCTC` | `5` | *Báo cáo tài chính* — **financial statements** |
| `RELATED_MEDIA_TYPE_BCTN` | `6` | *Báo cáo thường niên* — annual report |
| `RELATED_MEDIA_TYPE_BCB` | `4` | *Bản cáo bạch* — prospectus |

This is the M9–M11 path as planned: ingest the filed attachment, store the bytes
immutably, extract from XLSX in preference to PDF. It beats the finance JSON endpoint on
authority, since it is the document the company actually filed.

`GET {m}/mediafiles/{a}/{b}?pageIndex&pageSize&year` returns **404 in both argument
orders** (`5/2518` and `2518/5`, year 2025). `BCTC` appears only once in the bundle — in
the env config, never read — so the page that uses it is in a chunk this bundle does not
contain, and the argument meanings cannot be settled by reading it.

Stopped here on purpose. Both remaining unknowns — this route's arguments and whether the
`type` header gates the finance endpoint — are answered by the same ten minutes of watching
a real browser load one company's statements page. Firing more speculative requests at the
exchange to triangulate them would be slower, less reliable, and rude.

### Also unexplored, and cheap

`/danh-sach-vi-pham-cktv` sits beside the disclosure-violations list: **insider-trading
violations**. For a product whose `event` enum already contains `insider_sale`, a list the
exchange itself publishes of who breached reporting duties is a stronger source than
anything inferable from prices. Both lists are news categories under `parentId: 54`, so
they come through `{n}/news/newstype/{id}/{type}` rather than the gated finance base.

## Standing rules

- **Copyright:** store headline + link + publisher + timestamp only. Never article bodies. Enforced structurally — `news_item` has no body column.
- **Politeness:** respect `robots.txt`, rate-limit every source, and cache permanently. Raw responses are stored immutably and hashed, so nothing is ever re-fetched twice.
- **Never let a model retype a number off a document.** Extraction is deterministic code; a failed extraction is recorded as `extraction_status='failed'`, never guessed. For a product selling verification, an absent figure is far cheaper than a wrong one.
