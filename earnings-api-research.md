# Free APIs for Public-Company Quarterly Earnings — US, China, Japan, Korea, Taiwan

**Goal:** Pull quarterly earnings (EPS / revenue / net income) for publicly listed
companies across 5 markets and cross-check them against an existing Excel sheet.

**Date:** 2026-07-09

---

## TL;DR — Recommendation

There is **no single free API** that cleanly covers earnings for all five markets.
The US is easy and everything Asian is where free tiers fall apart. Use a **two-layer
approach**:

1. **One broad aggregator** for convenience and the US/global happy path —
   **Financial Modeling Prep (FMP)** or **Twelve Data** (both have real free tiers with
   an earnings endpoint and at least some Asian symbols).
2. **Official regulator APIs** for authoritative per-market numbers — these are the
   "source of truth" you actually want when *verifying* against a spreadsheet:

| Market | Best free authoritative source | Key needed? |
|--------|-------------------------------|-------------|
| 🇺🇸 US | **SEC EDGAR** `companyfacts` / `companyconcept` (XBRL) | No key (fair-use) |
| 🇯🇵 Japan | **EDINET** API (FSA) — XBRL filings | Free key (email) |
| 🇰🇷 Korea | **OpenDART** (FSS) — XBRL financial statements | Free key |
| 🇹🇼 Taiwan | **TWSE OpenAPI** + **TPEx OpenAPI** (income statement, BS) | No key |
| 🇨🇳 China | **AKShare** / **Tushare** (open-source, scrape Eastmoney/Sina) | AKShare: none; Tushare: free token |

For verification work, prefer the official sources — they match what the company
actually filed, which is what your Excel sheet presumably came from.

---

## Layer 1 — Broad aggregator APIs (free tiers)

These give you one API, one auth scheme, and normalized JSON. Good for a first pass and
for the US. Asian **earnings** coverage on free tiers is the weak spot — verify the exact
tickers you care about before committing.

| API | Free tier | Earnings endpoint | US | Asia (JP/KR/TW/CN) coverage | Notes |
|-----|-----------|-------------------|----|------------------------------|-------|
| **Financial Modeling Prep** | 250 calls/day | `income-statement`, `earnings`, `earnings-calendar` (quarterly + annual) | ✅ Deep (30+ yrs, as-reported GAAP) | Partial — lists many intl exchanges, but non-US fundamentals are thinner/more gated | Best for financial-statement depth |
| **Twelve Data** | 800 calls/day, 8 credits/min | `earnings`, `income_statement` | ✅ | ✅ Broadest free intl: Tokyo (XTKS), Taiwan (XTAI), Korea (XKRX), Shanghai/Shenzhen listed | Credit-based; good global exchange list |
| **Alpha Vantage** | 25 calls/day | `EARNINGS` (quarterly EPS, est. vs actual), `EARNINGS_CALENDAR` | ✅ Strong | Thin — 20+ exchanges but non-US equity is weaker | Very low daily cap |
| **Finnhub** | 60 calls/min | `stock/earnings`, `stock/financials-reported` | ✅ | ⚠️ International **fundamentals/earnings are gated to paid** — free is effectively US | Generous rate limit, but not for intl earnings |
| **EODHD** | 20 calls/day | Fundamentals (incl. earnings) | ✅ | 60+ exchanges but intl fundamentals need a **paid** plan | Free = US, past year only |
| **Polygon.io** | 5 calls/min | Financials (US) | ✅ | ❌ US-only | Great US data, no Asia |
| **yfinance / Yahoo** | Unofficial, "unlimited" | `.income_stmt`, `.quarterly_income_stmt`, `.earnings_dates` | ✅ | ✅ via suffix tickers: `.T` (Tokyo), `.KS`/`.KQ` (Korea), `.TW`/`.TWO` (Taiwan), `.SS`/`.SZ` (China), `.HK` | ⚠️ Only ~last 4 quarters of earnings; scraping ToS gray area; breaks periodically — **not for production verification** |

**Verdict for Layer 1:** Start with **Twelve Data** if you want the widest free Asian
symbol list in a single API; use **FMP** if you want the deepest financial statements and
mostly care about US + the larger Asian names. Treat **yfinance** as a quick exploratory
tool, not a source of record.

---

## Layer 2 — Official regulator APIs (the "source of truth")

These are free, authoritative, and ideal for **verifying** numbers because they are the
actual filed statements (XBRL). Trade-off: each market has its own API, ID scheme, and
(for JP/KR/CN) some data is in the local language.

### 🇺🇸 United States — SEC EDGAR
- **Endpoints:** `https://data.sec.gov/api/xbrl/companyfacts/CIK{##########}.json` and
  `companyconcept/...` for a single tagged value (e.g. `EarningsPerShareDiluted`).
- **Auth:** None. Requires a descriptive `User-Agent` header; fair-use ~10 req/s.
- **Coverage:** Every SEC filer, full history, as-reported quarterly/annual XBRL.
- **Best for:** Ground-truth US EPS/revenue/net income.

### 🇯🇵 Japan — EDINET (Financial Services Agency)
- **Endpoint:** `https://api.edinet-fsa.go.jp/api/v2/...` — document list + document
  download (XBRL/ZIP of quarterly & annual securities reports).
- **Auth:** Free subscription key (register with email).
- **Coverage:** ~4,600 listed companies; official filings, XBRL financial statements.
- **Note:** You parse XBRL yourself (or use a helper like `edinet-python`). Statements are
  filed in Japanese taxonomy but numbers are language-neutral.

