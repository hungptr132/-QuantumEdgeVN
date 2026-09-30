# QuantumEdge retrospective post-mortem: ROS (FLC Faros Construction JSC)

**What this document is:** a manual walk-through of the QuantumEdge verification framework (see `IDEA + PIPELINE MODELLING/`) applied to a real, already-concluded Vietnamese small/mid-cap collapse — ROS, the FLC Group construction subsidiary at the center of the Trịnh Văn Quyết stock-manipulation case. No pipeline, schema, or agents have been built; this is the "zero-infrastructure" prototype the product spec's section 8 recommends as the first move, done by hand.

**Method:** everything in sections 1–6 below is written as if it were the ticker page a user would have seen querying ROS **on 31 December 2021** — the last trading day before the Jan 2022 events that triggered the collapse. Only facts that were on the public record by that date are used there. Section 7 is the epilogue, written from today's vantage point, showing what actually happened next. This split is the entire point of the exercise: it tests whether the framework's flags would have existed *before* the outcome, not whether they can be reconstructed with hindsight.

**Scope note:** the product's intended coverage is all three Vietnamese exchanges (HOSE, HNX, UPCOM) across quantitative, technical, and qualitative signal — not HOSE alone. This document covers one HOSE-listed case because it is the best-documented Vietnamese small/mid-cap blow-up available, not because the product's scope is HOSE-only.

**Honesty note:** this prototype has no Layer-0 schema and no Postgres store, so sector-median figures that a shipped ticker page would compute automatically from a live database are not available here and are marked as such rather than invented — the same "không công bố" (not disclosed) discipline the spec requires of the real product.

---

## 1. Business description

FLC Faros Construction JSC (ticker: **ROS**) was a construction and real-estate contractor within the FLC Group ecosystem, founded as a small firm registered with VND 1.5 billion in charter capital in 2011. It listed on HOSE in September 2016. By the date of this snapshot, ROS was one of seven tickers retail forums and financial media routinely grouped together as "cổ phiếu họ FLC" ("the FLC family stocks") — FLC, ROS, HAI, AMD, GAB, ART, and KLF — due to shared leadership, cross-holdings, and correlated trading.

## 2. Metrics (as of 31 Dec 2021)

