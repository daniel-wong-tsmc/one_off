#!/usr/bin/env python3
"""
verify_earnings.py
==================
Pull publicly-filed quarterly financials for companies across US / Korea /
Taiwan / Japan and reconcile them against local CSV files, reporting which
(company, metric, period) rows do NOT match.

Sources (all free):
  US      -> SEC EDGAR       (no key)
  Korea   -> OpenDART        (env DART_KEY)
  Taiwan  -> FinMind         (env FINMIND_TOKEN optional; works without for light use)
  Japan   -> EDINET          (env EDINET_KEY)   [annual-only in v1, see README]

Usage:
  export DART_KEY=...  EDINET_KEY=...   # (FINMIND_TOKEN optional)
  python verify_earnings.py --data-dir ./data --out-dir ./out
  python verify_earnings.py --self-test        # live check against 4 known companies

See README.md for the assumptions this makes about your files (units, which
value column is compared, quarter semantics).
"""
from __future__ import annotations
import argparse, csv, io, json, os, re, sys, time, zipfile, datetime, urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'.  pip install -r requirements.txt")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)
SEC_UA = os.environ.get("SEC_USER_AGENT", "earnings-verify contact@example.com")
REL_TOL = 0.01          # 1% relative tolerance for money metrics
EPS_ABS_TOL = 0.02      # absolute tolerance for per-share values
NEAR_ZERO = 1.0         # values (in millions) below this compared absolutely

# Canonical metrics we know how to fetch.  kind: 'flow' (income statement,
# summed over the quarter) or 'stock' (balance sheet, point-in-time).
CANONICAL = {
    "REVENUE":             {"kind": "flow",  "per_share": False},
    "COGS":                {"kind": "flow",  "per_share": False},
    "GROSS_PROFIT":        {"kind": "flow",  "per_share": False},
    "OPERATING_INCOME":    {"kind": "flow",  "per_share": False},
    "PRE_TAX_INCOME":      {"kind": "flow",  "per_share": False},
    "NET_INCOME":          {"kind": "flow",  "per_share": False},
    "EPS_BASIC":           {"kind": "flow",  "per_share": True},
    "EPS_DILUTED":         {"kind": "flow",  "per_share": True},
    "ACCOUNTS_PAYABLE":    {"kind": "stock", "per_share": False},
    "CURRENT_ASSETS":      {"kind": "stock", "per_share": False},
    "TOTAL_ASSETS":        {"kind": "stock", "per_share": False},
    "CURRENT_LIABILITIES": {"kind": "stock", "per_share": False},
    "TOTAL_LIABILITIES":   {"kind": "stock", "per_share": False},
    "TOTAL_EQUITY":        {"kind": "stock", "per_share": False},
}

# Derived / ratio metrics (margins, turnover days, cash-conversion cycle,
# QoQ/YoY deltas). These are NOT a single as-filed line item — they are computed
# from primitives with company-specific conventions (which denominator, discrete
# vs trailing-twelve-month, period-average vs point-in-time), so pulling one API
# field can't reproduce them and dividing by 1e6 would be nonsense. We flag them
# UNSUPPORTED_DERIVED rather than guess. Map a code to `DERIVED` in metric_map.csv,
# or use a *_QOQ / *_YOY suffix, to land here.
DERIVED_SENTINEL = "DERIVED"
DERIVED_METRICS = {
    "NET_MARGIN", "GROSS_MARGIN", "OPERATING_MARGIN", "EBITDA_MARGIN",
    "CASH_CONVERSION_CYCLE", "DAYS_OF_INVENTORY", "DAYS_INVENTORY_OUTSTANDING",
    "DAYS_SALES_OUTSTANDING", "DAYS_PAYABLE_OUTSTANDING",
    "INVENTORY_TURNOVER", "ASSET_TURNOVER", "CURRENT_RATIO", "QUICK_RATIO",
    "ROE", "ROA", "DEBT_TO_EQUITY",
}


def is_derived_code(code: str, canonical: str) -> bool:
    """True if this financial_code is a derived ratio/turnover/delta metric that
    can't be reconciled against a single as-filed API line item."""
    if canonical == DERIVED_SENTINEL:
        return True
    c = code.upper()
    return c in DERIVED_METRICS or c.endswith("_QOQ") or c.endswith("_YOY")

SESSION = requests.Session()


def _cache_get(key: str):
    f = CACHE / (key.replace("/", "_") + ".json")
    if f.exists():
        return json.loads(f.read_text())
    return None


def _cache_put(key: str, val):
    (CACHE / (key.replace("/", "_") + ".json")).write_text(json.dumps(val))


