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
- `company_id_mapping` — `company_id;external_mapped_name` (**not consumed by the script yet**)
- There is a **7th file the user never described** — unknown.

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
- Segment rows are routed to `EdgarDimensional` for US; non-US → `UNSUPPORTED_SEGMENT`.

## Config (user-editable, drives everything)

- `config/company_registry.csv` — `company_id → market, api_id, fye_month, name`. **Required.** api_id = ticker/CIK (us), KRX code (kr), TWSE code (tw), sec code (jp).
- `config/metric_map.csv` — `financial_code → canonical_metric`. Only common codes seeded; unmapped → `NO_MAPPING`.
- `config/segment_members.csv` — `(company_id, label) → XBRL member` for US business segments / custom regions (country geo is built-in via `GEO_MEMBER`).

## Verified working (self-test, live)

All four markets, income statement + balance sheet; US all four `Seg_*` files.
Self-test = 16 MATCH / 1 MISMATCH (planted) / 1 MISSING (Qorvo geo op-income,
not disclosed) / 1 not-configured. Companies: Qorvo (US), DB HiTek (KR),
Marketech 6196 (TW), Socionext 6526 (JP).

| | US | KR | TW | JP |
|---|---|---|---|---|
| Income statement (rev/op-inc/net-inc/EPS) | ✅ | ✅ | ✅ | ✅ (EPS recent-only, J-Quants) |
| Balance sheet | ✅ | ✅ | ✅ | ✅ |
| Segment / geo (4 files) | ✅ | — | — | — |

## Key assumptions (⚠️ never validated against the user's REAL files)

1. Compares the **`financial_report_value`** column (as-filed local currency).
   `financial_value` looks FX-converted (TSMC ratio ≈ 30.9 = TWD/USD).
2. `financial_report_value` assumed to be in **millions of local currency**;
   every source is divided by 1e6 before comparison.
3. Quarters compared **discrete**, matched by **period-end calendar quarter**.

**These three assumptions are the biggest risk.** The tool has only ever run on
synthetic sample data. The single most valuable next step is to get the user's
real `FA.csv` + `company_id_mapping` and calibrate units / compare-column /
period semantics against actual values.

## Not done / next steps (roughly by value)

1. **Calibrate against the user's real data** — units, compare column, quarter
   semantics. Nothing has run on the actual files.
2. **Wire `company_id_mapping`** — currently ignored; the user still hand-fills
   `company_registry.csv` with market+api_id (a name can't be auto-resolved to a
   KRX/TWSE/sec code reliably, but the mapping could at least populate names and
   flag unconfigured ids).
3. **Japan segment/geo** — hard: much is XBRL **text-block prose** (Socionext's
   geographic revenue is a text block; it's single-segment with no business
   split). Expect partial results.
4. **Korea / Taiwan segment/geo** — footnote parsing; no clean API. Hardest.
5. **Expand `metric_map` + per-source element/field maps** — only rev/op-inc/
   net-inc/EPS + a few balance-sheet items are wired. Add more `financial_code`s
   as they appear in the real `FA.csv` (we only ever saw `ACCOUNTS_PAYABLE`).
6. **Identify the 7th data file.**
7. **Full-run performance** — EDINET date-scanning is slow for many JP companies
   × many years on first run (cached after). Consider a prebuilt EDINET doc
   index if the JP universe is large.

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
