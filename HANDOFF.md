# Handoff — Earnings verification toolkit

Context for the next Claude Code instance picking up this project.

## What this is

A tool that pulls publicly-filed quarterly financials for companies in **US,
Korea, Taiwan, Japan** from free official/near-official APIs and reconciles them
against the user's local CSV files, reporting **which company / metric / period
does not match**. (China was explicitly dropped from scope.)

- Main program: `verify_earnings.py` (single file, stdlib + `requests`).
- Background research: `earnings-api-research.md`.
- User guide + assumptions + limitations: `README.md` (read this too).
- Branch: `claude/earnings-api-research-kwe3km-gp1m3p`. All work is committed & pushed.

## Run it

```bash
pip install -r requirements.txt
export DART_KEY=...  EDINET_KEY=...  JQUANTS_KEY=...  SEC_USER_AGENT="you@example.com"
python verify_earnings.py --self-test              # live check on 6 known companies
python verify_earnings.py --data-dir ./data        # the user's real files
```

**Keys** (free): OpenDART (`DART_KEY`), EDINET (`EDINET_KEY`), J-Quants V2
(`JQUANTS_KEY`, sent as `x-api-key`). SEC/EDGAR needs no key (just a User-Agent).
The user has all three keys — ask them to paste them; **never commit keys**
(`.env` and `cache/` are gitignored).

## The user's data files (their schema, delimiter `;`)

- `FA.csv` — `company_id;fiscal_year;fiscal_quarter;calendar_year;calendar_quarter;financial_code;financial_value;financial_report_value`
- `Seg_Seg_Revenue`, `Seg_Seg_Operating_Income` — business segments; `...;segment_code;financial_code;financial_value;financial_report_value`
- `Seg_Geo_Revenue`, `Seg_Geo_Operating_Income` — geographic; same columns
- `company_id_mapping` — `company_id;external_mapped_name` (now auto-consumed)

The user confirmed there are **exactly six files** (the five above + `company_id_mapping`);
the earlier "7th unknown file" was a miscount — there is no 7th file.

`segment_code` looks like `2020Q2_China` / `2020Q1_Semiconductors` — the code
strips the leading `\d{4}Q\d_` and uses the remaining label. Some Seg rows have
only `financial_value` (no `financial_report_value`).

## Architecture (verify_earnings.py)