### 🇰🇷 Korea — OpenDART (Financial Supervisory Service)
- **Endpoint:** `https://opendart.fss.or.kr/api/...` — e.g. `fnlttSinglAcntAll.json`
  returns full financial statements (IFRS/K-GAAP) by corp code + year + report code
  (Q1/half/Q3/annual).
- **Auth:** Free API key (instant signup at opendart.fss.or.kr).
- **Coverage:** KOSPI / KOSDAQ / KONEX filers. English DART portal also exists.
- **Best for:** Authoritative Korean EPS/net income by quarter.

### 🇹🇼 Taiwan — TWSE OpenAPI + TPEx OpenAPI
- **Endpoints:** `https://openapi.twse.com.tw/` (listed) and
  `https://www.tpex.org.tw/openapi/` (OTC). Datasets include income statement, balance
  sheet, and monthly revenue under "Corporate Financials." Underlying source is **MOPS**.
- **Auth:** None. Free, no key.
- **Coverage:** All TWSE-listed + TPEx/OTC companies; quarterly statements.
- **Best for:** Taiwan EPS/revenue with zero signup friction.

### 🇨🇳 China — AKShare / Tushare (no clean free *official* English API)
- China's regulators (CSRC / exchanges) don't offer a friendly free English earnings API;
  Wind/CSMAR are paid. The practical free route is open-source Python libraries that
  aggregate free public sources (Eastmoney, Sina, THS):
  - **AKShare** — open-source, **no API key** for most endpoints; A-shares (SH/SZ), HK,
    and some global; returns pandas DataFrames. Broadest free coverage.
  - **Tushare** — free token (points-based); more structured/point-in-time financials,
    favored by quant users.
- **Caveat:** These scrape third-party portals, so reliability and Terms-of-Service are
  weaker than an official API. Good for bulk pulls; spot-check against filings.

---

## Recommended architecture for "verify against Excel"

```
Excel sheet (company, quarter, reported EPS/revenue/net income)
        │
        ▼
Normalizer  ──►  key = (market, local_ticker/identifier, fiscal_quarter)
        │
        ├─ US      → SEC EDGAR companyconcept (EarningsPerShareDiluted, Revenues, NetIncomeLoss)
        ├─ Japan   → EDINET XBRL  (or FMP/Twelve Data for the big caps)
        ├─ Korea   → OpenDART fnlttSinglAcntAll
        ├─ Taiwan  → TWSE/TPEx OpenAPI income statement
        └─ China   → AKShare (stock_financial_abstract / financial reports)
        │
        ▼
Compare(reported_value, api_value, tolerance) ──► match / mismatch report
```

Practical tips:
- **Identifiers are the hard part.** Each market keys differently: US=CIK, Japan=EDINET
  code / securities code, Korea=corp_code, Taiwan=stock code, China=6-digit SH/SZ code.
  Build a mapping table from your Excel tickers → each API's ID up front.
- **Fiscal calendars differ** (esp. Japan's March year-end and cumulative vs. discrete
  quarters). Decide whether your Excel holds discrete-quarter or year-to-date figures and
  match the API accordingly.
- **Currency & units** — statements are in local currency and sometimes thousands/millions;
  normalize before comparing.
- **Tolerance** — allow small rounding/restatement differences rather than exact equality.
- **Rate limits** — cache aggressively; the official APIs are generous but you'll hit
  free-tier caps (Alpha Vantage 25/day, EODHD 20/day) fast on a big list.

---

## Quick-start pick

- **Fastest to prototype (US + big Asian names):** Financial Modeling Prep free tier.
- **Widest free Asian symbol coverage in one API:** Twelve Data free tier.
- **Highest-accuracy verification (recommended):** official APIs — SEC EDGAR + EDINET +
  OpenDART + TWSE/TPEx + AKShare.

---

## Sources

- [Financial Modeling Prep — docs](https://site.financialmodelingprep.com/developer/docs) · [available countries](https://site.financialmodelingprep.com/developer/docs/stable/available-countries)
- [Finnhub — home](https://finnhub.io/) · [pricing](https://finnhub.io/pricing) · [earnings calendar docs](https://finnhub.io/docs/api/earnings-calendar)
- [Alpha Vantage — home](https://www.alphavantage.co/) · [documentation](https://www.alphavantage.co/documentation/)
- [Twelve Data — Taiwan Stock Exchange (XTAI)](https://twelvedata.com/exchanges/XTAI)
- [EODHD — fundamentals](https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds) · [pricing](https://eodhd.com/pricing)
- [Polygon.io](https://polygon.io/)
- [yfinance — Ticker reference](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html) · [EPS estimate discussion](https://github.com/ranaroussi/yfinance/discussions/2159)
- [SEC EDGAR — English DART equivalent] US: [data.sec.gov developer resources](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [Korea — English DART (FSS)](https://englishdart.fss.or.kr/) · OpenDART key at opendart.fss.or.kr
- [Japan — EDINET DB / API overview](https://edinetdb.com/) · official: api.edinet-fsa.go.jp
- [Taiwan — TWSE OpenAPI](https://openapi.twse.com.tw/) · [TPEx OpenAPI](https://www.tpex.org.tw/openapi/)
- [AKShare (GitHub)](https://github.com/akfamily/akshare) · [Tushare (GitHub)](https://github.com/waditu/tushare)
- [Extending OpenBB for A-Share/HK with AKShare & Tushare](https://openbb.co/blog/extending-openbb-for-a-share-and-hong-kong-stock-analysis-with-akshare-and-tushare/)
- [Best Financial Data APIs in 2026 (nb-data)](https://www.nb-data.com/p/best-financial-data-apis-in-2026)
