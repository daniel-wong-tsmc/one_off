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
| 🇯🇵 Japan | J-Quants V2 + EDINET | `JQUANTS_KEY`, `EDINET_KEY` (free) | quarterly recent ~2yr (J-Quants); annual (EDINET); pre-2024 quarters pending |

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
3. **Quarters.** Compared as **discrete (3-month) quarters**, matched by the
   period-end date's calendar year/quarter — so fiscal-vs-calendar offsets (e.g.
   Qorvo's March year-end) are handled automatically. US Q4 and Korea Q4 are
   derived as `FY − (Q1+Q2+Q3)`. Korea interim figures are auto-detected as
   discrete vs. cumulative.
4. **Tolerance.** 1% relative for money, ±0.02 absolute for EPS. Tune at the top
   of `verify_earnings.py`.

## Status codes in the output

| Status | Meaning |
|--------|---------|
| `MATCH` | API agrees with your file (within tolerance) |
| `MISMATCH` | **API and file disagree** — the thing you asked for |
| `MISSING_IN_API` | source has no value for that period (e.g. JP quarterly) |
| `NO_MAPPING` | `financial_code` not in `metric_map.csv` |
| `COMPANY_NOT_CONFIGURED` | `company_id` not in `company_registry.csv` |
| `UNSUPPORTED_METRIC` | that market's source doesn't expose that metric |
| `UNSUPPORTED_SEGMENT` | segment/geo file — not auto-verified in v1 |
| `SOURCE_UNAVAILABLE` | key missing for that market |
| `BAD_FILE_VALUE` / `ERROR` | unparseable value / fetch error |

## Known limitations (v1)

- **Japan quarterly coverage is split across two sources.** J-Quants V2
  (`/fins/summary`, TDnet 決算短信) supplies **discrete recent quarters** — its
  free plan gives a rolling ~2 years plus a ~12-week delay, which covers exactly
  the Q1/Q3 periods EDINET dropped after April 2024. EDINET supplies annual
  securities-report data. The remaining gap is **pre-2024 quarterly history**
  (older than the J-Quants free window): those rows return `MISSING_IN_API`
  until EDINET 四半期報告書 parsing is added. Because the J-Quants free window
  rolls forward, pull/cache older quarters sooner rather than later.
- **Segment & geographic files are not auto-verified.** Business-segment and
  geographic splits live in filing *footnotes*, not clean numeric API fields
  (in Socionext's report, geographic revenue sits inside a text block, and it's
  a single-segment filer with no business-segment split at all). These rows are
  flagged `UNSUPPORTED_SEGMENT`. Feasible follow-ups: US dimensional-XBRL
  segments via EDGAR, and Japan note-text parsing.
- **Metric coverage** starts with revenue, operating income, net income, EPS,
  and a few balance-sheet items. Extend `metric_map.csv` (and the per-market
  field maps in the source classes) to cover more `financial_code`s.

## Caching

API responses cache under `cache/` (gitignored). Delete it to force a refresh.
The first Korea run downloads the ~3.5 MB OpenDART corp-code file once.