- `CANONICAL` — metric registry: kind `flow`/`stock`, `per_share`.
- Sources (each exposes `quarterly(api_id, metric, fye_month, years) -> {(cal_y,cal_q): value_local}`):
  - `EdgarSource` (US) — `companyconcept` XBRL; discrete quarters (~90d frames), Q4 = FY − (Q1+Q2+Q3); balance sheet via instant facts.
  - `EdgarDimensional` (US segment/geo) — parses filing **XBRL instances** (`*_htm.xml`) for dimensional facts on `StatementBusinessSegmentsAxis` / `StatementGeographicalAxis`. Handles the `ConsolidationItemsAxis=OperatingSegmentsMember` qualifier. Quarterly from 10-Qs (~90d).
  - `OpenDartSource` (KR) — statements via `fnlttSinglAcntAll` (interim `thstrm_amount` auto-detected discrete-vs-cumulative; balance sheet; downloads `corpCode.xml` once, ~3.5 MB). **Segment/geo:** `segment_quarterly` downloads the full periodic report (`document.xml`, DS001), locates the note via `_note_windows`, and parses HTML tables — `_region_value` (지역별 geographic) and `_segment_revenue` (보고부문 business segment).
  - `FinMindSource` (TW) — `TaiwanStockFinancialStatements` (discrete quarters) + `TaiwanStockBalanceSheet`. Equity field is `Equity`.
  - `AKShareSource` (CN, prototype) — China A-share statements from the Eastmoney F10 abstract endpoints (`RPT_DMSK_FN_INCOME`/`RPT_DMSK_FN_BALANCE`) — the same data AKShare wraps, called directly with stdlib (AKShare itself needs pandas + a build-fragile antlr4/jsonpath chain, so it's NOT a dependency). Income statement is YTD-cumulative → de-cumulated to discrete quarters; balance sheet point-in-time; full RMB. Code = A-share number (`002156`→`.SZ`, `600104`→`.SH`, `68xxxx`→`.SH`/STAR). **Segment/geo:** the 主营构成 endpoint (`RPT_F10_FN_MAINOP`) has geographic 分地区 (境内/境外) and business 分产品/分行业 — BUT only in the ANNUAL and HALF-YEAR reports (12-31 / 06-30), never Q1/Q3, and cumulative. **The user's files are all discrete quarterly**, so there is no comparable figure → `segment_quarterly` returns `{}` (row reported MISSING with a China-specific note; never a false discrete-vs-cumulative mismatch). The semi-annual extraction is kept as `semiannual_composition()` for manual/aggregate spot-checks (e.g. confirm the user's Q1+Q2 sums to the disclosed H1). A real quarterly China segment/geo feed does not exist (companies don't file it). Validated on TongFu (002156): statements' discrete quarters sum to the reported annual (FY2024 revenue = RMB 23,881.68 M), balance sheet A=L+E; geo 境内+境外 = total revenue (FY2025 9,327.6 + 18,593.8 = 27,921.4 M), segment 集成电路封装测试 FY2025 = 27,247.6 M — all MATCH end-to-end.
  - `MopsTwSource` (TW segment/geo) — downloads the consolidated IFRS financial-report book (`…_AI1.pdf`) from `doc.twse.com.tw` (two-step: POST step=9 → follow the `/pdf/…` link; no key) and parses its text layer with `pdfplumber`. **Geographic** revenue from the 營業收入 note's 地區別 table (regions-as-rows; discrete 3-month column + 9-month cumulative column; Q4 = full-year − 9-month). **Business-segment** revenue from the 部門資訊 note's 來自外部客戶收入 row (segments-as-columns; Q4 = full-year − Q1–Q3). Unit is NT$ 仟元 (×1e3). Every table is validated against its printed total → a misread returns nothing (never a false MATCH). Caches only the extracted note text, not the multi-MB PDF.
  - `EdinetSource` (JP) — discovers annual (docType 120) + quarterly (140) reports by scanning statutory filing windows (cached per date; **slow first run per company**), reads YTD `*Duration` values and **de-cumulates**; balance sheet via `*Instant`. **EPS excluded** (YTD EPS is restated across stock splits → differencing invalid).
  - `JQuantsSource` (JP) — V2 `/fins/summary` (TDnet 決算短信); YTD values de-cumulated; free plan = rolling ~2yr + ~12-week delay.
  - `JapanSource` — composite: EDINET (history) + J-Quants (recent), J-Quants wins on overlap.
- `run()` — per row: resolve company (registry) → map financial_code (metric_map) → fetch (memoized per company/metric, only needed years) → normalize to millions (÷1e6; per-share direct) → compare (1% money, ±0.02 EPS) → status.
- Segment rows are routed by market: US → `EdgarDimensional`; JP →
  `EdinetSource.segment_quarterly` (dimensional segment/geo facts from EDINET
  reports, de-cumulated); KR → `OpenDartSource.segment_quarterly` (parses the
  영업부문/보고부문 note tables from `document.xml`; geographic AND business-segment
  revenue, all four quarters); TW → `MopsTwSource.segment_quarterly` (geographic
  AND business-segment revenue parsed from the MOPS financial-report-book PDF).

## Config (user-editable, drives everything)

- `config/company_registry.csv` — `company_id → market, api_id, fye_month, name`. **Required.** api_id = ticker/CIK (us), KRX code (kr), TWSE code (tw), sec code (jp).
- `config/metric_map.csv` — `financial_code → canonical_metric`. Only common codes seeded; unmapped → `NO_MAPPING`.
- `config/segment_members.csv` — `(company_id, label) → member`. US: exact XBRL
  member local-name for business segments / custom regions (country geo is
  built-in via `GEO_MEMBER`). JP: a **substring** of the EDINET member local-name
  (e.g. `GameAndNetworkServices`). KR: the 부문 name in the reportable-segment note
  for BUSINESS segments (e.g. `DS`); KR geographic needs no mapping (region names
  are auto-matched, country- and continent-level).

## Verified working (self-test, live)

All four markets, income statement + balance sheet; US all four `Seg_*` files.
Self-test = **21 MATCH** / 1 MISMATCH (planted) / 1 MISSING (Qorvo geo op-income,
not disclosed) / 1 not-configured. Companies: Qorvo (US), DB HiTek (KR),
Marketech 6196 (TW), Socionext 6526 (JP), **TSMC 2330 (TW geo)**, **Acer 2353 (TW
segment)**. The 5 new TW rows all MATCH live: TSMC geographic revenue US/China/
Japan 2023Q3 (discrete) + US 2023Q4 (annual − 9-month de-cumulation), and Acer
資通訊產品事業群 2023Q3. (Baseline before the TW work was 16 MATCH.)

| | US | KR | TW | JP |
|---|---|---|---|---|
| Income statement (rev/COGS/op-inc/pre-tax/net-inc/EPS) | ✅ | ✅ | ✅ | ✅ (EPS recent-only, J-Quants; diluted EPS n/a) |
| Balance sheet (assets/liabs/equity) | ✅ | ✅ | ✅ | ✅ |
| Segment / geo (4 files) | ✅ | 🟡 geo + segment revenue, all 4 qtrs (DART notes) | 🟡 geo + segment revenue (MOPS PDF book) | ✅ (EDINET dimensional XBRL) |

## Key assumptions — ✅ NOW VALIDATED against real sample rows

The user provided real `FA.csv` rows (companies 154 Socionext / 157 Qorvo /
158 Acer / 159 ADTechnology, + others) and the `company_id_mapping`. All three
core assumptions checked out:

1. **Compare `financial_report_value`** ✅. For every money metric,
   `financial_report_value / financial_value` = that currency's USD FX rate
   (JPY≈137, TWD≈31.2, KRW≈1218) and exactly 1.0 for US filers. So
   `financial_report_value` is the as-filed local figure; `financial_value` is
   FX-converted to USD. Comparing `financial_report_value` is correct.
2. **Millions of local currency** ✅. Verified to the cent against live APIs:
   Acer COGS 2019Q3 = FinMind NT$56,207,007,000 → 56,207.007 vs file 56207.01;
   ADTechnology current assets 2020Q1 = DART ₩103,268,625,174 → 103,268.625 vs
   file 103268.62. EPS is a raw per-share local figure (Socionext diluted EPS
   44.28 JPY), compared directly — not millions.
3. **Discrete quarters, period-end match** ✅ **after a fix.** `cal_key_from_date`
   now snaps the period-end to the **nearest calendar quarter-end** before taking
   the quarter. 52/53-week filers (Qorvo) end quarters 1–6 days into the next
   month (2023-04-01, 2020-10-03, 2022-01-01…); the old code bucketed ~half of
   Qorvo's history into the wrong calendar quarter. Fix validated on real Qorvo
   revenue/pretax dates; self-test unchanged.

### Metric coverage & de-cumulation (from the calibration round)
- **Metric taxonomy.** Real `FA.csv` is dominated by **derived** codes (margins,
  turnover days, cash-conversion cycle, `*_QOQ`/`*_YOY` deltas). These are now
  categorized `UNSUPPORTED_DERIVED` (can't reconcile a computed ratio against one
  API field, and must never be ÷1e6). Directly-fetchable codes added & wired
  across all four sources with **verified field names**: COGS, GROSS_PROFIT,
  PRE_TAX_INCOME, CURRENT_ASSETS, CURRENT_LIABILITIES, TOTAL_LIABILITIES,
  EPS_DILUTED (EPS_DILUTED is US/KR/TW only — JP sources don't expose it).
- **EDGAR YTD-ladder de-cumulation.** US filers that report an income item only
  as year-to-date cumulatives (not discrete 90-day frames) now get all four
  quarters via de-cumulation (additive `setdefault` fallback; every value is a
  one-quarter difference). Verified on Qorvo pretax FY2025.
- **Known real-data limitation:** Qorvo pretax pre-FY2025 is filed **annual-only**
  in EDGAR companyconcept (no quarterly/YTD facts), so e.g. 2023Q1 legitimately
  returns `MISSING_IN_API` — a data-availability gap, not a tool bug.

### Finance-arm ACCOUNTS_RECEIVABLE (GM / Ford) — the user's convention
For captive-finance filers, the user's `ACCOUNTS_RECEIVABLE` = automotive **trade**
receivables (current) **+ the finance subsidiary's receivables (current portion)**.
`us-gaap:AccountsReceivableNetCurrent` (companyconcept) is trade-only, so the finance
line is read straight from the filing's **XBRL instance** (companyconcept never
carries it): `EdgarDimensional.instant_series()` parses all point-in-time facts in
`<doc>_htm.xml` and, per element per instant, takes the fact with the **fewest
dimensions** (the aggregate, not a portfolio/segment sub-breakdown), then sums.
Config: `US_FINANCE_RECEIVABLE` (CIK → extra current finance-receivable element
local-names). Both use `NotesAndLoansReceivableNetCurrent` — GM's is dimensioned on
`BusinessGroupAxis=GmFinancialMember`, Ford's is an undimensioned face line; the
fewest-dimensions rule handles both. **Non-current** finance receivables are
excluded. Every other US filer is untouched (trade only). **Verified:** GM 2026Q1 =
16,381 + 43,751 = **60,132**; Ford 2026Q1 = 17,227 + 46,185 = **63,412** (US$M).

### SG&A excluding R&D — the user's convention (KR done, JP no-value)
The user's SG&A **excludes** R&D (US ON Semi SGA = S&M + G&A, no R&D). KR 판매비와관리비
and JP 販管費 both **include** R&D, so it must be subtracted.
- **Korea (done).** `OpenDartSource._sga_note_rd()` reads the R&D line
  (경상연구개발비 / 경상개발비 / 연구비, …) from the **판매비와관리비 functional-breakdown
  note** in `document.xml` (validated: the table carries the 판매비와관리비 total row and
  a 급여 row; first qualifying table in doc order = the 연결/consolidated note; unit
  from the 단위 hint just before the `<TABLE>`). `_sga_rd_series()` de-cumulates it
  with the *same* `_to_discrete` machinery as the SGA total, and `quarterly()`
  subtracts. Filers with R&D in COGS have no SG&A R&D row → nothing subtracted (SGA
  unchanged). **Verified:** Hyundai (005380) 2025Q3 판관비 5,746,793 − R&D 640,577 =
  **5,106,216** (₩M). Note: the SG&A R&D label was renamed 연구비→경상개발비 between the
  2024 and 2025 filings — matched on 연구/개발 within the validated note, not a fixed label.
  Consistency rule: once a year is known to carry R&D in SG&A, every quarter must be
  R&D-excluded; a quarter whose R&D can't be recovered is DROPPED (no value) rather than
  left R&D-inclusive. This happens for fabless filers like LX Semicon (108320) — R&D is
  ~56% of its SG&A, and its ANNUAL report doesn't repeat the 급여-bearing functional
  breakdown the interim reports use, so its Q4 SG&A is reported as no value (Q1–Q3 remain
  correctly R&D-excluded). Hyundai's annual DOES repeat the breakdown, so all four
  quarters resolve.
- **Japan (no-value by user decision).** EDINET tags R&D-in-SG&A
  (`jppfs_cor:ResearchAndDevelopmentExpensesSGA`, 一般管理費に含まれる研究開発費) **only in
  the annual (and some half-year) securities report — never reliably per quarter**
  (confirmed on Socionext, Tokyo Seimitsu, Mitsubishi Gas Chemical), so a clean
  discrete-quarter subtraction like Korea's isn't derivable. Per the user's choice
  ("no value over a wrong, R&D-inclusive value"), `EdinetSource.quarterly()` subtracts
  R&D only for YTD points that actually disclose it and **drops** the rest — so JP
  `SGA_EXPENSE` reports a value only where it can be correctly R&D-excluded, else
  MISSING. In practice most/all JP quarters report no value. (JP SGA has no J-Quants
  fallback, so this fully governs JP SGA.) No confirmed JP target yet — best-effort.

## Not done / next steps (roughly by value)

1. ~~**Calibrate against real data**~~ **DONE** (see "Key assumptions" above).
2. ~~**Wire `company_id_mapping`**~~ **DONE.** Auto-loads the mapping, fills
   `company_name`, prints a "COMPANIES TO CONFIGURE" to-do list. Still doesn't
   resolve name → market/api_id (unreliable); the user fills those.
3. ~~**Japan segment/geo**~~ **DONE (structured path).** JP reportable-segment
   (and geographic) figures ARE dimensional XBRL in EDINET securities reports —
   the member is baked into the context id (e.g. `CurrentQuarterDuration_...
   GameAndNetworkServicesReportableSegmentMember`). `EdinetSource.segment_quarterly`
   reads the YTD value per member and de-cumulates to discrete quarters (element
   id picked heuristically by local-name; external "ToCustomers" revenue preferred).
   Validated on Sony (Game/Music segment revenue + operating income, discrete
   quarters summing to the annual). Members are mapped per company in
   `segment_members.csv` (JP member = a substring of the XBRL member local-name).
   Caveat: single-segment filers (Socionext) and post-Apr-2024 periods (no more
   四半期 reports) have little/no structured segment data → partial coverage.
4. **Korea segment/geo** — **DONE for geographic AND business-segment revenue (all
   four quarters).** `OpenDartSource.segment_quarterly` downloads the full periodic
   report (`document.xml`, DS001) and parses HTML note tables (`<TH>`/`<TD>`/`<TE>`
   cells; unit-aware 백만원/천원/억원). Note location is via `_note_windows`, which
   yields a window around EVERY anchor occurrence and the caller tries each in turn
   — ordered most-specific-anchor first, then DOCUMENT order, so the CONSOLIDATED
   (연결) note is preferred over the separate (별도) one. (A fixed position threshold
   failed — DB HiTek's note is at ~39% of the doc, LG Electronics' at ~20%.)
   - **Geographic** (`_region_value`): Q1–Q3 read the discrete 3개월 column; Q4 =
     annual − 9-month, where the annual (사업보고서) note is **transposed**
     (regions-as-columns). Country- and continent-level region canonicalization.
     Validated on DB HiTek: China 2023 Q1–Q4 (162,798 / 169,257 / 167,085 /
     166,475 백만원) all exact.
   - **Business segment** (`_segment_revenue`): reads the reportable-segment
     (보고부문) note — discrete 당분기(3개월) table for Q1–Q3, annual − 9-month for Q4;
     handles transposed (segments-as-columns, 매출액 row) and segments-as-rows;
     label→부문 mapping via `segment_members.csv`. Validated on Samsung
     (DX/DS/SDC/Harman): discrete quarters sum exactly to the 9-month figure, and
     FY2023 DS = ₩66.59 tn matches the filing. Single-segment filers correctly
     return nothing (no false match).
   - **Skipped by request:** segment & geographic **operating income** (not
     consistently disclosed) → MISSING.
5. **Taiwan segment/geo** — **DONE for geographic AND business-segment revenue**
   via the MOPS financial-report-book PDF (`MopsTwSource`). The path that worked,
   after ruling the others out:
   - **XBRL is a dead end.** TWSE OpenAPI `t187ap06_*` and FinMind
     `TaiwanStockFinancialStatements` are statement-level only. The MOPS t164
     report (`mopsov.twse.com.tw/server-java/t164sb01?…&REPORT_ID=C`, big5) is the
     XBRL-*derived* HTML view and, like the underlying instance, carries only the
     primary statements + investment/endorsement disclosures — **not** the 營運部門
     / 地區別 revenue note. Taiwan's public XBRL does not dimensionally tag the note
     (unlike EDGAR/EDINET). Confirmed by inspecting the t164 output directly.
   - **The note lives only in the PDF financial-report book (財務報告書).** It IS
     downloadable, keyless: POST to `doc.twse.com.tw/server-java/t57sb01` with
     `step=9&kind=A&co_id=<coid>&filename=<YYYY0Q>_<coid>_AI1.pdf`, then follow the
     returned `/pdf/…` link. `AI1` = consolidated IFRS book (what we want).
   - **`MopsTwSource` parses it** (see architecture). Geographic 地區別 table has a
     discrete 3-month column (Q1–Q3 direct) + a 9-month cumulative column (Q4 =
     annual − 9M). Business-segment 部門資訊 note gives the discrete 來自外部客戶收入
     row per quarter (Q4 = annual − Q1–Q3). Unit is 仟元 (×1e3); **the note slice
     often omits the unit header, so the source DEFAULTS to 1e3** (the regulatory
     standard) rather than trusting `_unit_multiplier`'s 1.0 fallback — this was a
     real bug caught by the Acer sum-check (values came out 1000× low).
   - **Validated:** TSMC (2330) geographic revenue 2023 Q1–Q4 for all six regions,
     discrete quarters summing exactly to the disclosed annual (US 2023Q3 =
     360,671 M ≈ 66%, matches FinMind total revenue); Acer (2353) business-segment
     revenue (資通訊產品事業群 / 其他事業群) summing to the annual. TSMC is
     single-segment (foundry) so it has no business-segment split — correct empty.
   - **Limits / fragility (documented honestly):** the note is PDF text — companies
     whose 部門資訊 table is typeset vertically (character-per-line, e.g. Marketech
     6196) don't parse; and many TW companies don't disclose a 地區別 table at all
     (Acer/Marketech don't). Both cases → `MISSING_IN_API`, never a false MATCH
     (guarded by the per-table sum-to-total check). Op-income by segment/region is
     skipped (rarely disclosed). PDFs are multi-MB → slow on first fetch, cached
     after (only the extracted note text is cached).
6. **Expand `metric_map` + per-source field maps** — income statement (rev, COGS,
   gross profit, op-inc, pre-tax, net-inc, basic/diluted EPS) and balance sheet
   (current/total assets, A/P, current/total liabilities, equity) are wired with
   verified field names. Add more `financial_code`s as new ones appear.
7. **Full-run performance** — EDINET date-scanning is slow for many JP companies
   × years on first run (cached after), and now segment extraction adds more
   report fetches. Consider a prebuilt EDINET doc index if the JP universe is
   large. (Cold-cache runs can transiently miss a JP report; a warm-cache re-run
   fixes it.)

## Gotchas / lessons

- EDINET has **no company-filter endpoint** → discovery scans dates (cached per
  date, shared across companies). `fye_month` drives which windows to scan.
- OpenDART interim IS `thstrm_amount` is discrete-quarter for some filers,
  cumulative for others → auto-detected by the Q3/FY ratio.
- EDGAR dimensional segment facts carry a **second axis**
  (`ConsolidationItemsAxis=OperatingSegmentsMember`) — don't filter to single-axis.
- EDGAR revenue tag varies: try `Revenues`,
  `RevenueFromContractWithCustomerExcludingAssessedTax`, etc.
- J-Quants free window **rolls forward** — cache older quarters sooner.
- The `segment_code` period prefix is **unreliable** (e.g. Amazon `2020Q3_AWS`
  row with calendar cols 2025 Q3) — always use the `calendar_year`/`calendar_quarter`
  columns for the period; the prefix is only stripped to get the label.
- KR note parsing (`document.xml`): DART markup uses `<TH>`/`<TD>` **and `<TE>`**
  body cells; units are per-note (`백만원`/`천원`/`억원` — overview tables often 억원,
  notes 백만원); the segment note appears in both the **연결 (consolidated)** and
  **별도 (separate)** statements — prefer consolidated (comes first in doc order);
  quarterly notes give 당분기(3개월)+누적, annual notes a single 당기 table
  (sometimes transposed with the dimension across the header).
- A single phrase anchor is fragile (the phrase recurs in overview + both note
  sections); `_note_windows` returns all occurrences and the caller tries each —
  reuse this pattern for any new footnote-parsing source (e.g. Taiwan).
