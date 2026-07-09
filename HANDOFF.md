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
- Branch: `claude/earnings-api-research-kwe3km`. All work is committed & pushed.

## Run it

```bash
pip install -r requirements.txt
export DART_KEY=...  EDINET_KEY=...  JQUANTS_KEY=...  SEC_USER_AGENT="you@example.com"
python verify_earnings.py --self-test              # live check on 4 known companies
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
  - `OpenDartSource` (KR) — `fnlttSinglAcntAll`; interim `thstrm_amount` auto-detected discrete-vs-cumulative; balance sheet. Downloads `corpCode.xml` once (~3.5 MB, slow).
  - `FinMindSource` (TW) — `TaiwanStockFinancialStatements` (discrete quarters) + `TaiwanStockBalanceSheet`. Equity field is `Equity`.
  - `EdinetSource` (JP) — discovers annual (docType 120) + quarterly (140) reports by scanning statutory filing windows (cached per date; **slow first run per company**), reads YTD `*Duration` values and **de-cumulates**; balance sheet via `*Instant`. **EPS excluded** (YTD EPS is restated across stock splits → differencing invalid).
  - `JQuantsSource` (JP) — V2 `/fins/summary` (TDnet 決算短信); YTD values de-cumulated; free plan = rolling ~2yr + ~12-week delay.
  - `JapanSource` — composite: EDINET (history) + J-Quants (recent), J-Quants wins on overlap.
- `run()` — per row: resolve company (registry) → map financial_code (metric_map) → fetch (memoized per company/metric, only needed years) → normalize to millions (÷1e6; per-share direct) → compare (1% money, ±0.02 EPS) → status.
- Segment rows are routed by market: US → `EdgarDimensional`; JP →
  `EdinetSource.segment_quarterly` (dimensional segment/geo facts from EDINET
  reports, de-cumulated); KR → `OpenDartSource.segment_quarterly` (parses the
  영업부문 note tables from `document.xml`; geographic revenue all four quarters);
  TW → `SEGMENT_SOURCE_UNAVAILABLE` (footnote-only MOPS PDF, no free API). KR
  business-segment rows → `UNSUPPORTED_SEGMENT` (note tables too filer-variable).

## Config (user-editable, drives everything)

- `config/company_registry.csv` — `company_id → market, api_id, fye_month, name`. **Required.** api_id = ticker/CIK (us), KRX code (kr), TWSE code (tw), sec code (jp).
- `config/metric_map.csv` — `financial_code → canonical_metric`. Only common codes seeded; unmapped → `NO_MAPPING`.
- `config/segment_members.csv` — `(company_id, label) → XBRL member`. US: exact
  member local-name for business segments / custom regions (country geo is
  built-in via `GEO_MEMBER`). JP: a **substring** of the EDINET member local-name
  (e.g. `GameAndNetworkServices`).

## Verified working (self-test, live)

All four markets, income statement + balance sheet; US all four `Seg_*` files.
Self-test = 16 MATCH / 1 MISMATCH (planted) / 1 MISSING (Qorvo geo op-income,
not disclosed) / 1 not-configured. Companies: Qorvo (US), DB HiTek (KR),
Marketech 6196 (TW), Socionext 6526 (JP).

| | US | KR | TW | JP |
|---|---|---|---|---|
| Income statement (rev/COGS/op-inc/pre-tax/net-inc/EPS) | ✅ | ✅ | ✅ | ✅ (EPS recent-only, J-Quants; diluted EPS n/a) |
| Balance sheet (assets/liabs/equity) | ✅ | ✅ | ✅ | ✅ |
| Segment / geo (4 files) | ✅ | 🟡 geo revenue all 4 qtrs (DART notes) | ⛔ footnote-only (MOPS PDF) | ✅ (EDINET dimensional XBRL) |

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

### New this round
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
4. **Korea segment/geo** — **DONE for geographic revenue (all four quarters).**
   `OpenDartSource.segment_quarterly` downloads the full periodic report
   (`document.xml`, DS001), finds the 영업부문 note by phrase-anchoring on the LAST
   occurrence of the note phrase (a fixed position threshold fails — DB HiTek's
   note is at ~39% of the doc, LG Electronics' at ~20%), and parses its HTML
   tables (`<TH>`/`<TD>`/`<TE>` cells), unit-aware (백만원/천원), with country- and
   continent-level region canonicalization. Q1–Q3 read the discrete 3개월 column;
   **Q4 = annual − 9-month cumulative**, where the annual (사업보고서) note uses a
   **transposed** regions-as-columns layout (handled). Validated on DB HiTek:
   China 2023 Q1–Q4 (162,798 / 169,257 / 167,085 / 166,475 백만원) all exact.
   **Scope decisions:** geo **operating income** isn't disclosed by region in KR
   filings (→ MISSING); **business-segment** (영업부문) rows → `UNSUPPORTED_SEGMENT`
   — those note tables are too filer-variable to parse reliably (labels are
   H&A/HE/VS vs 본부; overview-vs-note; transposed), and shipping fragile extraction
   into a reconciliation tool risks a false MATCH; most KR filers here are
   single-segment anyway. (If needed later, build per-company segment mapping.)
5. **Taiwan segment/geo** — **not available via any free API** (probed concretely,
   not just researched):
   - TWSE OpenAPI `t187ap06_*` and FinMind `TaiwanStockFinancialStatements` are
     statement-level only (revenue/COGS/gross/op-inc/pretax/EPS) — no segment/geo.
   - The MOPS financial-statement HTML report (`mopsov.twse.com.tw/server-java/
     t164sb01?step=1&CO_ID=2330&SYEAR=..&SSEASON=..&REPORT_ID=C`, big5) IS
     reachable and has the four primary statements + investment/endorsement
     disclosures, but **not** the 營運部門 / 地區別 revenue note — the region-revenue
     terms (美洲/歐洲/北美/其他地區) and 部門 are absent. That note lives only in the
     separate PDF financial-report book (財務報告書) / annual report.
   - So TW rows return `SEGMENT_SOURCE_UNAVAILABLE`. A real implementation needs a
     MOPS **PDF** table extractor (or the TIFRS XBRL instance if it dimensionally
     tags 營運部門 — unverified), or a paid feed (TEJ / Capital IQ / Refinitiv).
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