def _http_json(url: str, headers=None, cache_key=None, retries=3):
    if cache_key:
        c = _cache_get(cache_key)
        if c is not None:
            return c
    for i in range(retries):
        try:
            r = SESSION.get(url, headers=headers or {}, timeout=60)
            if r.status_code == 404:
                if cache_key:
                    _cache_put(cache_key, None)
                return None
            r.raise_for_status()
            data = r.json()
            if cache_key:
                _cache_put(cache_key, data)
            return data
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def cal_key_from_date(d: str):
    """Map a fiscal period-end date to the calendar quarter the period belongs
    to, by snapping to the NEAREST calendar quarter-end.

    52/53-week filers (e.g. Qorvo) routinely end a quarter a few days into the
    following month — 2023-04-01 is the Jan–Mar (Q1) quarter, 2020-10-03 is the
    Jul–Sep (Q3) quarter — so keying off the raw period-end month bucketed them
    into the *next* calendar quarter. The user's `calendar_quarter` reflects the
    quarter the period actually falls in, which is what nearest-quarter-end
    snapping reproduces. Ordinary calendar/month-end filers are unaffected.
      '2024-03-31' -> (2024, 1);  '2023-04-01' -> (2023, 1);  '2020-10-03' -> (2020, 3)
    """
    dt = datetime.date.fromisoformat(d[:10])
    cands = [datetime.date(yy, mm, dd)
             for yy in (dt.year - 1, dt.year, dt.year + 1)
             for mm, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    best = min(cands, key=lambda c: abs((c - dt).days))
    return (best.year, quarter_of(best.month))


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class Source:
    market = "?"
    available = True
    note = ""

    def supports(self, metric: str) -> bool:
        return metric in self.metric_map

    def quarterly(self, api_id: str, metric: str, fye_month: int = 12,
                  years=None) -> dict:
        """Return {(cal_year, cal_q): value_in_local_currency}. Discrete for
        flow metrics, period-end balance for stock metrics. `years` is the set
        of calendar years actually needed (lets per-year sources fetch less)."""
        raise NotImplementedError


# ---- US: SEC EDGAR -------------------------------------------------------- #
class EdgarSource(Source):
    market = "us"
    metric_map = {
        "REVENUE": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
                    "SalesRevenueNet"],
        "COGS": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
                 "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization"],
        "GROSS_PROFIT": ["GrossProfit"],
        "OPERATING_INCOME": ["OperatingIncomeLoss"],
        "PRE_TAX_INCOME": [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
        "NET_INCOME": ["NetIncomeLoss"],
        "EPS_BASIC": ["EarningsPerShareBasic"],
        "EPS_DILUTED": ["EarningsPerShareDiluted"],
        "ACCOUNTS_PAYABLE": ["AccountsPayableCurrent", "AccountsPayableTradeCurrent"],
        "CURRENT_ASSETS": ["AssetsCurrent"],
        "TOTAL_ASSETS": ["Assets"],
        "CURRENT_LIABILITIES": ["LiabilitiesCurrent"],
        "TOTAL_LIABILITIES": ["Liabilities"],
        "TOTAL_EQUITY": ["StockholdersEquity"],
    }

    def __init__(self):
        self._cik = {}

    def _resolve_cik(self, api_id: str) -> str | None:
        api_id = api_id.strip().upper()
        if api_id.isdigit():
            return api_id.zfill(10)
        if not self._cik:
            d = _http_json("https://www.sec.gov/files/company_tickers.json",
                           headers={"User-Agent": SEC_UA}, cache_key="edgar_tickers")
            for v in (d or {}).values():
                self._cik[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
        return self._cik.get(api_id)

    def _concept(self, cik: str, tag: str):
        return _http_json(
            f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json",
            headers={"User-Agent": SEC_UA}, cache_key=f"edgar_{cik}_{tag}")

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        cik = self._resolve_cik(api_id)
        if not cik:
            return {}
        per_share = CANONICAL[metric]["per_share"]
        kind = CANONICAL[metric]["kind"]
        unit = "USD/shares" if per_share else "USD"
        # use the first fallback tag that actually has data (don't mix tags,
        # e.g. Excluding- vs Including-AssessedTax revenue)
        facts = []
        for tag in self.metric_map[metric]:
            d = self._concept(cik, tag)
            if d and d.get("units", {}).get(unit):
                facts = d["units"][unit]
                break
        if not facts:
            return {}
        if kind == "stock":
            out = {}
            for x in facts:
                if x.get("end") and not x.get("start"):
                    out[cal_key_from_date(x["end"])] = float(x["val"])
                elif x.get("end") and x.get("start"):
                    s = datetime.date.fromisoformat(x["start"])
                    e = datetime.date.fromisoformat(x["end"])
                    if (e - s).days <= 5:   # instant reported as tiny duration
                        out[cal_key_from_date(x["end"])] = float(x["val"])
            return out
        # flow: collect discrete quarters (~90d) and annuals (~365d)
        quarters, annuals = {}, {}
        for x in facts:
            if not (x.get("start") and x.get("end") and x.get("form")):
                continue
            s = datetime.date.fromisoformat(x["start"])
            e = datetime.date.fromisoformat(x["end"])
            days = (e - s).days
            if 80 <= days <= 100:
                quarters[cal_key_from_date(x["end"])] = (float(x["val"]), e)
            elif 350 <= days <= 380:
                annuals[e] = float(x["val"])
        out = {k: v[0] for k, v in quarters.items()}
        # derive Q4 = FY - (the three quarters ending within the prior ~12 months)
        for e_annual, ann_val in annuals.items():
            sub = [v for (k, (v, e)) in quarters.items()
                   if 0 < (e_annual - e).days <= 285]
            if len(sub) == 3:
                out[cal_key_from_date(e_annual.isoformat())] = ann_val - sum(sub)
        # YTD-ladder fallback: many filers report an income item only as
        # year-to-date cumulatives in their 10-Qs (Q1 ~90d, then ~180/270/365d,
        # all sharing one fiscal-year start) instead of discrete quarters. Group
        # duration facts by start date and de-cumulate each contiguous ladder,
        # filling ONLY quarters the discrete path above didn't already produce
        # (setdefault). A rung is emitted only when the immediately preceding
        # quarter of the ladder is present, so every value written is exactly a
        # one-quarter difference (never annual-minus-Q1, etc.).
        from collections import defaultdict
        by_start = defaultdict(list)
        for x in facts:
            if not (x.get("start") and x.get("end")):
                continue
            s = datetime.date.fromisoformat(x["start"])
            e = datetime.date.fromisoformat(x["end"])
            dd = (e - s).days
            if dd >= 80:
                by_start[s].append((e, dd, float(x["val"])))
        for _s, lst in by_start.items():
            ladder = {}
            for e, dd, v in lst:
                qi = round(dd / 91.3)
                if 1 <= qi <= 4:
                    prev = ladder.get(qi)
                    if prev is None or abs(dd - qi * 91.3) < abs(prev[1] - qi * 91.3):
                        ladder[qi] = (e, dd, v)
            if len(ladder) < 2:
                continue
            prev_v, prev_q = 0.0, 0
            for qi in sorted(ladder):
                e, dd, v = ladder[qi]
                if qi - 1 == prev_q:
                    out.setdefault(cal_key_from_date(e.isoformat()), v - prev_v)
                prev_v, prev_q = v, qi
        return out


# ---- US segment/geo: EDGAR dimensional XBRL (pilot) ----------------------- #
# Built-in geographic label -> XBRL member local-name. Country members are the
# ISO-2 code (country:CN -> "CN"); regions vary by filer, add via config.
GEO_MEMBER = {
    "CHINA": "CN", "US": "US", "USA": "US", "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US", "TAIWAN": "TW", "JAPAN": "JP",
    "KOREA": "KR", "SOUTH KOREA": "KR", "GERMANY": "DE", "EUROPE": "EuropeMember",
}


def _xloc(q):
    return q.split("}")[-1].split(":")[-1]


class EdgarDimensional:
    """Extract dimensional (segment / geographic) facts from EDGAR filing XBRL
    instances. Pilot: US only."""
    REV_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax")
    OPINC_TAGS = ("OperatingIncomeLoss",)

    def __init__(self, edgar: "EdgarSource"):
        self.edgar = edgar

    def _filings(self, cik, years):
        d = _http_json(f"https://data.sec.gov/submissions/CIK{cik}.json",
                       headers={"User-Agent": SEC_UA}, cache_key=f"edgar_sub_{cik}")
        r = (d or {}).get("filings", {}).get("recent", {})
        out = set()
        yrs = set(int(y) for y in years) if years else None
        for form, acc, rd, doc in zip(r.get("form", []), r.get("accessionNumber", []),
                                      r.get("reportDate", []), r.get("primaryDocument", [])):
            if form not in ("10-K", "10-Q") or not doc.endswith(".htm"):
                continue
            try:
                y = int(rd[:4])
            except ValueError:
                continue
            if yrs and y not in yrs and (y - 1) not in yrs:
                continue
            out.add((acc, doc))
        return out

    # extra axes allowed alongside a segment/geo breakdown (value must match).
    # OperatingSegmentsMember is the standard qualifier on segment tables.
    QUALIFIERS = {"ConsolidationItemsAxis": "OperatingSegmentsMember"}

    def _facts(self, cik, acc, doc):
        ck = f"edgar_dim2_{acc}"
        c = _cache_get(ck)
        if c is not None:
            return c
        inst = doc[:-4] + "_htm.xml"
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{inst}"
        facts = []
        try:
            r = SESSION.get(url, headers={"User-Agent": SEC_UA}, timeout=90)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ctx = {}
            for cc in root:
                if _xloc(cc.tag) != "context":
                    continue
                dims = {}
                s = e = None
                for x in cc.iter():
                    l = _xloc(x.tag)
                    if l == "explicitMember":
                        dims[_xloc(x.get("dimension"))] = _xloc((x.text or "").strip())
                    elif l == "startDate":
                        s = x.text
                    elif l == "endDate":
                        e = x.text
                ctx[cc.get("id")] = (s, e, dims)
            for el in root.iter():
                cr = el.get("contextRef")
                if not cr or cr not in ctx:
                    continue
                s, e, dims = ctx[cr]
                if not s or not e or not dims:
                    continue
                if not any("Segment" in k or "Geograph" in k for k in dims):
                    continue
                try:
                    val = float(el.text)
                except (TypeError, ValueError):
                    continue
                facts.append([_xloc(el.tag), s, e, dims, val])
        except Exception:
            pass
        _cache_put(ck, facts)
        return facts

    def series(self, cik, tags, axis_kw, member, years):
        """Discrete-quarter (~90d) dimensional values keyed by (cal_year, cal_q).
        Matches the target axis+member, allowing only the standard segment
        qualifier axis alongside it (rejects product/geo cross-tabs, etc.)."""
        out = {}
        for acc, doc in self._filings(cik, years):
            for tag, s, e, dims, val in self._facts(cik, acc, doc):
                if tag not in tags:
                    continue
                target = [k for k in dims if axis_kw in k]
                if not target or dims[target[0]] != member:
                    continue
                extra_ok = all(k == target[0] or self.QUALIFIERS.get(k) == v
                               for k, v in dims.items())
                if not extra_ok:
                    continue
                days = (datetime.date.fromisoformat(e) - datetime.date.fromisoformat(s)).days
                if 85 <= days <= 95:
                    out[cal_key_from_date(e)] = val
        return out


def load_segment_members(path: Path) -> dict:
    """(company_id, label_upper) -> XBRL member local-name (business segments and
    any custom/region geographic members)."""
    mm = {}
    if not path.exists():
        return mm
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            cid = (row.get("company_id") or "").strip()
            label = (row.get("label") or "").strip()
            member = (row.get("member") or "").strip()
            if cid and label and member and not cid.startswith("#"):
                mm[(cid, label.upper())] = member
    return mm


# --------------------------------------------------------------------------- #
# Footnote-table parsing (Korea DART documents, Taiwan MOPS) — segment/geo lives
# in the notes, which are HTML-ish tables rather than clean API fields.
# --------------------------------------------------------------------------- #
def _html_cell(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&cr;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("　", " ")):
        s = s.replace(a, b)
    return s.strip()


def _html_tables(html: str):
    """Parse every <TABLE> in an HTML/DART-markup fragment into a list of rows
    (each row a list of stripped cell strings)."""
    tables = []
    for tbl in re.findall(r"<TABLE\b[^>]*>(.*?)</TABLE>", html, re.S | re.I):
        rows = []
        for tr in re.findall(r"<TR\b[^>]*>(.*?)</TR>", tbl, re.S | re.I):
            # DART markup uses <TH>/<TD> and also <TE> for body cells
            cells = [_html_cell(c) for c in
                     re.findall(r"<T[HDE]\b[^>]*>(.*?)</T[HDE]>", tr, re.S | re.I)]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _kr_num(s: str):
    s = s.replace(",", "").replace("–", "-").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _unit_multiplier(text: str) -> float:
    """Read the '(단위: 백만원)' hint. Returns won-per-reported-unit."""
    if "백만원" in text:
        return 1e6
    if "천원" in text:
        return 1e3
    return 1.0


# Canonicalize a region label (Korean or English) so a user's geographic label
# matches the row label in a Korean 지역별 table.
_REGION_CANON = {
    "한국": "KR", "국내": "KR", "대한민국": "KR", "KOREA": "KR", "SOUTHKOREA": "KR",
    "중국": "CN", "CHINA": "CN", "중화인민공화국": "CN", "대중국": "CN", "중화권": "CN",
    "미국": "US", "미주": "US", "USA": "US", "UNITEDSTATES": "US", "US": "US",
    "대만": "TW", "TAIWAN": "TW",
    "일본": "JP", "JAPAN": "JP",
    "홍콩": "HK", "HONGKONG": "HK",
    "싱가포르": "SG", "SINGAPORE": "SG",
    "유럽": "EU", "EUROPE": "EU", "구주": "EU", "유럽연합": "EU",
    "독일": "DE", "GERMANY": "DE",
    "아시아": "ASIA", "ASIA": "ASIA", "아태": "ASIA", "아시아태평양": "ASIA",
    "북미": "NA", "북미주": "NA", "NORTHAMERICA": "NA",
    "중남미": "LATAM", "남미": "LATAM", "LATINAMERICA": "LATAM",
    "중동": "ME", "MIDDLEEAST": "ME", "중동아프리카": "MEA",
    "아프리카": "AF", "AFRICA": "AF",
    "인도": "IN", "INDIA": "IN", "베트남": "VN", "VIETNAM": "VN",
    "오세아니아": "OCEANIA", "OCEANIA": "OCEANIA",
    "기타": "OTHER", "기타국가": "OTHER", "기타지역": "OTHER",
    "OTHER": "OTHER", "OTHERS": "OTHER",
    "합계": "TOTAL", "합 계": "TOTAL", "총계": "TOTAL", "TOTAL": "TOTAL", "소계": "TOTAL",
}
# Regions specific enough to identify a geographic table (excludes the structural
# OTHER / TOTAL rows, which appear in many non-geographic tables too).
_SPECIFIC_REGIONS = set(_REGION_CANON.values()) - {"OTHER", "TOTAL"}


def _canon_region(s: str) -> str:
    key = re.sub(r"[\s()\.\-_/]", "", s).upper()
    return _REGION_CANON.get(key, key)


# ---- Korea: OpenDART ------------------------------------------------------ #
class OpenDartSource(Source):
    market = "kr"
    metric_map = {
        "REVENUE": (["매출액", "수익(매출액)", "영업수익", "매출"], ("IS", "CIS")),
        "COGS": (["매출원가"], ("IS", "CIS")),
        "GROSS_PROFIT": (["매출총이익", "매출총이익(손실)"], ("IS", "CIS")),
        "OPERATING_INCOME": (["영업이익", "영업이익(손실)"], ("IS", "CIS")),
        "PRE_TAX_INCOME": (["법인세비용차감전순이익", "법인세비용차감전계속사업이익",
                            "법인세차감전순이익", "법인세비용차감전순이익(손실)",
                            "법인세비용차감전계속영업이익"], ("IS", "CIS")),
        "NET_INCOME": (["당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"], ("IS", "CIS")),
        "EPS_BASIC": (["기본주당이익", "기본주당이익(손실)", "기본주당순이익"], ("IS", "CIS")),
        "EPS_DILUTED": (["희석주당이익", "희석주당이익(손실)", "희석주당순이익"], ("IS", "CIS")),
        "ACCOUNTS_PAYABLE": (["매입채무", "매입채무및기타채무"], ("BS",)),
        "CURRENT_ASSETS": (["유동자산"], ("BS",)),
        "TOTAL_ASSETS": (["자산총계"], ("BS",)),
        "CURRENT_LIABILITIES": (["유동부채"], ("BS",)),
        "TOTAL_LIABILITIES": (["부채총계"], ("BS",)),
        "TOTAL_EQUITY": (["자본총계"], ("BS",)),
    }
    REPRT = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}

    def __init__(self):
        self.key = os.environ.get("DART_KEY")
        self.available = bool(self.key)
        self.note = "" if self.key else "DART_KEY not set"
        self._corp = None

    def _corp_map(self):
        if self._corp is None:
            self._corp = {}
            zf = CACHE / "dart_corp.zip"
            if not zf.exists():
                r = SESSION.get(
                    f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={self.key}",
                    timeout=300)
                zf.write_bytes(r.content)
            import xml.etree.ElementTree as ET
            data = zipfile.ZipFile(zf).read("CORPCODE.xml").decode("utf-8")
            for el in ET.fromstring(data).iter("list"):
                sc = (el.findtext("stock_code") or "").strip()
                if sc:
                    self._corp[sc] = el.findtext("corp_code")
        return self._corp

    def _fs(self, corp, year, reprt, fs_div):
        q = urllib.parse.urlencode({"crtfc_key": self.key, "corp_code": corp,
                                    "bsns_year": str(year), "reprt_code": reprt,
                                    "fs_div": fs_div})
        return _http_json("https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?" + q,
                          cache_key=f"dart_{corp}_{year}_{reprt}_{fs_div}")

    def _val(self, data, names, sj_divs):
        if not data or data.get("status") != "000":
            return None
        for row in data.get("list", []):
            if (row.get("account_nm") or "").strip() in names and row.get("sj_div") in sj_divs:
                raw = (row.get("thstrm_amount") or "").replace(",", "").strip()
                try:
                    return float(raw)
                except ValueError:
                    return None
        return None

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        if not self.available:
            return {}
        corp = self._corp_map().get(api_id.strip())
        if not corp:
            return {}
        names, sj = self.metric_map[metric]
        kind = CANONICAL[metric]["kind"]
        if years:
            year_range = sorted(set(int(y) for y in years))
        else:
            year_range = range(2015, datetime.date.today().year + 1)
        out = {}
        for year in year_range:
            vals = {}
            for q, reprt in self.REPRT.items():
                # consolidated preferred; fall back to separate if no value
                v = self._val(self._fs(corp, year, reprt, "CFS"), names, sj)
                if v is None:
                    v = self._val(self._fs(corp, year, reprt, "OFS"), names, sj)
                if v is not None:
                    vals[q] = v
            if not vals:
                continue
            if kind == "stock":
                for q, v in vals.items():
                    out[(year, q)] = v
            else:
                for q, v in self._to_discrete(vals).items():
                    out[(year, q)] = v
        return out

    @staticmethod
    def _to_discrete(vals: dict) -> dict:
        """vals keyed by report quarter 1..4. Interim thstrm may be discrete
        (3-month) or cumulative depending on the filer; detect via the ratio of
        the Q3 report to the annual, then produce discrete quarters."""
        out = {}
        fy = vals.get(4)
        q3r = vals.get(3)
        cumulative = False
        if fy and q3r and fy != 0:
            cumulative = (q3r / fy) > 0.5   # ~0.75 => cumulative 9M; ~0.25 => discrete
        if cumulative:
            prev = 0
            for q in (1, 2, 3):
                if q in vals:
                    out[q] = vals[q] - prev
                    prev = vals[q]
            if fy is not None and 3 in vals:
                out[4] = fy - vals[3]
        else:
            for q in (1, 2, 3):
                if q in vals:
                    out[q] = vals[q]
            if fy is not None:
                out[4] = fy - sum(vals.get(q, 0) for q in (1, 2, 3))
        return out

    # ---- segment / geographic (from the notes, via document.xml) ---------- #
    # Korean segment (영업부문) and geographic (지역별) breakdowns live in the
    # financial-statement notes, not the primary statements — so we download the
    # full periodic report (document.xml), find the 영업부문 note, and parse its
    # HTML tables. Values are read as discrete quarters (the tables carry a
    # 3개월 / 누적 split; Q4 = annual − 9-month cumulative).
    REPORT_NM = {1: "분기보고서", 2: "반기보고서", 3: "분기보고서", 4: "사업보고서"}
    REPORT_MM = {1: "03", 2: "06", 3: "09", 4: "12"}

    def _dart_list(self, corp, year):
        """Periodic filings for a corp covering `year` (annual is filed early the
        next year, so the window runs into the following April)."""
        q = urllib.parse.urlencode({"crtfc_key": self.key, "corp_code": corp,
                                    "bgn_de": f"{year}0101", "end_de": f"{year + 1}0430",
                                    "pblntf_ty": "A", "page_count": "100"})
        return _http_json("https://opendart.fss.or.kr/api/list.json?" + q,
                          cache_key=f"dart_list_{corp}_{year}")

    def _rcept_for(self, corp, year, quarter):
        d = self._dart_list(corp, year)
        if not d or d.get("status") != "000":
            return None
        want_nm, want_mm = self.REPORT_NM[quarter], self.REPORT_MM[quarter]
        tag = f"({year}.{want_mm})"
        for r in d.get("list", []):
            nm = (r.get("report_nm") or "")
            if want_nm in nm and tag in nm.replace(" ", ""):
                return r.get("rcept_no")
        return None

    def _dart_document(self, rcept):
        ck = f"dart_doc_{rcept}"
        c = _cache_get(ck)
        if c is not None:
            return c
        r = SESSION.get(f"https://opendart.fss.or.kr/api/document.xml"
                        f"?crtfc_key={self.key}&rcept_no={rcept}", timeout=120)
        try:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            raw = zf.read(zf.namelist()[0])
            for enc in ("utf-8", "cp949", "euc-kr", "utf-16"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    text = ""
        except Exception:
            text = ""
        _cache_put(ck, text)
        return text

    @staticmethod
    def _note_window(text, is_geo):
        """Slice of the report holding the relevant 영업부문 note table. The note
        appears both in the business-overview section and (authoritatively) in the
        financial-statement notes; the latter comes later, so we anchor on the LAST
        occurrence of the note phrase. Document layout varies a lot by filer, so a
        fixed position threshold does not work — phrase anchoring does."""
        anchors = (["지역별 부문정보", "지역별 매출", "지역에 대한", "지역별"] if is_geo
                   else ["영업부문에 대한", "부문별 정보", "부문에 대한 정보",
                         "부문별", "영업부문"])
        for a in anchors:                 # most specific first
            i = text.rfind(a)             # notes come after the business overview
            if i >= 0:
                # geo tables follow their heading; segment tables often precede the
                # explanatory footnote we anchor on, so widen the window backwards.
                lo = i - 200 if is_geo else max(0, i - 14000)
                return text[lo:i + 25000]
        return ""

    @staticmethod
    def _pick_column(rows):
        """Index (into a data row's numeric cells) of the current-period 3-month
        column, and of the current-period cumulative column. Korean tables list
        the current period before the prior period, 3개월 before 누적."""
        disc_i = cum_i = None
        for r in rows:
            cells = [c.replace(" ", "") for c in r]
            if any("3개월" in c for c in cells) or any(c == "누적" for c in cells):
                markers = [c for c in cells if ("3개월" in c or "누적" in c)]
                for k, m in enumerate(markers):
                    if "3개월" in m and disc_i is None:
                        disc_i = k
                    if "누적" in m and cum_i is None:
                        cum_i = k
                break
        return disc_i, cum_i

    def _region_value(self, text, region_canon, col):
        """col in {'discrete','cumulative','current'} -> value in won, or None."""
        note = self._note_window(text, is_geo=True)
        if not note:
            return None
        mult = _unit_multiplier(note)
        known = set(_REGION_CANON.values())

        def region_at(row):
            """Index of the first cell in a row that names a known region."""
            for j, c in enumerate(row):
                if c and _canon_region(c) in known:
                    return j
            return None

        for tbl in _html_tables(note):
            # region-as-rows layout (standard in quarterly reports): a table with
            # ≥2 rows each labelled by a *specific* region (not just Other/Total,
            # which appear in many non-geographic tables).
            rows_with_region = [r for r in tbl if region_at(r) is not None]
            specific = {_canon_region(r[region_at(r)]) for r in rows_with_region
                        } & _SPECIFIC_REGIONS
            if len(specific) < 2:
                continue
            disc_i, cum_i = self._pick_column(tbl)
            for r in rows_with_region:
                lj = region_at(r)
                if _canon_region(r[lj]) != region_canon:
                    continue
                nums = [n for n in (_kr_num(c) for c in r[lj + 1:]) if n is not None]
                if not nums:
                    return None
                if col == "discrete":
                    idx = disc_i if disc_i is not None else 0
                elif col == "cumulative":
                    idx = cum_i if cum_i is not None else (len(nums) - 1)
                else:
                    idx = 0
                if idx < len(nums):
                    return nums[idx] * mult
        # region-as-columns layout (annual reports): regions are header columns and
        # the values sit in a data row. Only full-year ('current') values appear
        # here — the current period (당기) table comes first, so the first match wins.
        if col == "current":
            for tbl in _html_tables(note):
                hdr = next((r for r in tbl
                            if sum(_canon_region(c) in _SPECIFIC_REGIONS
                                   for c in r) >= 2), None)
                if not hdr:
                    continue
                col_of = {}
                for j, c in enumerate(hdr):
                    cc = _canon_region(c)
                    if cc in known and cc not in col_of:
                        col_of[cc] = j
                if region_canon not in col_of:
                    continue
                j = col_of[region_canon]
                for r in tbl:
                    if r is hdr or j >= len(r):
                        continue
                    v = _kr_num(r[j])
                    if v is not None:
                        return v * mult
        return None

    def segment_quarterly(self, api_id, fye_month, years, label, want, is_geo):
        """Discrete-quarter {(cal_y, cal_q): value_won} for a Korean GEOGRAPHIC
        (지역별) revenue label. Q1–Q3 read the note's 3-month column directly; Q4 =
        full-year − 9-month cumulative (the annual note is transposed).

        Scope: geographic **revenue** only. Korean filings do not break operating
        income out by region, and business-*segment* (영업부문) note tables are too
        filer-variable to parse reliably (label styles, overview-vs-note, transposed
        layouts differ per company), so those return empty rather than risk a wrong
        reconciliation."""
        if not self.available or not is_geo or want == "opincome":
            return {}
        corp = self._corp_map().get(api_id.strip())
        if not corp:
            return {}
        region_canon = _canon_region(label)
        yrs = sorted(set(int(y) for y in years)) if years else \
            range(2018, datetime.date.today().year + 1)
        out = {}
        for year in yrs:
            for q in (1, 2, 3, 4):
                try:
                    if q in (1, 2, 3):
                        rc = self._rcept_for(corp, year, q)
                        if not rc:
                            continue
                        v = self._region_value(self._dart_document(rc),
                                               region_canon, "discrete")
                    else:  # Q4 = full-year − 9-month cumulative
                        rc4 = self._rcept_for(corp, year, 4)
                        rc3 = self._rcept_for(corp, year, 3)
                        if not rc4 or not rc3:
                            continue
                        fy = self._region_value(self._dart_document(rc4),
                                                region_canon, "current")
                        c3 = self._region_value(self._dart_document(rc3),
                                                region_canon, "cumulative")
                        v = (fy - c3) if (fy is not None and c3 is not None) else None
                    if v is not None:
                        out[(year, q)] = v
                except Exception:
                    continue
        return out


# ---- Taiwan: FinMind ------------------------------------------------------ #
class FinMindSource(Source):
    market = "tw"
    IS = "TaiwanStockFinancialStatements"
    BS = "TaiwanStockBalanceSheet"
    metric_map = {
        "REVENUE": (IS, "Revenue"),
        "COGS": (IS, "CostOfGoodsSold"),
        "GROSS_PROFIT": (IS, "GrossProfit"),
        "OPERATING_INCOME": (IS, "OperatingIncome"),
        "PRE_TAX_INCOME": (IS, "PreTaxIncome"),
        "NET_INCOME": (IS, "IncomeAfterTaxes"),
        "EPS_BASIC": (IS, "EPS"),
        "ACCOUNTS_PAYABLE": (BS, "AccountsPayable"),
        "CURRENT_ASSETS": (BS, "CurrentAssets"),
        "TOTAL_ASSETS": (BS, "TotalAssets"),
        "CURRENT_LIABILITIES": (BS, "CurrentLiabilities"),
        "TOTAL_LIABILITIES": (BS, "Liabilities"),
        "TOTAL_EQUITY": (BS, "Equity"),
    }

    def __init__(self):
        self.token = os.environ.get("FINMIND_TOKEN", "")

    def _data(self, dataset, sid):
        q = {"dataset": dataset, "data_id": sid,
             "start_date": "2015-01-01",
             "end_date": datetime.date.today().isoformat()}
        if self.token:
            q["token"] = self.token
        url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(q)
        return _http_json(url, cache_key=f"finmind_{dataset}_{sid}")

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        dataset, typ = self.metric_map[metric]
        d = self._data(dataset, api_id.strip())
        if not d or d.get("status") != 200:
            return {}
        out = {}
        for row in d.get("data", []):
            if row.get("type") == typ:
                out[cal_key_from_date(row["date"])] = float(row["value"])
        return out


# ---- Japan: EDINET (annual + quarterly securities reports, XBRL) ---------- #
def _month_last_day(y, m):
    nxt = datetime.date(y + (m == 12), (m % 12) + 1, 1)
    return nxt - datetime.timedelta(days=1)


class EdinetSource(Source):
    market = "jp"
    # canonical -> XBRL element id. Read at CurrentYTDDuration (quarterly report)
    # or CurrentYearDuration (annual securities report); YTD values de-cumulated.
    # Only absolute-yen flow metrics: their YTD values de-cumulate exactly and
    # are immune to share-count changes. EPS is deliberately excluded because
    # YTD EPS is restated across stock splits, so differencing it is invalid
    # (Japan EPS comes from J-Quants for recent quarters instead).
    metric_map = {
        # flow (income statement): read at *Duration contexts, de-cumulated
        "REVENUE": "jppfs_cor:NetSales",
        "COGS": "jppfs_cor:CostOfSales",
        "GROSS_PROFIT": "jppfs_cor:GrossProfit",
        "OPERATING_INCOME": "jppfs_cor:OperatingIncome",
        "PRE_TAX_INCOME": "jppfs_cor:IncomeBeforeIncomeTaxes",
        "NET_INCOME": "jppfs_cor:ProfitLossAttributableToOwnersOfParent",
        # stock (balance sheet): read at *Instant contexts, point-in-time
        "ACCOUNTS_PAYABLE": "jppfs_cor:AccountsPayableTrade",
        "CURRENT_ASSETS": "jppfs_cor:CurrentAssets",
        "TOTAL_ASSETS": "jppfs_cor:Assets",
        "CURRENT_LIABILITIES": "jppfs_cor:CurrentLiabilities",
        "TOTAL_LIABILITIES": "jppfs_cor:Liabilities",
        "TOTAL_EQUITY": "jppfs_cor:NetAssets",
    }
    note = ""

    def __init__(self):
        self.key = os.environ.get("EDINET_KEY")
        self.available = bool(self.key)
        if not self.key:
            self.note = "EDINET_KEY not set"
        self._doc_index = {}   # (sec5, fye, years) -> {period_end_iso: (docID, is_annual)}

    @staticmethod
    def _quarter_end_months(fye_month):
        return {((fye_month - 3 * i - 1) % 12) + 1 for i in range(4)}

    def _list(self, date):
        return _http_json(
            f"https://api.edinet-fsa.go.jp/api/v2/documents.json?date={date}"
            f"&type=2&Subscription-Key={self.key}", cache_key=f"edinet_list_{date}")

    def _find_doc(self, sec5, period_end, is_annual):
        """Scan the statutory filing window after a period end for the annual
        (120) or quarterly (140) securities report of this company."""
        lo, hi = (76, 96) if is_annual else (30, 50)   # ~3 months / ~45 days
        want = "120" if is_annual else "140"
        today = datetime.date.today()
        for d in range(lo, hi + 1):
            date = period_end + datetime.timedelta(days=d)
            if date > today:
                break
            for r in (self._list(date.isoformat()) or {}).get("results", []):
                if r.get("secCode") == sec5 and r.get("docTypeCode") == want:
                    return r.get("docID")
        return None

    def _discover(self, sec5, fye_month, years):
        key = (sec5, fye_month, tuple(sorted(years)))
        if key in self._doc_index:
            return self._doc_index[key]
        qmonths = self._quarter_end_months(fye_month)
        docs = {}
        for y in sorted(years):
            for m in qmonths:
                pe = _month_last_day(y, m)
                is_annual = (m == fye_month)
                doc = self._find_doc(sec5, pe, is_annual)
                if doc:
                    docs[pe.isoformat()] = (doc, is_annual)
        self._doc_index[key] = docs
        return docs

    def _csv_text(self, doc):
        ck = f"edinet_csvtext_{doc}"
        c = _cache_get(ck)
        if c is not None:
            return c
        r = SESSION.get(f"https://api.edinet-fsa.go.jp/api/v2/documents/{doc}"
                        f"?type=5&Subscription-Key={self.key}", timeout=120)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = [n for n in zf.namelist() if "jpcrp" in n][0]  # main report, not audit
        text = zf.read(name).decode("utf-16")
        _cache_put(ck, text)
        return text

    def _report_value(self, doc, metric, is_annual):
        text = self._csv_text(doc)
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        hdr = rows[0]
        eid, ctxi, vali = hdr.index("要素ID"), hdr.index("コンテキストID"), hdr.index("値")
        elt = self.metric_map[metric]
        if CANONICAL[metric]["kind"] == "stock":   # balance sheet: point-in-time
            ctx = "CurrentYearInstant" if is_annual else "CurrentQuarterInstant"
        else:                                       # income statement: period flow
            ctx = "CurrentYearDuration" if is_annual else "CurrentYTDDuration"
        for r in rows[1:]:
            if r[eid] == elt and r[ctxi] == ctx and "NonConsolidated" not in r[ctxi]:
                try:
                    return float(r[vali])
                except ValueError:
                    return None
        return None

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        if not self.available:
            return {}
        sec5 = api_id.strip()
        if len(sec5) == 4:
            sec5 += "0"
        yrs = (sorted(set(int(y) for y in years)) if years
               else range(2018, datetime.date.today().year + 1))
        docs = self._discover(sec5, fye_month, yrs)
        # stock (balance sheet): each report gives the period-end balance directly
        if CANONICAL[metric]["kind"] == "stock":
            out = {}
            for pe_iso, (doc, is_annual) in docs.items():
                v = self._report_value(doc, metric, is_annual)
                if v is not None:
                    out[cal_key_from_date(pe_iso)] = v
            return out
        # flow (income statement): period-end -> YTD, de-cumulated below
        ytd = {}
        for pe_iso, (doc, is_annual) in docs.items():
            v = self._report_value(doc, metric, is_annual)
            if v is not None:
                ytd[pe_iso] = v
        if not ytd:
            return {}
        # group by fiscal year, index quarters, de-cumulate YTD -> discrete
        from collections import defaultdict
        groups = defaultdict(dict)
        for pe_iso, v in ytd.items():
            pe = datetime.date.fromisoformat(pe_iso)
            fye = _month_last_day(pe.year + (pe.month > fye_month), fye_month)
            mdist = (fye.year - pe.year) * 12 + (fye.month - pe.month)
            qidx = 4 - mdist // 3            # 1..4 within the fiscal year
            groups[fye][qidx] = (pe, v)
        out = {}
        for q_map in groups.values():
            for q in (1, 2, 3, 4):
                if q not in q_map:
                    continue
                pe, v = q_map[q]
                if q == 1:
                    disc = v
                elif (q - 1) in q_map:
                    disc = v - q_map[q - 1][1]
                else:
                    continue                 # missing prior quarter -> can't derive
                out[cal_key_from_date(pe.isoformat())] = disc
        return out

    # ---- segment / geographic (dimensional XBRL in EDINET reports) --------- #
    # Japanese securities reports tag reportable-segment (and geographic) figures
    # as dimensional facts: the member is baked into the context id, e.g.
    #   CurrentQuarterDuration_jpcrp040300-q2r_E01777-000MusicReportableSegmentMember
    # We read the year-to-date value per member (CurrentYTDDuration for a
    # quarterly report, CurrentYearDuration for an annual) and de-cumulate to
    # discrete quarters — exactly like the main flow metrics. The element id
    # varies by filer, so we pick it heuristically by local-name.
    @staticmethod
    def _seg_score(localname: str, want: str) -> int:
        """Rank how well an element local-name fits the wanted segment metric.
        0 = not a match. Higher = better (external revenue beats total; a plain
        operating-income tag beats nothing)."""
        ln = localname
        low = ln.lower()
        if want == "revenue":
            if "intersegment" in low:
                return 0
            if not any(k in low for k in ("revenue", "sales", "netsales")):
                return 0
            if "tocustomers" in low or "external" in low:
                return 3          # revenue to external customers (what we want)
            if low.startswith("netsales") or low.startswith("revenue") or low.startswith("sales"):
                return 2
            return 1              # e.g. total incl. intersegment — last resort
        else:  # operating income
            if "intersegment" in low:
                return 0
            if "operatingincome" in low or "operatingprofit" in low:
                return 3
            if "segmentincome" in low or "segmentprofit" in low:
                return 2
            return 0

    def _segment_ytd(self, doc, member_substr, want, is_annual):
        """Best YTD (or full-year) value for a segment/geo member in one report."""
        text = self._csv_text(doc)
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        hdr = rows[0]
        eid, ctxi, vali = hdr.index("要素ID"), hdr.index("コンテキストID"), hdr.index("値")
        period = "CurrentYearDuration" if is_annual else "CurrentYTDDuration"
        pref = period + "_"
        sub = member_substr.lower()
        best_v, best_s = None, 0
        for r in rows[1:]:
            if len(r) <= vali:
                continue
            ctx = r[ctxi]
            if not ctx.startswith(pref):
                continue
            mem = ctx[len(pref):]
            if "member" not in mem.lower() or sub not in mem.lower():
                continue
            score = self._seg_score(r[eid].split(":")[-1], want)
            if score <= best_s:
                continue
            try:
                best_v, best_s = float(r[vali]), score
            except ValueError:
                continue
        return best_v

    def segment_quarterly(self, api_id, fye_month, years, member_substr, want):
        """Discrete-quarter {(cal_y, cal_q): value} for a reportable-segment or
        geographic member. `want` is 'revenue' or 'opincome'."""
        if not self.available:
            return {}
        sec5 = api_id.strip()
        if len(sec5) == 4:
            sec5 += "0"
        yrs = (sorted(set(int(y) for y in years)) if years
               else range(2018, datetime.date.today().year + 1))
        docs = self._discover(sec5, fye_month, yrs)
        ytd = {}
        for pe_iso, (doc, is_annual) in docs.items():
            try:
                v = self._segment_ytd(doc, member_substr, want, is_annual)
            except Exception:
                v = None
            if v is not None:
                ytd[pe_iso] = v
        if not ytd:
            return {}
        from collections import defaultdict
        groups = defaultdict(dict)
        for pe_iso, v in ytd.items():
            pe = datetime.date.fromisoformat(pe_iso)
            fye = _month_last_day(pe.year + (pe.month > fye_month), fye_month)
            mdist = (fye.year - pe.year) * 12 + (fye.month - pe.month)
            qidx = 4 - mdist // 3
            groups[fye][qidx] = (pe, v)
        out = {}
        for q_map in groups.values():
            for q in (1, 2, 3, 4):
                if q not in q_map:
                    continue
                pe, v = q_map[q]
                if q == 1:
                    disc = v
                elif (q - 1) in q_map:
                    disc = v - q_map[q - 1][1]
                else:
                    continue
                out[cal_key_from_date(pe.isoformat())] = disc
        return out


# ---- Japan: J-Quants V2 (recent quarters, from TDnet 決算短信) ------------- #
class JQuantsSource(Source):
    market = "jp"
    BASE = "https://api.jquants.com/v2"
    metric_map = {   # canonical -> /fins/summary field
        "REVENUE": "Sales", "OPERATING_INCOME": "OP", "NET_INCOME": "NP",
        "EPS_BASIC": "EPS", "TOTAL_ASSETS": "TA", "TOTAL_EQUITY": "Eq",
    }

    def __init__(self):
        self.key = os.environ.get("JQUANTS_KEY")
        self.available = bool(self.key)
        self.note = "" if self.key else "JQUANTS_KEY not set"

    def _summary(self, code):
        ck = f"jquants_summary_{code}"
        c = _cache_get(ck)
        if c is not None:
            return c
        rows, params = [], {"code": code}
        while True:
            url = self.BASE + "/fins/summary?" + urllib.parse.urlencode(params)
            d = _http_json(url, headers={"x-api-key": self.key})
            if not d or "data" not in d:
                break
            rows.extend(d["data"])
            if d.get("pagination_key"):
                params["pagination_key"] = d["pagination_key"]
            else:
                break
        _cache_put(ck, rows)
        return rows

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        if not self.available:
            return {}
        field = self.metric_map[metric]
        kind = CANONICAL[metric]["kind"]
        # actual financial statements only (skip forecast-only revisions);
        # dedupe by (fiscal-year-end, period-type) keeping the latest disclosure
        best = {}
        for r in self._summary(api_id.strip()):
            if "FinancialStatements" not in (r.get("DocType") or ""):
                continue
            k = (r.get("CurFYEn"), r.get("CurPerType"))
            if k not in best or (r.get("DiscDate", "") > best[k].get("DiscDate", "")):
                best[k] = r

        def num(r):
            try:
                return float(r.get(field))
            except (TypeError, ValueError):
                return None

        out = {}
        if kind == "stock":
            for r in best.values():
                v, end = num(r), r.get("CurPerEn")
                if v is not None and end:
                    out[cal_key_from_date(end)] = v
            return out
        # flow: values are cumulative YTD -> de-cumulate within each fiscal year
        from collections import defaultdict
        groups = defaultdict(list)
        for r in best.values():
            if r.get("CurPerEn") and num(r) is not None:
                groups[r.get("CurFYEn")].append(r)
        for grp in groups.values():
            grp.sort(key=lambda r: r["CurPerEn"])
            if len(grp) == 1 and grp[0].get("CurPerType") == "FY":
                continue  # lone annual row -> can't derive a discrete quarter
            prev = 0.0
            for r in grp:
                cum = num(r)
                out[cal_key_from_date(r["CurPerEn"])] = cum - prev
                prev = cum
        return out


# ---- Japan composite: J-Quants (recent) + EDINET (annual / future quarterly) #
class JapanSource(Source):
    market = "jp"

    def __init__(self):
        self.jq = JQuantsSource()
        self.ed = EdinetSource()
        self.available = self.jq.available or self.ed.available
        gaps = []
        if not self.jq.available:
            gaps.append("JQUANTS_KEY not set")
        if not self.ed.available:
            gaps.append("EDINET_KEY not set")
        self.note = ("; ".join(gaps) if gaps else
                     "J-Quants covers recent ~2yr quarters (TDnet 決算短信); "
                     "EDINET covers annual + pre-2024 quarterly securities reports.")
        self.metric_map = {**self.ed.metric_map, **self.jq.metric_map}

    def supports(self, metric):
        return self.jq.supports(metric) or self.ed.supports(metric)

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        out = {}
        for src in (self.ed, self.jq):   # J-Quants wins on overlap (fresher)
            if src.available and src.supports(metric):
                try:
                    out.update(src.quarterly(api_id, metric, fye_month, years))
                except Exception:
                    pass
        return out


# --------------------------------------------------------------------------- #
# Registry & metric map (user-editable CSVs)
# --------------------------------------------------------------------------- #
def load_registry(path: Path) -> dict:
    reg = {}
    if not path.exists():
        return reg
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("company_id") or row["company_id"].startswith("#"):
                continue
            reg[row["company_id"].strip()] = {
                "market": row["market"].strip().lower(),
                "api_id": row["api_id"].strip(),
                "fye_month": int(row.get("fye_month") or 12),
                "name": (row.get("name") or "").strip(),
            }
    return reg


def load_metric_map(path: Path) -> dict:
    mm = {}
    if not path.exists():
        return mm
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("financial_code") or "").strip()
            if not code or code.startswith("#"):
                continue
            mm[code] = (row.get("canonical_metric") or "").strip()
    return mm


