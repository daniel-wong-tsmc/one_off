# Earnings verification toolkit

Pulls publicly-filed quarterly financials for companies in **US, Korea, Taiwan,
and Japan** from free official/near-official APIs and reconciles them against
your local CSV files, then reports **which company, for which metric and period,
does not match**.

| Market | Source | Key needed | Coverage | Segment/geo |
|--------|--------|-----------|----------|-------------|
| 🇺🇸 US | SEC EDGAR | none (User-Agent only) | quarterly, full history | ✅ EDGAR dimensional XBRL |
| 🇰🇷 Korea | OpenDART | `DART_KEY` (free) | quarterly, full history | 🟡 geographic **and** business-segment revenue (all 4 quarters) from the DART note tables |
| 🇹🇼 Taiwan | FinMind (statements) + MOPS PDF book (segment/geo) | `FINMIND_TOKEN` optional; MOPS needs none | quarterly, full history | 🟡 geographic **and** business-segment revenue, parsed from the MOPS financial-report-book PDF |
| 🇯🇵 Japan | J-Quants V2 + EDINET | `JQUANTS_KEY`, `EDINET_KEY` (free) | quarterly recent ~2yr (J-Quants) + pre-2024 quarterly & annual (EDINET 四半期/有価証券報告書) | ✅ EDINET dimensional XBRL |
| 🇨🇳 China A-shares | Eastmoney F10 (AKShare data) | none | quarterly statements (YTD-cumulative → de-cumulated) | ⛔ not at quarterly granularity — 主营构成 is **semi-annual only**, so it can't reconcile discrete-quarter files (→ `MISSING`) |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DART_KEY and EDINET_KEY
set -a; source .env; set +a   # export the vars
```

## Configure your companies and metrics

Small CSVs under `config/` drive everything:

- **`config/company_registry.csv`** — map each `company_id` (from your
  `company_id_mapping` file) to its market and API id:
  `us`→ticker/CIK, `kr`→KRX code, `tw`→TWSE code, `jp`→sec code. Set `fye_month`
  for non-December fiscal years (e.g. Qorvo = 3, Socionext = 3).
- **`config/metric_map.csv`** — map your `financial_code` values (e.g.
  `REVENUE`, `ACCOUNTS_PAYABLE`) to a canonical metric. Codes not listed here are
  reported as `NO_MAPPING` rather than silently skipped. Ships with the common
  income-statement, balance-sheet and cash-flow lines wired across all markets
  (revenue/COGS/margins-inputs, receivables, cash, inventory, PP&E, equity,
  contract liabilities, R&D/SG&A/tax expense, operating cash flow, capex, …).
  Computed ratios/subtotals are routed to `UNSUPPORTED_DERIVED`, and operational
  or non-GAAP KPIs (headcount, wafer volume/ASP, utilization, backlog, FX, …) to
  `UNSUPPORTED_NONFINANCIAL`, so neither is mistaken for an un-mapped code.
- **`config/completeness_exclude.csv`** *(optional)* — extra `financial_code`
  values `--check-completeness` should not flag as missing (`*`/`?` globs allowed).
  Additive to the built-in derived/operational exclusions. See
  [Find missing values](#find-missing-values-in-your-own-data-completeness-check).

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
# your files live in ./data (FA.csv, Seg_* — a .csv suffix is optional).
python verify_earnings.py --data-dir ./data --out-dir ./out

# also write the API values back in YOUR file schema (to diff against your files):
python verify_earnings.py --data-dir ./data --out-dir ./out --export

# quick live check against 6 known-good companies (US/KR/TW/JP + TSMC & Acer TW seg/geo):
python verify_earnings.py --self-test
```

### Find missing values in your own data (completeness check)

Reconciliation only compares rows you *have* — a value that's simply absent never
surfaces, because there's nothing to compare. The completeness check finds those holes.
**It runs automatically on every verification** (both the live run and the offline
`--reference` run), writing `out/missing_values.csv` alongside the usual outputs — so
your normal command already produces it:

```bash
python verify_earnings.py --data-dir ./data --out-dir ./out          # includes it
python verify_earnings.py --data-dir ./data --out-dir ./out --check-completeness  # only this, offline
python verify_earnings.py --data-dir ./data --out-dir ./out --no-completeness     # skip it
```

It takes the **universe of every distinct `financial_code` in your data**, drops the
ones that shouldn't be flagged, and checks that each company has every remaining code
for every quarter it reports. Writes `out/missing_values.csv`, one row per missing
`(company, quarter, financial_code)`, each tagged with a **`scope`**:

