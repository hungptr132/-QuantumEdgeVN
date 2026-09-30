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

## Standing rules

- **Copyright:** store headline + link + publisher + timestamp only. Never article bodies. Enforced structurally — `news_item` has no body column.
- **Politeness:** respect `robots.txt`, rate-limit every source, and cache permanently. Raw responses are stored immutably and hashed, so nothing is ever re-fetched twice.
- **Never let a model retype a number off a document.** Extraction is deterministic code; a failed extraction is recorded as `extraction_status='failed'`, never guessed. For a product selling verification, an absent figure is far cheaper than a wrong one.