| # | Metric | Value | Sector median / 3-yr range | Source |
|---|---|---|---|---|
| 1 | Charter capital growth, 2011→2016 | VND 1.5bn → VND 4.3 trillion (~2,900x) in 5 years, at listing | không công bố — no live sector-median feed in this prototype; flagged for the scale alone, not a comparison | [VnExpress: 5 năm 'tạo sóng' cổ phiếu họ FLC](https://vnexpress.net/5-nam-tao-song-co-phieu-ho-flc-cua-ong-trinh-van-quyet-4716298.html) |
| 2 | Further capital increase, 2016→May 2018 | VND 4.3 trillion → VND 5,675 billion | n/a | [VietStock company profile](https://finance.vietstock.vn/ros/ho-so-doanh-nghiep.htm?languageid=2) |
| 3 | Price vs. 3-year range | Peaked at 214,000 VND/share (Nov 2017); closed below its own 10,000 VND par value for most of 2020–Nov 2021, first closing above par on 15 Dec 2021; closed the year at 13,600 VND (–93.6% from the 2017 peak) | không công bố — no sector index constructed in this prototype | [VietnamBiz: cổ phiếu ROS đóng cửa dưới mệnh giá lần đầu tiên](https://vietnambiz.vn/co-phieu-ros-dong-cua-duoi-menh-gia-lan-dau-tien-trong-lich-su-20200131160112225.htm); [VietnamBiz: ROS kịch trần về mệnh giá](https://vietnambiz.vn/co-phieu-ho-flc-dong-loat-tang-ros-kich-tran-ve-menh-gia-2021121516041785.htm) |
| 4 | Guided FY2021 profit growth | Company plan: **54x** prior-year profit | không công bố — no independent verification of the underlying business existed at this date | [Kinhtechungkhoan: FLC Faros lên kế hoạch lợi nhuận năm 2021 tăng trưởng 54 lần](https://kinhtechungkhoan.vn/flc-faros-len-ke-hoach-loi-nhuan-nam-2021-tang-truong-54-lan-1331878.html) |
| 5 | Free float / independent ownership depth | **không công bố** — not independently reconstructed for this prototype | n/a | — |

Row 5 is left blank deliberately: a real Layer-1 page would either source this from disclosed ownership filings or mark it exactly this way. Leaving it blank here is itself the demonstration of the product's "explicit gap is part of the value" rule.

## 3. Active red flags (as of 31 Dec 2021)

Each flag below existed on the public record before the collapse. Tagged with the closed event-type enum from the spec's Agent 1 (`dilution`, `auditor_change`, `insider_sale`, `related_party`, `profit_warning`, `board_change`, `share_pledge`).

| Date | Type | Flag | Evidence |
|---|---|---|---|
| Sept 2016 | `dilution` | Charter capital reached VND 4.3 trillion at listing, up ~2,900x from VND 1.5bn in five years, for a firm with a short operating history | [VnExpress](https://vnexpress.net/5-nam-tao-song-co-phieu-ho-flc-cua-ong-trinh-van-quyet-4716298.html) |
| 2016–May 2018 | `dilution` | Charter capital raised again, to VND 5,675 billion, within 18 months of listing | [VietStock](https://finance.vietstock.vn/ros/ho-so-doanh-nghiep.htm?languageid=2) |
| 2020 (ongoing) | `related_party` | ROS traded as part of a seven-ticker cluster (FLC, ROS, HAI, AMD, GAB, ART, KLF) under common leadership; ART (BOS Securities) was the group's own affiliated brokerage — a related-party lender to accounts trading the other six tickers | [Hanoi Times](https://hanoitimes.vn/flc-linked-stocks-subject-to-forced-delisting-from-hose-321619.html) |
| 2020 | `related_party` (ecosystem-level technical flag) | GAB, a same-ecosystem ticker, rose from 16,250 to 196,800 VND (+~1,110%) in 2020 on sessions of only a few hundred to a few thousand shares traded — a textbook thin-float, one-way-price pattern | [CafeLand](https://cafeland.vn/tin-tuc/co-phieu-thi-gia-cao-nhat-ho-flc-bi-huy-niem-yet-120821.html); [VietnamNet](https://vietnamnet.vn/co-muc-gia-ky-luc-gab-bi-kiem-soat-chu-tich-xin-tu-nhiem-2062846.html) |
| 2020 | `board_change` | GAB's chairman resigned around the time the stock hit its record high and came under exchange scrutiny | [VietnamNet](https://vietnamnet.vn/co-muc-gia-ky-luc-gab-bi-kiem-soat-chu-tich-xin-tu-nhiem-2062846.html) |
| Jan 2021 – late Nov 2021 | (price/technical, not an event-enum item) | ROS traded below its own par value (10,000 VND) for essentially the entire year, before a rally to 13,600 VND in the final two weeks of December | [VietnamBiz](https://vietnambiz.vn/co-phieu-ho-flc-dong-loat-tang-ros-kich-tran-ve-menh-gia-2021121516041785.htm) |
| Late 2021 | `profit_warning` (guidance-plausibility flag, not a literal warning) | Company issued FY2021 guidance of 54x prior-year profit growth, unaccompanied by independently verifiable operating detail | [Kinhtechungkhoan](https://kinhtechungkhoan.vn/flc-faros-len-ke-hoach-loi-nhuan-nam-2021-tang-truong-54-lan-1331878.html) |

No `auditor_change` or `share_pledge` events are dated before 31 Dec 2021 in the sources reviewed for this prototype — per the spec's own rule, absence of a match is recorded as "no corresponding disclosure found for this date," not as "clean."

## 4. What would have to be true for this to work

For a 31 Dec 2021 buyer of ROS to be rewarded, essentially all of the following would need to hold simultaneously:
- A construction subsidiary that grew its charter capital ~2,900x in five years, then again within 18 months of listing, had a real balance sheet behind that capital rather than one built mostly on paper.
- A company that traded below its own par value for nearly two full years could plausibly deliver 54x profit growth in the following year, without new disclosed contracts, assets, or business lines explaining the jump.
- The seven-ticker "họ FLC" cluster's correlated, often thinly-traded price moves (exemplified by GAB's +1,110% year on near-zero volume) reflected independent fundamentals in each company rather than common control of trading activity.
- The group's captive brokerage (ART/BOS) extending financing to accounts trading the other ecosystem tickers created no conflict of interest.

## 5. Explicit disclosure gaps (as of 31 Dec 2021)

- No independently sourced free-float or beneficial-ownership breakdown for ROS is available in this prototype (không công bố).
- No sector-median comparison is available — this prototype has no live data pipeline (không công bố).
- Nothing in the public record reviewed here disclosed, before 31 Dec 2021, *how* the 2016 charter-capital increase had been achieved. Its scale was visible and flaggable; the fraud mechanism behind it (see §7) was not yet public.
- No related-party financing terms between ART/BOS and the other ecosystem tickers were quantified in sources reviewed for this date.

## 6. Mock tip run through the Agent 3 logic

**Tip, as it might plausibly have circulated in a Zalo group in late December 2021:**
> "ROS sắp bứt phá! Vừa vượt mệnh giá sau 2 năm, kế hoạch lợi nhuận 2022 tăng khủng, cả họ FLC đang kéo trần. Vào hàng sớm trước khi tăng tiếp!"
> ("ROS is about to break out! Just crossed par value after 2 years, 2022 profit plan is surging, the whole FLC family is hitting ceiling. Get in early before it runs further!")

**What the tip claims:** ROS crossed above par value; a large forward profit increase is planned; the wider FLC-linked group is rallying together; implication that the rally will continue.

**What the data shows:** The par-value crossing and the group-wide rally are both confirmed by public price data as of this date (§2, §3). The "40x+ profit plan" claim matches the company's own guidance — no corresponding disclosure found that independently substantiates the operating basis for that guidance. No disclosure found explaining why seven related tickers are moving together beyond common ownership/leadership, which is itself a `related_party` flag rather than a fundamentals signal.

**What it doesn't tell you:** That ROS spent nearly two years trading below par value before this rally; that its charter capital had grown roughly 2,900x in five years at listing with no available sector-median context; that GAB — a same-ecosystem ticker — had already shown a near-identical thin-volume, one-way-price pattern the year before. No conclusion of "scam" or "don't buy" is offered — only that the tip's excitement is not yet supported by anything beyond the same public facts already flagged above.

## 7. Epilogue — what actually happened

- **5–10 Jan 2022** (`insider_sale`): FLC filed, then re-filed, disclosure of chairman Trịnh Văn Quyết's plan to sell 74.8 million FLC shares. The paperwork for the executed sale was only completed at 17:45 on 10 Jan 2022 — after the trading session closed — despite shares having already been sold that day, and without the legally required advance notice. HOSE canceled the transaction. ([VietnamNet](https://vietnamnet.vn/en/trinh-van-quyets-transaction-canceled-securities-accounts-blocked-808363.html))
- **18 Jan 2022**: the State Securities Commission fined Quyết VND 1.5 billion and banned him from trading for five months. ([VIR](https://vir.com.vn/flc-chairman-trinh-van-quyet-to-be-heavily-fined-for-negligence-to-publicly-report-stocks-sale-90609.html))
- **29 Mar 2022**: Quyết was arrested; the Ministry of Public Security opened an investigation into stock-price manipulation and concealment of information. ([VnExpress](https://e.vnexpress.net/news/companies/flc-chairman-arrested-for-stock-market-manipulation-4445018.html))
- **Investigation findings**: Quyết's sister had opened roughly 500 trading accounts under 45 family members and 20 shell companies from 2017 to manipulate five ecosystem tickers (FLC, ROS, HAI, AMD, GAB); the group's own brokerage, BOS/ART, extended fake margin credit worth VND 170,500 billion across more than 1,500 transactions to accounts in the scheme. ([TheInvestor](https://theinvestor.vn/seven-officials-accused-of-helping-former-flc-chairman-manipulate-stock-market-d8728.html))
- **Sept 2022** (`auditor_change`): FLC could not find an audit firm willing to sign off on its FY2021 statements; UHY Auditing and Consulting eventually took the engagement but could not reach agreement with FLC on the opinion, leaving FY2021–2023 statements unpublished. ([VietnamBiz](https://vietnambiz.vn/nhung-vuong-mac-nghin-ty-khien-flc-chua-the-cong-bo-bao-cao-tai-chinh-du-da-lam-viec-voi-kiem-toan-trong-nhieu-thang-2023360109312.htm))
- **5 Sept 2022**: ROS was forcibly delisted from HOSE for serious disclosure violations — no H1 2022 financial statements, no annual general meeting, and an insufficient board of directors. ([The Saigon Times](https://english.thesaigontimes.vn/ros-forced-to-delist-from-hose-from-early-next-month/))
- **Feb 2023**: FLC itself was delisted from HOSE. ([Tuoi Tre News](https://tuoitrenews.vn/news/business/20230215/vietnams-flc-shares-to-be-delisted-next-week/71561.html))
- **5 Aug 2024 / appeal**: the Hanoi People's Court found that Quyết and accomplices had falsified capital-contribution documents from 2014–Sept 2016 to inflate Faros's charter capital from VND 1.5 billion to VND 4.3 trillion, then sold over 391 million shares built on that fabricated capital between 2016 and 2022 for proceeds of roughly VND 4.8 trillion, of which about VND 3.6 trillion was found to have been misappropriated from investors. Quyết was convicted of fraud and stock-market manipulation; his sentence was later reduced on appeal to 7 years plus a fine. ([TheInvestor](https://theinvestor.vn/prison-sentence-reduced-by-14-years-for-former-flc-chairman-trinh-van-quyet-d16125.html); [VnExpress](https://e.vnexpress.net/news/business/companies/property-tycoon-trinh-van-quyet-sentence-reduced-by-14-years-to-7-4906495.html))

**The point of this exercise:** every flag in §3 — the ~2,900x capital growth, the two-year stretch below par value, the 54x profit guidance, the ecosystem-wide correlated thin-volume rallies, the captive in-group brokerage — was on the public record before 31 Dec 2021. None of it required the 2024 court verdict to see. A tool that surfaced these mechanically, without needing to *know* it was fraud, would have shown a retail investor exactly what they were buying into before the January 2022 crash, the arrest, the delisting, and the criminal conviction that followed.

---

*Reviewed by: (prototype — no named reviewer; produced as a written framework demonstration, not a published product page). Personal position in FLC/ROS/HAI/AMD/GAB/ART/KLF or any related entity: none. This document makes no investment recommendation and states no conclusion beyond what the cited sources report.*