| `scope` | Meaning | Signal |
|---------|---------|--------|
| `gap` | the company reports this code in **other** quarters but not this one | **high — a real hole** |
| `not_reported_by_company` | the code exists in the dataset but this company reports it in **no** quarter | low — maybe legitimately not disclosed |
| `quarter_missing` | an **entire** quarter inside the company's first→last span has no rows (only with `--fill-quarter-gaps`) | high |

Filter to `scope == gap` for the genuine per-quarter holes; `not_reported_by_company`
is informational (a company that never reports, say, `EPS_DILUTED` isn't a data error).
Add `--fill-quarter-gaps` to also flag quarters that are missing wholesale (not just
codes missing within quarters that exist).

**What's excluded automatically:** derived metrics (margins, turnover days, any
`*_QOQ`/`*_YOY`) and operational KPIs / non-GAAP figures — `WAFER_ASP`, `WAFER_SALES`
(and variants), `UTILIZATION`, `BILLING_12INCH`, `BACKLOG`, `ADJUSTED_*`, … — via the
same built-in classification the verifier uses. Add your own via
`config/completeness_exclude.csv` (one `financial_code` per row; `*`/`?` globs allowed,
e.g. `WAFER_*`, `*12INCH`).

### Pull a standalone reference, then fuzzy-match offline

Instead of hitting the APIs live per row, you can pull a **reference** of every
configured company once, then reconcile your files against it repeatedly (offline,
fast) with fuzzy label matching:

```bash
# 1. Pull the reference (slow — Japan EDINET + Taiwan PDFs; caches as it goes):
python verify_earnings.py --dump --dump-seg --dump-years 2019-2025 --out-dir ./out
#    -> ./out/reference/FA.csv, Seg_Geo_Revenue.csv, Seg_Seg_Revenue.csv (your schema)

# 2. Fuzzy-match your files against it (no API calls):
python verify_earnings.py --data-dir ./data --reference ./out/reference --out-dir ./out
```

`--reference` matches your `financial_code`s via `metric_map`, and your segment/geo
`segment_code` labels by **fuzzy matching** — canonical region (e.g. `China`→`CN`,
`North America`→`US`), then exact / substring / string-similarity — so labels that
differ from the source's native names (US XBRL members, Chinese segment names) still
line up. Each matched row's `note` records *how* it matched. Values use the same
tolerances (1% money, ±0.02 EPS). Rows with no reference counterpart →
`MISSING_IN_REFERENCE`. (`--dump` alone does statements only; add `--dump-seg` for
segment/geo. China segment/geo is excluded — semi-annual only.)

`--dump` writes **every distinct filing vintage** of a restated period (see below),
one row per vintage, and `--reference` matches your file against **any** of them —
so the offline path handles as-filed-vs-restated exactly like the live path, with
the same `api_vintages` / `vintage_match` columns. **Re-run `--dump` after upgrading**
if your existing `reference/` was pulled by an older version that stored a single
(for US, restated) value per period — otherwise the offline path can't see the
as-originally-filed figure.

