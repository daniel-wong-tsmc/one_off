# Earnings verification toolkit

Pulls publicly-filed quarterly financials for companies in **US, Korea, Taiwan,
and Japan** from free official/near-official APIs and reconciles them against
your local CSV files, then reports **which company, for which metric and period,
does not match**.

| Market | Source | Key needed | Coverage in v1 |
|--------|--------|-----------|----------------|
| 🇺🇸 US | SEC EDGAR | none (User-Agent only) | quarterly, full history |
| 🇰🇷 Korea | OpenDART | `DART_KEY` (free) | quarterly, full history |
| 🇹🇼 Taiwan | FinMind | `FINMIND_TOKEN` optional | quarterly, full history |
| 🇯🇵 Japan | J-Quants V2 + EDINET | `JQUANTS_KEY`, `EDINET_KEY` (free) | quarterly recent ~2yr (J-Quants) + pre-2024 quarterly & annual (EDINET 四半期/有価証券報告書) |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DART_KEY and EDINET_KEY
set -a; source .env; set +a   # export the vars
```

## Configure your companies and metrics

Two small CSVs under `config/` drive everything:

- **`config/company_registry.csv`** — map each `company_id` (from your
  `company_id_mapping` file) to its market and API id:
  `us`→ticker/CIK, `kr`→KRX code, `tw`→TWSE code, `jp`→sec code. Set `fye_month`
  for non-December fiscal years (e.g. Qorvo = 3, Socionext = 3).
- **`config/metric_map.csv`** — map your `financial_code` values (e.g.
  `REVENUE`, `ACCOUNTS_PAYABLE`) to a canonical metric. Codes not listed here are
  reported as `NO_MAPPING` rather than silently skipped.

Your **`company_id_mapping`** file (`company_id;external_mapped_name`) is picked
up automatically from `--data-dir` (a `.csv` suffix is optional; override the
name with `--mapping-file`). It supplies human-readable company names to the
output and, at the end of a run, prints a **"COMPANIES TO CONFIGURE"** to-do
list of every `company_id` that appears in your data but is still missing from
`company_registry.csv`. It does *not* resolve a company to its market/API id —
a name can't be turned into a KRX/TWSE/sec code reliably, so you still fill in
`market` and `api_id` yourself.

## Run

```bash
# your files live in ./data (FA.csv, Seg_*.csv). Filenames configurable in code.
python verify_earnings.py --data-dir ./data --out-dir ./out