def load_company_mapping(path: Path) -> dict:
    """The user's `company_id_mapping` file: `company_id;external_mapped_name`
    (delimiter `;`, like the other data files). Returns {company_id -> name}.
    Header row optional — if the first row isn't a real mapping it's skipped.
    This does NOT resolve a company to its market/API id (a human name can't be
    turned into a KRX/TWSE/sec code reliably); it only supplies display names
    and lets us flag which mapped ids are still missing from the registry."""
    mp = {}
    if not path.exists():
        return mp
    text = path.read_text(encoding="utf-8-sig")
    # the user's data files are ';'-delimited, but tolerate a comma file too:
    # pick whichever delimiter actually appears on a real (non-comment) line.
    delim = ";"
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            if ";" not in line and "," in line:
                delim = ","
            break
    for parts in csv.reader(io.StringIO(text), delimiter=delim):
        if not parts or not (parts[0] or "").strip():
            continue
        cid = parts[0].strip()
        # skip a header row and comment lines
        if cid.startswith("#") or cid.lower() == "company_id":
            continue
        name = parts[1].strip() if len(parts) > 1 else ""
        mp[cid] = name
    return mp


def find_mapping_file(data_dir: Path, name: str) -> Path:
    """Locate the mapping file, tolerating a `.csv` suffix or its absence
    (the user's file is literally named `company_id_mapping`)."""
    for cand in (data_dir / name, data_dir / (name + ".csv")):
        if cand.exists():
            return cand
    return data_dir / name


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #
SEG_FILES = {"Seg_Seg_Revenue", "Seg_Seg_Operating_Income",
             "Seg_Geo_Revenue", "Seg_Geo_Operating_Income"}