Outputs:
- `out/verification_results.csv` — every row with a status.
- `out/mismatches.csv` — only the rows that disagree.
- `out/missing_values.csv` — rows ABSENT from your data (the completeness check;
  see [Find missing values](#find-missing-values-in-your-own-data-completeness-check)).
  Written on every run unless `--no-completeness`.
- console — a status summary and the list of mismatches.
- `out/export/` (with `--export`) — the **API-fetched values in your own CSV schema**
  (one file per input file). `financial_report_value` is filled from the filings
  (millions of local currency; per-share direct); `financial_value` (your FX→USD
  column) is left blank — we can't reproduce your conversion — and cells are blank
  wherever the API has no value (derived codes, unconfigured companies, or figures a
  market doesn't disclose). Diff it against your originals to see every difference.

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
2b. **`NET_INCOME` = attributable to the parent's owners** (母公司業主 / 지배기업
   소유주 / owners-of-parent) in every market — the figure universally reported as
   "net income" and quoted by data providers. The **total including
   non-controlling interest** is a separate code, `NET_INCOME_INC_NCI`. For
   high-NCI groups the two differ a lot (e.g. Pegatron, Hon Hai ~10%+), so a file
   holding the standard parent figure reconciles against `NET_INCOME`, not the
   consolidated total.
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
| `UNSUPPORTED_DERIVED` | computed ratio / turnover-days / subtotal / QoQ-delta metric (e.g. `NET_MARGIN`, `CASH_CONVERSION_CYCLE`, `QUICK_ASSETS`, `TAX_RATE`, `FREE_CASH_FLOW`, `*_QOQ`) — not a single as-filed line item, so not reconcilable against one API field |
| `UNSUPPORTED_NONFINANCIAL` | operational KPI or company-defined non-GAAP figure (e.g. `FULL_TIME_EMPLOYEES`, `WAFER_SALES`, `UTILIZATION`, `BACKLOG`, `BOOK_TO_BILL_RATIO`, `FX_RATE`, `NON_GAAP_REVENUE`) — not drawn from the audited statements, so no filing API line item corresponds to it |
| `COMPANY_NOT_CONFIGURED` | `company_id` not in `company_registry.csv` |
| `UNSUPPORTED_METRIC` | that market's source doesn't expose that metric |
| `NO_SEGMENT_MAPPING` | business-segment label not resolvable — add to `segment_members.csv` (US: XBRL member; JP: member substring; KR: 부문 name; TW: 部門 name). Geographic labels auto-match by region name |
| `SOURCE_UNAVAILABLE` | key missing for that market |
| `BAD_FILE_VALUE` / `ERROR` | unparseable value / fetch error |

### Restated vs as-filed periods (EDGAR multi-frame)

A company can report the **same quarter under two different figures**: the value
in its original filing, and a later restated value (a spin-off / discontinued-
operations reclass, a prior-period error correction, …). EDGAR keeps both. When
this happens the tool now pulls **every distinct vintage** for that period and
counts a `MATCH` if your file agrees with **any** of them — so a file holding the
as-originally-filed number isn't flagged as a `MISMATCH` just because EDGAR's
latest frame is the restated one (or vice versa). The `note` column lists all the
vintages and says which one your value matched (or "matches none" on a genuine
`MISMATCH`); `api_value_local` / `api_value_millions` show the vintage that
matched, `api_vintages` lists them all, and `vintage_match` is `latest` /
`superseded` / `none`. This holds on **both** paths — the live API run and the
offline `--dump`/`--reference` run (the dump writes one row per vintage). It also
covers Korea (DART) and Japan (EDINET) restatements recovered from the following
year's comparative columns, not just US EDGAR.

Example — **Dell CY2021Q3 COGS** (VMware spun off Nov 1 2021): `$20,335M` as
originally filed (incl. VMware) and `$20,890M` as later restated to continuing
operations. A file with either value now reconciles as `MATCH`. Revenue likewise
carries `$28,394M` (as-filed) and `$26,424M` (restated). Only directly-filed
figures (discrete quarters, point-in-time balances) carry vintages this way;
derived Q4/YTD-ladder values use the single latest frame.

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
    at instant contexts, point-in-time), plus receivables, cash, inventory, PP&E,
    parent equity, NCI, SG&A, tax and net-income-incl-NCI. Each metric is read
    under **both Japanese GAAP (`jppfs_cor`) and IFRS (`jpigp_cor`, …IFRS)** element
    names, so IFRS filers (Toyota, AGC, Panasonic, …) resolve too rather than
    silently missing. **EPS is intentionally excluded**: year-to-date EPS is
    restated across stock splits (e.g. Socionext FY2024), so differencing it is
    invalid. Japan EPS comes from J-Quants (recent quarters).
  - Any period neither source can reach (older than the J-Quants window *and*
    with no EDINET filing) returns `MISSING_IN_API`. Because the J-Quants free
    window rolls forward, cache older quarters sooner rather than later.
- **Segment & geographic files (`Seg_*`) — all four markets supported for
  revenue.** These files hold business-segment and geographic splits, which live
  in filing footnotes rather than clean top-line API fields.
  - **US (EDGAR).** Dimensional XBRL facts from the filing instances (`Revenues` /
    `OperatingIncomeLoss` on `StatementBusinessSegmentsAxis` /
    `StatementGeographicalAxis`, with the `OperatingSegmentsMember` qualifier
    handled), at discrete quarterly granularity from 10-Qs. Geographic *country*
    labels resolve via a built-in map (China→CN, US→US, Taiwan→TW, …); business
    segments and custom regions are mapped per company in
    `config/segment_members.csv`. Verified on Qorvo (segment revenue, segment
    operating income, geographic revenue). Geographic *operating income* is
    usually not disclosed by US filers → `MISSING_IN_API` (expected).
  - **Japan (EDINET).** Reportable-segment and geographic figures are dimensional
    XBRL in the securities reports — the member is encoded in the context id
    (e.g. `CurrentQuarterDuration_…GameAndNetworkServicesReportableSegmentMember`).
    The tool reads the year-to-date value per member and **de-cumulates to
    discrete quarters** (the revenue/operating-income element is picked
    heuristically by name; external "to customers" revenue is preferred). Map
    each label to a **substring of its XBRL member** in `segment_members.csv`
    (e.g. `301,Game & Network Services,GameAndNetworkServices`). Verified on Sony
    (Game/Music segment revenue and operating income, discrete quarters summing
    to the annual). *Caveats:* single-segment filers and post-April-2024 periods
    (no more 四半期 reports) yield little/no structured segment data.
  - **Korea (OpenDART notes).** KR geographic revenue lives in the
    financial-statement notes, not the primary statements — so the tool downloads
    the full periodic report (`document.xml`), finds the 영업부문 note (anchored on
    the note phrase, not a fixed position), and parses its HTML tables (`<TH>`/
    `<TD>`/`<TE>` cells). **Geographic revenue** (지역별 매출) is reconciled for **all
    four quarters**: Q1–Q3 read the note's discrete 3-month (3개월) column, and Q4 =
    annual − 9-month cumulative (the annual note uses a transposed, regions-as-
    columns layout, handled separately). The reported unit (백만원/천원) is applied
    and region names are mapped both country-level and continent-level
    (중국/China, 한국/Korea, 미국/US, 북미/NorthAmerica, 유럽/Europe, 중남미/LatAm, …).
    Verified on DB HiTek: China/Korea/US/Japan quarterly geographic revenue,
    including the Q4 derivation. **Business-segment revenue** (영업부문/보고부문) is
    also reconciled for all four quarters: the reportable-segment note table
    (consolidated) is read at its discrete 당분기(3개월) table (Q1–Q3) and derived
    as annual − 9-month for Q4; segment labels are mapped to the note's 부문 name
    in `segment_members.csv` (e.g. `Semiconductors → DS`). Verified on Samsung
    Electronics: DX / DS / SDC / Harman quarterly segment revenue, with the
    discrete quarters summing exactly to the disclosed 9-month figure and FY2023
    DS = ₩66.59 tn matching the filing. *Scope:* segment/geographic **operating
    income** is skipped (not consistently disclosed) → `MISSING_IN_API`.
  - **Taiwan (MOPS financial-report book, PDF).** TW segment/geographic revenue
    is disclosed only in the notes to the financial statements — **not** in
    FinMind, the TWSE OpenAPI, or the MOPS t164 XBRL-derived HTML view (all of
    which stop at the primary statements). The tool downloads the consolidated
    IFRS financial-report book (`…_AI1.pdf`) from the TWSE document server
    (`doc.twse.com.tw`, no key) and parses its text layer:
    - **Geographic revenue** from the 營業收入 disaggregation note's **地區別**
      (revenue-by-region) table — regions-as-rows, with a discrete 3-month column
      and a 9-month cumulative column. Q1–Q3 read the discrete column directly;
      Q4 = full-year (annual book) − 9-month. Region names are canonicalised in
      both Traditional Chinese and English (台灣/Taiwan, 美國/US/North America,
      中國/China, 日本/Japan, 歐洲、中東及非洲/EMEA, 其他/Other). Verified on **TSMC**:
      2023 quarterly geographic revenue (US 2023Q3 = NT$360,671 M ≈ 66%) with the
      discrete quarters summing exactly to the disclosed annual figures.
    - **Business-segment revenue** from the 部門資訊 note's 來自外部客戶收入
      (external-customer revenue) row — segments-as-columns; discrete quarters
      (Q4 = full-year − Q1–Q3). Map each label to the note's Chinese 部門 name in
      `segment_members.csv` (e.g. `158,ICT,資通訊產品事業群`). Verified on **Acer**
      (資通訊產品事業群 / 其他事業群, quarters summing to the annual). Single-segment
      filers (TSMC is one — foundry only) correctly return nothing.
    - Every extracted table is validated against its own printed total (region /
      segment values must sum to the total, else the parse is rejected) so a
      misread yields `MISSING_IN_API`, never a false `MATCH`. Values are NT$
      thousands (仟元). *Scope:* revenue only — segment/geographic **operating
      income** is skipped (rarely disclosed by region/segment) → `MISSING_IN_API`.
      *Caveats:* the book is a multi-MB PDF (slow on first fetch, then cached);
      companies whose note is typeset vertically (character-per-line) don't parse
      → `MISSING_IN_API`; companies that don't disclose a region/segment split
      (many, e.g. Acer/Marketech have no 地區別 table) → `MISSING_IN_API`.
      Needs `pdfplumber` (in `requirements.txt`).
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
