# Decision log

One line per decision: what was chosen, why, and what would reverse it.
Newest last. This file is the project's memory — if a choice here looks wrong later,
the "reverses if" column is how you know it's time to revisit rather than re-litigate.

| Date | Decision | Why | Reverses if |
|---|---|---|---|
| 2026-10-01 | Phase 1 is the **deterministic backbone only** — no LLM, no Agents 1–4 | Reproducibility is the product; prove the data layer before a model can obscure it. Agent 5 is in scope because it is pure code. | The data layer is proven and prose generation becomes the bottleneck |
| 2026-10-01 | **Python owns the schema** (SQLAlchemy + Alembic); Next.js consumes read-only | The pipeline is the only writer; two migration systems over one database is the classic shared-DB failure | The web app ever needs its own tables (Layer 4 auth) — then it gets its own schema, not shared authority |
| 2026-10-01 | Publication gate is a **Postgres view + privilege-limited `qe_web` role**, not an app-layer filter | A forgotten `WHERE` clause becomes a permission error instead of a leaked draft | Never — this is strictly stronger than the alternative |
| 2026-10-01 | `flag_state` is **three-valued** (`active`/`inactive`/`unknown`), not boolean | With a boolean, missing inputs render as "no flag" i.e. *clean*, which the spec forbids. `unknown` is what makes a page say *không tìm thấy công bố tương ứng* | Never |
| 2026-10-01 | Review state lives in **columns outside the hashed payload** | Approving must not change `payload_hash`, or every approval looks like a data change | Never |
| 2026-10-01 | Fact sheet JSON is **committed to git**; raw payloads are not | Sheet diffs are the review artifact and the regression baseline; raw blobs are large and reproducible from source | Coverage grows past a few hundred tickers and the diffs stop being readable |
| 2026-10-01 | **Local portable Postgres**, no Docker/WSL2 | Docker Desktop on Win 11 Home needs WSL2 → admin + reboot. Portable binaries need neither, and the DSN is identical | A team forms, or CI needs a reproducible container |
| 2026-10-01 | Postgres data directory goes on **C: (NTFS)**, not the repo drive | `E:` is **exFAT** — no journaling, unreliable fsync. PostgreSQL requires sound fsync semantics or risks corruption | The repo moves to an NTFS volume |
| 2026-10-01 | **Python 3.12**, not the installed 3.14 | Wheel availability for the pandas/lxml/pdfplumber stack. Verified: all 18 dependencies install and import cleanly on 3.12.14 | 3.14 wheels become universal across the stack |
| 2026-10-01 | **Dropped `vnstock`**, the source named in the spec | Unusable: `vnstock` is not on PyPI, and every `vnstock3` version depends on `vnai`, which is unpublished with no public repo. See `data-sources.md` | `vnai` is published, or a licence is obtained from vnstock-hq |
| 2026-10-01 | Prices from **VNDirect dchart**; fundamentals from **HOSE/IR filings** | Only verified-working sources with a clear legal basis (HOSE robots.txt grants full permission). No unaffiliated wrapper in the critical path | Either source breaks, or an official data licence is acquired |
| 2026-10-01 | Extraction (Arc B) sequenced **after** a working published page (Arc A) | The spec's own "~3x longer than expected" warning applies to PDF extraction; a bad fortnight there must not leave the project with nothing running | Arc A proves trivial and fundamentals become the only blocker |
| 2026-10-01 | `.gitattributes` pins **LF** for text artifacts | Committed factsheet JSON must be byte-stable across machines or the reproducibility diff is polluted by line-ending churn | Never |

## Kill criteria

Carried from the product spec, section 8 — recorded here so they stay honest:

- Publish 5 hand-written pages on genuinely obscure tickers. **If nobody forwards them and nobody sends tickers to cover, the USP isn't real.**
- If HOSE/IR extraction (M10–M11) consumes more than a few weeks without producing reliable statement data for the 9 seed tickers, fall back to hand-seeded fundamentals rather than continuing to sink time — the backbone works without it.