# quick live check against 4 known-good companies (needs the two keys):
python verify_earnings.py --self-test
```

Outputs:
- `out/verification_results.csv` — every row with a status.
- `out/mismatches.csv` — only the rows that disagree.
- console — a status summary and the list of mismatches.

## What it compares (assumptions — please confirm against your data)

1. **Value column.** It reconciles the API value against your
   **`financial_report_value`** column (the as-filed, local-currency figure).
   Your `financial_value` column looks FX-converted (for TSMC,
   `financial_report_value / financial_value ≈ 30.9`, the TWD/USD rate), which
   the script can't reproduce, so it isn't used. Switch with
   `--compare-column financial_value` if that's wrong.
2. **Units.** Your `financial_report_value` is assumed to be in **millions of
   local reporting currency** (TSMC accounts payable `27661.85` ⇒ NT$27.66 bn).
   Every source returns full local currency, which the script divides by 1e6
   before comparing. Per-share metrics (EPS) are compared directly.
3. **Quarters.** Compared as **discrete (3-month) quarters**, matched to the
   calendar quarter the fiscal period falls in. The period-end date is snapped to
   the **nearest calendar quarter-end** first, so 52/53-week filers whose quarters
   end a few days into the next month (e.g. Qorvo's `2023-04-01` fiscal-Q4 end, or
   `2020-10-03`) are attributed to the quarter they belong to (Q1, Q3) rather than
   the following one. US Q4 and Korea Q4 are derived as `FY − (Q1+Q2+Q3)`. Korea
   interim figures are auto-detected as discrete vs. cumulative.

   *Confirmed against real data:* `financial_report_value / financial_value`
   equals the local-currency/USD FX rate for money metrics (JPY≈137, TWD≈31,
   KRW≈1218) and exactly 1.0 for US filers — so `financial_report_value` is the
   as-filed local figure and `financial_value` is FX-converted to USD. Reconciled
   values verified to the cent: Acer COGS 2019Q3 = NT$56,207M, ADTechnology
   current assets 2020Q1 = ₩103,269M.
4. **Tolerance.** 1% relative for money, ±0.02 absolute for EPS. Tune at the top
   of `verify_earnings.py`.

## Status codes in the output

| Status | Meaning |
|--------|---------|
| `MATCH` | API agrees with your file (within tolerance) |
| `MISMATCH` | **API and file disagree** — the thing you asked for |
| `MISSING_IN_API` | source has no value for that period (e.g. JP quarterly) |
| `NO_MAPPING` | `financial_code` not in `metric_map.csv` |
| `UNSUPPORTED_DERIVED` | computed ratio / turnover-days / QoQ-delta metric (e.g. `NET_MARGIN`, `CASH_CONVERSION_CYCLE`, `*_QOQ`) — not a single as-filed line item, so not reconcilable against one API field |
| `COMPANY_NOT_CONFIGURED` | `company_id` not in `company_registry.csv` |
| `UNSUPPORTED_METRIC` | that market's source doesn't expose that metric |
| `UNSUPPORTED_SEGMENT` | segment/geo row for a non-US company (pilot is US-only) |
| `NO_SEGMENT_MAPPING` | US segment/geo label not resolvable — add to `segment_members.csv` |
| `SOURCE_UNAVAILABLE` | key missing for that market |
| `BAD_FILE_VALUE` / `ERROR` | unparseable value / fetch error |

## Known limitations (v1)

- **Japan quarterly coverage is split across two sources**, merged
  automatically (J-Quants wins on overlap):
  - **J-Quants V2** (`/fins/summary`, TDnet 決算短信) — discrete recent quarters;
    free plan is a rolling ~2 years + ~12-week delay, covering the Q1/Q3 periods
    EDINET dropped after April 2024.
  - **EDINET** — annual 有価証券報告書 **and** pre-2024 quarterly 四半期報告書.
    Quarterly reports are discovered by scanning the statutory filing window
    after each period end (slow on first run for a company, then cached), and
    their year-to-date XBRL values are de-cumulated into discrete quarters.
  - **EDINET covers revenue / operating income / net income** (de-cumulated)
    **and balance-sheet items** (total assets, trade payables, net assets — read
    at instant contexts, point-in-time). **EPS is intentionally excluded**:
    year-to-date EPS is restated across stock splits (e.g. Socionext FY2024), so
    differencing it is invalid. Japan EPS comes from J-Quants (recent quarters).
  - Any period neither source can reach (older than the J-Quants window *and*
    with no EDINET filing) returns `MISSING_IN_API`. Because the J-Quants free
    window rolls forward, cache older quarters sooner rather than later.
- **Segment & geographic files — US pilot only.** The `Seg_*` files hold
  business-segment and geographic splits, which live in filing footnotes rather
  than clean top-line API fields.
  - **US (EDGAR) is fully implemented for all four `Seg_*` files.** Values are
    read as *dimensional* XBRL facts from the filing instances (`Revenues` /
    `OperatingIncomeLoss` on `StatementBusinessSegmentsAxis` /
    `StatementGeographicalAxis`, with the standard `OperatingSegmentsMember`
    qualifier handled), at discrete quarterly granularity from 10-Qs.
    Geographic *country* labels resolve via a built-in map (China→CN, US→US,
    Taiwan→TW, …); business segments and custom regions are mapped per company
    in `config/segment_members.csv`. Verified on Qorvo: **segment revenue,
    segment operating income, and geographic revenue** (US/China/Taiwan) all
    match at quarterly granularity. Geographic *operating income* is usually not
    disclosed by US filers, so those rows come back `MISSING_IN_API` — expected,
    not a tool gap.
  - **Japan / Korea / Taiwan segment/geo are not yet built** — those rows return
    `UNSUPPORTED_SEGMENT`. In Japan much of this sits in XBRL text blocks
    (Socionext's geographic revenue is prose, and it's single-segment); KR/TW
    need note parsing.
- **Metric coverage.** Income statement: revenue, COGS, gross profit, operating
  income, pre-tax income, net income, basic & diluted EPS. Balance sheet: current
  assets, total assets, accounts payable, current liabilities, total liabilities,
  total equity. (Diluted EPS is US/KR/TW only — Japan's sources don't expose it.)
  Extend `metric_map.csv` (and the per-market field maps in the source classes)
  to cover more `financial_code`s. **Derived** metrics — margins, turnover days,
  cash-conversion cycle, and any `*_QOQ` / `*_YOY` delta — are reported
  `UNSUPPORTED_DERIVED` rather than reconciled, because they're computed from
  primitives with company-specific conventions, not filed as a single line item.

## Caching

API responses cache under `cache/` (gitignored). Delete it to force a refresh.
The first Korea run downloads the ~3.5 MB OpenDART corp-code file once.