def compare(file_val, api_local, per_share):
    if per_share:
        return ("MATCH" if abs(file_val - api_local) <= EPS_ABS_TOL else "MISMATCH",
                api_local)
    api_m = api_local / 1e6           # source is full local currency -> millions
    if max(abs(file_val), abs(api_m)) < NEAR_ZERO:
        ok = abs(file_val - api_m) < NEAR_ZERO
    else:
        ok = abs(file_val - api_m) / max(abs(file_val), abs(api_m)) <= REL_TOL
    return ("MATCH" if ok else "MISMATCH", api_m)


def run(data_dir: Path, out_dir: Path, compare_col: str,
        registry: dict, metric_map: dict, files_map: dict, seg_members: dict = None,
        mapping: dict = None):
    sources = {s.market: s for s in
               (EdgarSource(), OpenDartSource(), FinMindSource(), JapanSource())}
    edgar_dim = EdgarDimensional(sources["us"])
    seg_members = seg_members or {}
    mapping = mapping or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    unconfigured = {}   # company_id -> mapped name, for the end-of-run to-do list

    # pre-scan: which calendar years does each company appear in? (lets per-year
    # sources like OpenDART fetch only what's needed instead of all history)
    years_by_company = {}
    for logical, fname in files_map.items():
        fpath = data_dir / fname
        if not fpath.exists():
            continue
        with open(fpath, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                cid = (row.get("company_id") or "").strip()
                cy = (row.get("calendar_year") or "").strip()
                if cid and cy.isdigit():
                    years_by_company.setdefault(cid, set()).add(int(cy))

    series_cache = {}   # (market, api_id, metric) -> {(y,q): value}

    def get_series(src, comp, cid, metric):
        k = (comp["market"], comp["api_id"], metric)
        if k not in series_cache:
            series_cache[k] = src.quarterly(
                comp["api_id"], metric, comp["fye_month"],
                years=years_by_company.get(cid))
        return series_cache[k]

    for logical, fname in files_map.items():
        fpath = data_dir / fname
        if not fpath.exists():
            print(f"  (skip {logical}: {fpath} not found)")
            continue
        is_seg = logical in SEG_FILES
        with open(fpath, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                cid = (row.get("company_id") or "").strip()
                cy = (row.get("calendar_year") or "").strip()
                cq = (row.get("calendar_quarter") or "").strip()
                code = (row.get("financial_code") or "").strip()
                rec = {"file": logical, "company_id": cid,
                       "calendar_year": cy, "calendar_quarter": cq,
                       "financial_code": code,
                       "segment_code": (row.get("segment_code") or "").strip(),
                       "file_value": row.get(compare_col, ""),
                       "api_value_local": "", "api_value_millions": "",
                       "status": "", "note": ""}
                comp = registry.get(cid)
                cname = (comp["name"] if comp else "") or mapping.get(cid, "")
                rec["company_name"] = cname
                if not comp and cid:
                    unconfigured[cid] = mapping.get(cid, "")

                if is_seg:
                    if not comp:
                        rec["status"] = "COMPANY_NOT_CONFIGURED"
                        rec["note"] = ("add to config/company_registry.csv"
                                       + (f" (mapped name: {mapping[cid]})"
                                          if mapping.get(cid) else ""))
                        results.append(rec); continue
                    market = comp["market"]
                    label = re.sub(r"^\d{4}Q\d_", "", rec["segment_code"]).strip()
                    is_geo = logical.startswith("Seg_Geo")
                    want = ("opincome" if logical.endswith("Operating_Income")
                            else "revenue")
                    fv_raw = (rec["file_value"] or row.get("financial_value", "")
                              or row.get("financial_report_value", ""))
                    try:
                        fv = float(str(fv_raw).replace(",", ""))
                    except ValueError:
                        rec["status"] = "BAD_FILE_VALUE"; results.append(rec); continue
                    rec["file_value"] = fv

                    # Taiwan: segment & geographic figures live only in the TIFRS
                    # footnotes (PDF/HTML on MOPS), which no free API exposes as
                    # structured data — so we can't reconcile them yet.
                    if market == "tw":
                        rec["status"] = "SEGMENT_SOURCE_UNAVAILABLE"
                        rec["note"] = ("TW segment/geo is footnote-only (MOPS TIFRS); "
                                       "not in the free API (see README)")
                        results.append(rec); continue

                    member = seg_members.get((cid, label.upper()))
                    if market == "us":
                        if not member and is_geo:
                            member = GEO_MEMBER.get(label.upper())   # ISO codes: US only
                        if not member:
                            rec["status"] = "NO_SEGMENT_MAPPING"
                            rec["note"] = f"map '{label}' in config/segment_members.csv"
                            results.append(rec); continue
                    elif market == "jp" and not member:
                        rec["status"] = "NO_SEGMENT_MAPPING"
                        rec["note"] = (f"map '{label}' in config/segment_members.csv "
                                       "(JP: a substring of the EDINET member local-name)")
                        results.append(rec); continue
                    elif market == "kr" and not is_geo:
                        # KR support is geographic revenue only (business-segment
                        # note tables are too filer-variable to parse reliably).
                        rec["status"] = "UNSUPPORTED_SEGMENT"
                        rec["note"] = ("KR business-segment not reconciled (geographic "
                                       "revenue only); many KR filers are single-segment")
                        results.append(rec); continue

                    try:
                        if market == "us":
                            tags = (EdgarDimensional.OPINC_TAGS if want == "opincome"
                                    else EdgarDimensional.REV_TAGS)
                            axis_kw = "Geograph" if is_geo else "Segment"
                            cik = sources["us"]._resolve_cik(comp["api_id"])
                            ser = edgar_dim.series(cik, tags, axis_kw, member,
                                                   years_by_company.get(cid))
                        elif market == "jp":
                            ser = sources["jp"].ed.segment_quarterly(
                                comp["api_id"], comp["fye_month"],
                                years_by_company.get(cid), member, want)
                        elif market == "kr":
                            # KR geographic revenue: matched by region name (a
                            # segment_members.csv override can rename the label).
                            ser = sources["kr"].segment_quarterly(
                                comp["api_id"], comp["fye_month"],
                                years_by_company.get(cid), member or label, want, is_geo)
                        else:
                            rec["status"] = "UNSUPPORTED_SEGMENT"
                            rec["note"] = f"no segment source for market {market}"
                            results.append(rec); continue
                        api_local = ser.get((int(cy), int(cq)))
                        if api_local is None:
                            rec["status"] = "MISSING_IN_API"
                            rec["note"] = ("no discrete-quarter dimensional fact "
                                           "(annual-only disclosure or period absent)")
                            results.append(rec); continue
                        status, api_m = compare(fv, api_local, False)
                        rec["api_value_local"] = api_local
                        rec["api_value_millions"] = round(api_m, 3)
                        rec["status"] = status
                    except Exception as e:
                        rec["status"] = "ERROR"; rec["note"] = str(e)[:120]
                    results.append(rec); continue
                if not comp:
                    rec["status"] = "COMPANY_NOT_CONFIGURED"
                    rec["note"] = ("add to config/company_registry.csv"
                                   + (f" (mapped name: {mapping[cid]})"
                                      if mapping.get(cid) else ""))
                    results.append(rec); continue
                canonical = metric_map.get(code)
                if is_derived_code(code, canonical):
                    rec["status"] = "UNSUPPORTED_DERIVED"
                    rec["note"] = ("computed ratio/turnover/delta metric — not a "
                                   "single as-filed line item, so not reconcilable")
                    results.append(rec); continue
                if not canonical:
                    rec["status"] = "NO_MAPPING"
                    rec["note"] = "add to config/metric_map.csv"
                    results.append(rec); continue
                src = sources.get(comp["market"])
                if not src or not src.available:
                    rec["status"] = "SOURCE_UNAVAILABLE"
                    rec["note"] = src.note if src else f"market {comp['market']}"
                    results.append(rec); continue
                if not src.supports(canonical):
                    rec["status"] = "UNSUPPORTED_METRIC"
                    rec["note"] = f"{comp['market']} source lacks {canonical}"
                    results.append(rec); continue
                try:
                    fv = float(str(rec["file_value"]).replace(",", ""))
                except ValueError:
                    rec["status"] = "BAD_FILE_VALUE"; results.append(rec); continue

                try:
                    key = (int(cy), int(cq))
                    series = get_series(src, comp, cid, canonical)
                    api_local = series.get(key)
                    if api_local is None and comp["market"] == "jp":
                        rec["note"] = sources["jp"].note
                    if api_local is None:
                        rec["status"] = "MISSING_IN_API"
                        results.append(rec); continue
                    status, api_m = compare(fv, api_local,
                                            CANONICAL[canonical]["per_share"])
                    rec["api_value_local"] = api_local
                    rec["api_value_millions"] = "" if CANONICAL[canonical]["per_share"] else round(api_m, 3)
                    rec["status"] = status
                except Exception as e:
                    rec["status"] = "ERROR"; rec["note"] = str(e)[:120]
                results.append(rec)

    # write outputs
    cols = ["file", "company_id", "company_name", "calendar_year", "calendar_quarter",
            "financial_code", "segment_code", "file_value", "api_value_local",
            "api_value_millions", "status", "note"]
    all_csv = out_dir / "verification_results.csv"
    with open(all_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in cols})
    mism = [r for r in results if r["status"] == "MISMATCH"]
    mm_csv = out_dir / "mismatches.csv"
    with open(mm_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in mism:
            w.writerow({k: r.get(k, "") for k in cols})

    # console summary
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print("\n=== Summary ===")
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {st:22} {n}")
    print(f"\nFull results : {all_csv}")
    print(f"Mismatches   : {mm_csv}  ({len(mism)} rows)")
    if mism:
        print("\n=== MISMATCHES (company / metric / period) ===")
        for r in mism:
            print(f"  [{r['company_id']} {r['company_name']}] {r['financial_code']} "
                  f"{r['calendar_year']}Q{r['calendar_quarter']}: "
                  f"file={r['file_value']} vs api(millions)={r['api_value_millions']}")
    if unconfigured:
        print(f"\n=== COMPANIES TO CONFIGURE ({len(unconfigured)}) ===")
        print("  (present in your data but missing from config/company_registry.csv)")
        for cid in sorted(unconfigured):
            nm = unconfigured[cid]
            print(f"  company_id={cid}" + (f"  ->  {nm}" if nm else "  (no mapped name)"))
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
DEFAULT_FILES = {
    "FA": "FA.csv",
    "Seg_Seg_Revenue": "Seg_Seg_Revenue.csv",
    "Seg_Seg_Operating_Income": "Seg_Seg_Operating_Income.csv",
    "Seg_Geo_Revenue": "Seg_Geo_Revenue.csv",
    "Seg_Geo_Operating_Income": "Seg_Geo_Operating_Income.csv",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data", help="dir with your CSV files")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--compare-column", default="financial_report_value",
                    choices=["financial_report_value", "financial_value"],
                    help="which file column to reconcile against the API")
    ap.add_argument("--mapping-file", default="company_id_mapping",
                    help="your company_id -> name file (in --data-dir); a "
                         "'.csv' suffix is tolerated. Supplies display names and "
                         "flags ids missing from company_registry.csv")
    ap.add_argument("--self-test", action="store_true",
                    help="run live against the 4 validated companies in sample_data/")
    args = ap.parse_args()

    cfg = Path(args.config_dir)
    if args.self_test:
        args.data_dir = "sample_data"
        cfg = Path("sample_data")
    registry = load_registry(cfg / "company_registry.csv")
    metric_map = load_metric_map(Path(args.config_dir) / "metric_map.csv")
    seg_members = load_segment_members(Path(args.config_dir) / "segment_members.csv")
    if args.self_test:
        seg_members = load_segment_members(cfg / "segment_members.csv") or seg_members
    mapping = load_company_mapping(find_mapping_file(Path(args.data_dir), args.mapping_file))
    if not registry:
        print("WARNING: empty company_registry.csv — every row will be COMPANY_NOT_CONFIGURED")
    run(Path(args.data_dir), Path(args.out_dir), args.compare_column,
        registry, metric_map, DEFAULT_FILES, seg_members, mapping)


if __name__ == "__main__":
    main()
