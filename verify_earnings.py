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
  python verify_earnings.py --self-test        # live check against 6 known companies

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
    # --- additional balance-sheet line items (stock, point-in-time) ---
    "ACCOUNTS_RECEIVABLE":          {"kind": "stock", "per_share": False},
    "CASH_AND_CASH_EQUIVALENTS":    {"kind": "stock", "per_share": False},
    "INVENTORIES":                  {"kind": "stock", "per_share": False},
    # inventory sub-components (only filers that disclose the breakdown on the
    # face balance sheet carry these — e.g. US semis; others report MISSING).
    "FINISHED_GOODS":               {"kind": "stock", "per_share": False},
    "RAW_MATERIALS":                {"kind": "stock", "per_share": False},
    "WORK_IN_PROCESS":              {"kind": "stock", "per_share": False},
    "PROPERTY_PLANT_AND_EQUIPMENT": {"kind": "stock", "per_share": False},
    # equity attributable to the parent's owners (excludes non-controlling
    # interest); distinct from TOTAL_EQUITY, which includes NCI in KR/TW/JP.
    "SHAREHOLDERS_EQUITY":          {"kind": "stock", "per_share": False},
    "NON_CONTROL_INTEREST":         {"kind": "stock", "per_share": False},
    "CONTRACT_LIABILITIES":         {"kind": "stock", "per_share": False},
    # --- additional income-statement / cash-flow line items (flow, discrete) ---
    "OPERATING_EXPENSE":            {"kind": "flow",  "per_share": False},
    "RD_EXPENSE":                   {"kind": "flow",  "per_share": False},
    "SGA_EXPENSE":                  {"kind": "flow",  "per_share": False},
    "TAX_EXPENSE":                  {"kind": "flow",  "per_share": False},
    # consolidated net income INCLUDING non-controlling interest (vs NET_INCOME,
    # which is the portion attributable to the parent's owners).
    "NET_INCOME_INC_NCI":           {"kind": "flow",  "per_share": False},
    "DEPRECIATION_AND_AMORTIZATION": {"kind": "flow", "per_share": False},
    "CASH_FROM_OPERATION":          {"kind": "flow",  "per_share": False},
    "CAPEX":                        {"kind": "flow",  "per_share": False},
}

# Metrics whose API value is sign-flipped before comparing/reporting, to match the
# user's file convention. The APIs report CAPEX as a positive cash amount
# (us-gaap:PaymentsToAcquirePropertyPlantAndEquipment, CN 购建固定资产…), but the
# user's files carry it as a negative (a cash OUTFLOW, e.g. -1,000), so we negate the
# API figure so the signs line up (else every CAPEX row would falsely mismatch).
SIGN_FLIP_METRICS = {"CAPEX"}

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
    # computed subtotals / ratios / cash-flow deltas the user's files carry that
    # aren't a single as-filed line item (so not reconcilable against one field):
    "QUICK_ASSETS",              # current assets − inventory − prepaids (a subtotal)
    "FREE_CASH_FLOW",            # CFO − capex
    "TAX_RATE",                  # tax expense / pre-tax income
    "RD_EXPENSE_OF_REVENUE",     # R&D / revenue
    "SGA_EXPENSE_OF_REVENUE",    # SG&A / revenue
}


def is_derived_code(code: str, canonical: str) -> bool:
    """True if this financial_code is a derived ratio/turnover/delta metric that
    can't be reconciled against a single as-filed API line item."""
    if canonical == DERIVED_SENTINEL:
        return True
    c = code.upper()
    return c in DERIVED_METRICS or c.endswith("_QOQ") or c.endswith("_YOY")


# Operational KPIs and company-defined non-GAAP figures that are NOT drawn from
# the audited financial statements (headcount, wafer volume/ASP, utilization,
# backlog/bookings, book-to-bill, FX rates, non-GAAP revenue/margins). No API
# line item corresponds to them, so we flag them UNSUPPORTED_NONFINANCIAL rather
# than NO_MAPPING (which would wrongly imply "you just forgot to map this").
# Map a code to `NON_FINANCIAL` in metric_map.csv, or match the keywords below.
NONFINANCIAL_SENTINEL = "NON_FINANCIAL"
NONFINANCIAL_METRICS = {
    "FULL_TIME_EMPLOYEES", "UTILIZATION", "WAFER_ASP", "WAFER_SALES",
    "WAFER_SALES_USD", "WAFER_SALES_TWD_YTD", "BILLING 12INCH", "BILLING_12INCH",
    "CAPACITY_12INCH", "BACKLOG", "BOOKING", "BOOK_TO_BILL_RATIO", "FX_RATE",
    "NON_GAAP_REVENUE", "NONGAAP_GROSS_MARGIN", "ADJUSTED_OPERATING_MAFGIN",
    "ADJUSTED_OPERATING_MARGIN",
}
# Any adjusted / non-GAAP / pro-forma figure is a company-defined measure absent
# from the audited statements (each filer picks its own add-backs, and it lives in
# the 8-K earnings-release exhibit, not the GAAP XBRL) — so ADJUSTED*/PRO(_)FORMA*
# join NON(_)GAAP* here. This also stops adjusted_* codes (e.g. adjusted_gross_margin)
# from slipping through to NO_MAPPING, which wrongly implies "just add a mapping".
_NONFIN_KEYWORDS = ("WAFER", "UTILIZATION", "BACKLOG", "BOOKING", "BOOK_TO_BILL",
                    "EMPLOYEE", "HEADCOUNT", "FX_RATE", "NON_GAAP", "NONGAAP",
                    "NON-GAAP", "ADJUSTED", "PRO_FORMA", "PROFORMA", "PRO-FORMA",
                    "12INCH", "12_INCH")


def is_nonfinancial_code(code: str, canonical: str) -> bool:
    """True if this financial_code is an operational KPI or non-GAAP figure that
    isn't an audited-statement line item, so it can't be reconciled against the
    filing APIs at all."""
    if canonical == NONFINANCIAL_SENTINEL:
        return True
    c = code.upper()
    return c in NONFINANCIAL_METRICS or any(k in c for k in _NONFIN_KEYWORDS)

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

    def frames(self, api_id: str, metric: str, fye_month: int = 12,
               years=None) -> dict:
        """Optional: {(cal_year, cal_q): [distinct as-reported values]} for
        periods a source discloses under MORE THAN ONE vintage — e.g. EDGAR keeps
        both the as-originally-filed figure and later restatements of the same
        quarter (a spin-off/discontinued-operations reclass, a prior-period error
        correction, …). Only periods with >1 distinct value are returned. Default:
        none, so `quarterly()`'s single value is the only vintage."""
        return {}


# US SG&A = the combined SG&A tag when a filer reports one, else Selling &
# Marketing + G&A summed (filers like ON Semi split them into two lines, so the
# single G&A tag alone understates SG&A). Shared by SGA_EXPENSE and the
# OPERATING_EXPENSE fallback.
_US_SGA_SPEC = {
    "prefer": ["SellingGeneralAndAdministrativeExpense"],
    "else": {"sum": [["SellingAndMarketingExpense", "SellingExpense"],
                     ["GeneralAndAdministrativeExpense"]]},
}

# Finance-arm filers whose ACCOUNTS_RECEIVABLE — per the user's convention — is
# automotive trade receivables (current) PLUS the captive financing subsidiary's
# receivables (current portion only). Keyed by CIK -> the extra current
# finance-receivable element local-name(s) SUMMED onto us-gaap:AccountsReceivableNetCurrent.
# These lines live in the filing's XBRL instance rather than the companyconcept API
# (GM Financial's is dimensioned on BusinessGroupAxis=GmFinancialMember; Ford
# Credit's is an undimensioned face-statement line) — so they're pulled from the
# instance via EdgarDimensional.instant_series. The matching NON-current finance
# receivable is deliberately excluded (current portion only). Every other US filer
# is unaffected and reports trade receivables alone.
US_FINANCE_RECEIVABLE = {
    "0001467858": ["NotesAndLoansReceivableNetCurrent"],   # GM   -> GM Financial receivables, net (current)
    "0000037996": ["NotesAndLoansReceivableNetCurrent"],   # Ford -> Ford Credit finance receivables, net (current)
    "0000858877": ["NotesAndLoansReceivableNetCurrent"],   # Cisco -> Cisco Systems Capital financing receivables, net (current)
    "0001571996": ["NotesAndLoansReceivableNetCurrent"],   # Dell  -> Dell Financial Services short-term financing receivables, net (current; long-term portion excluded)
    "0001645590": ["NotesAndLoansReceivableNetCurrent"],   # HPE   -> HPE Financial Services financing receivables, net (current; long-term portion excluded)
}

# Filers whose D&A (or its amortization component) is a company custom-extension
# element the companyconcept API can't serve -> read it from the filing instance
# (EdgarDimensional.duration_series, undimensioned) and SUM onto the us-gaap D&A
# series. SLAB has us-gaap Depreciation but tags amortization as a custom line;
# Cisco tags only a combined custom D&A line (its us-gaap D&A resolves empty, so
# this supplies the whole figure). Keyed by CIK -> custom element local-name(s).
US_CUSTOM_DA = {
    # "add": SUM the custom element onto the us-gaap D&A series (filer tags a
    #        separate amortization line as an extension).
    # "replace": the custom element IS the filer's whole reported D&A add-back —
    #        use it alone (the us-gaap series would be a partial component and would
    #        double-count if added).
    "0001038074": {"add": ["AmortizationOfIntangiblesAndOtherAssets"]},  # Silicon Labs: + amortization onto us-gaap Depreciation
    "0000858877": {"replace": ["DepreciationAmortizationAndOther"]},     # Cisco: reported "Depreciation, amortization, and other"
}


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
        "ACCOUNTS_RECEIVABLE": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
                                "AccountsReceivableNet",
                                "AccountsAndOtherReceivablesNetCurrent"],
        "CASH_AND_CASH_EQUIVALENTS": [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"],
        "INVENTORIES": ["InventoryNet"],
        # filers split between the …NetOfReserves and the plain tags, so try both
        "FINISHED_GOODS": ["InventoryFinishedGoodsNetOfReserves",
                           "InventoryFinishedGoods"],
        "RAW_MATERIALS": ["InventoryRawMaterialsNetOfReserves",
                          "InventoryRawMaterials",
                          "InventoryRawMaterialsAndSuppliesNetOfReserves",
                          "InventoryRawMaterialsAndSupplies"],
        "WORK_IN_PROCESS": ["InventoryWorkInProcessNetOfReserves",
                            "InventoryWorkInProcess"],
        "PROPERTY_PLANT_AND_EQUIPMENT": ["PropertyPlantAndEquipmentNet"],
        # parent-owners' equity (excludes NCI); same tag as TOTAL_EQUITY in
        # US-GAAP, where StockholdersEquity is already parent-only.
        "SHAREHOLDERS_EQUITY": ["StockholdersEquity"],
        "NON_CONTROL_INTEREST": ["MinorityInterest"],
        "CONTRACT_LIABILITIES": ["ContractWithCustomerLiabilityCurrent",
                                 "ContractWithCustomerLiability"],
        # operating expenses EXCLUDING COGS: the reported OperatingExpenses line
        # when a filer has one (ON Semi: 354), else SG&A + R&D (Ford has no
        # OperatingExpenses tag and no separate R&D, so this = SG&A = 2,807).
        # Deliberately NOT CostsAndExpenses — that is TOTAL costs incl. COGS, a
        # different metric (Ford 40,924, HPE's total) that this used to return.
        "OPERATING_EXPENSE": {
            "prefer": ["OperatingExpenses", "OperatingCostsAndExpenses"],
            "else": {"sum": [_US_SGA_SPEC,
                             ["ResearchAndDevelopmentExpense",
                              "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]]}},
        "RD_EXPENSE": ["ResearchAndDevelopmentExpense",
                       "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
        "SGA_EXPENSE": _US_SGA_SPEC,
        "TAX_EXPENSE": ["IncomeTaxExpenseBenefit"],
        "NET_INCOME_INC_NCI": ["ProfitLoss"],
        # Total D&A (the cash-flow add-back). Prefer a filer's combined
        # depreciation-AND-amortization tag; otherwise SUM depreciation + amortization
        # of intangibles, since filers that split them (SWKS, QRVO) tag only the
        # depreciation-only `Depreciation` line — taking it alone dropped the
        # amortization. (Filers whose amortization is a custom extension element —
        # SLAB, CSCO — are topped up from the filing instance via US_CUSTOM_DA below.)
        "DEPRECIATION_AND_AMORTIZATION": {
            "prefer": ["DepreciationDepletionAndAmortization",
                       "DepreciationAmortizationAndAccretionNet",
                       "DepreciationAndAmortization"],
            "else": {"sum": [["Depreciation"],
                             ["AmortizationOfIntangibleAssets",
                              "FiniteLivedIntangibleAssetsAmortizationExpense"]]}},
        "CASH_FROM_OPERATION": [
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "CAPEX": ["PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsToAcquireProductiveAssets"],
    }

    def __init__(self):
        self._cik = {}
        self._dim = None

    def _dimensional(self) -> "EdgarDimensional":
        """Lazily-built helper for reading facts straight from filing XBRL instances
        (used for finance-arm receivable lines that never surface in companyconcept)."""
        if self._dim is None:
            self._dim = EdgarDimensional(self)
        return self._dim

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

    @staticmethod
    def _annual_by_end(d):
        """{fiscal-year-end: value} for the ~365-day frames of a concept."""
        out = {}
        for x in (d or {}).get("units", {}).get("USD", []):
            if x.get("start") and x.get("end"):
                s = datetime.date.fromisoformat(x["start"])
                e = datetime.date.fromisoformat(x["end"])
                if 350 <= (e - s).days <= 380:
                    out[x["end"]] = float(x["val"])
        return out

    def _revenue_tag_order(self, cik):
        """Tag priority for REVENUE, with one correction: some filers (e.g. GM) tag
        only part of revenue under RevenueFromContract...ExcludingAssessedTax and put
        the true total under `Revenues` (GM Financial is excluded from the contracts
        tag). If, at the SAME most-recent fiscal year-end, `Revenues` is materially
        larger than the contracts tag, prefer `Revenues` so we pull total revenue.
        Everyone whose tags agree (or who only files one) is unaffected."""
        tags = list(self.metric_map["REVENUE"])
        con = self._annual_by_end(
            self._concept(cik, "RevenueFromContractWithCustomerExcludingAssessedTax"))
        rev = self._annual_by_end(self._concept(cik, "Revenues"))
        common = sorted(set(con) & set(rev))
        if common and rev[common[-1]] > con[common[-1]] * 1.005:
            tags.remove("Revenues")
            tags.insert(0, "Revenues")
        return tags

    def _facts_for(self, cik, tags, unit):
        """Facts of the first tag (priority order) that actually has data in `unit`
        (don't mix tags, e.g. Excluding- vs Including-AssessedTax revenue)."""
        for tag in tags:
            d = self._concept(cik, tag)
            if d and d.get("units", {}).get(unit):
                return d["units"][unit]
        return []

    @staticmethod
    def _series_from_facts(facts, kind):
        """{(cal_y, cal_q): value} from a concept's raw facts — point-in-time for
        stock, discrete quarters for flow (Q4 preferred-as-filed then derived, plus
        a YTD-ladder fallback)."""
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
        # Q4: when the filer actually files a discrete "three months ended
        # [FY-end]" frame (Dell, Flex do), that as-reported value is already in
        # `quarters` — keep it. Only DERIVE Q4 = full-year − (Q1+Q2+Q3) when no
        # discrete Q4 exists. Deriving over an as-filed Q4 was the bug: the annual
        # can carry a different vintage (10-K vs proxy) or a different measure
        # (total net income vs attributable-to-parent) than the quarters, so
        # FY − 9M matched neither (e.g. Dell FY2025 net income $1,533M as-filed
        # vs $1,517M derived; Flex FY2022 revenue $6,851M vs $5,443M).
        for e_annual, ann_val in annuals.items():
            q4_key = cal_key_from_date(e_annual.isoformat())
            if q4_key in quarters:          # discrete Q4 filed -> don't overwrite
                continue
            sub = [v for (k, (v, e)) in quarters.items()
                   if 0 < (e_annual - e).days <= 285]
            if len(sub) == 3:
                out[q4_key] = ann_val - sum(sub)
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

    def _resolve(self, cik, spec, kind, unit):
        """Resolve a metric spec to a discrete series. `spec` is one of:
          - list of tags        -> first tag with data (priority order)
          - {"sum":[spec, ...]} -> add the sub-series per period (each de-cumulated
                                   first, so summing discrete quarters is correct;
                                   e.g. SG&A = Selling&Marketing + G&A)
          - {"prefer":spec, "else":spec} -> prefer if it yields data, else the
                                   fallback (e.g. the OperatingExpenses line, else
                                   SG&A + R&D)"""
        if isinstance(spec, dict) and "sum" in spec:
            total, got = {}, False
            for sub in spec["sum"]:
                s = self._resolve(cik, sub, kind, unit)
                if s:
                    got = True
                for k, v in s.items():
                    total[k] = total.get(k, 0.0) + v
            return total if got else {}
        if isinstance(spec, dict) and "prefer" in spec:
            s = self._resolve(cik, spec["prefer"], kind, unit)
            return s if s else self._resolve(cik, spec["else"], kind, unit)
        tags = spec if isinstance(spec, list) else [spec]
        return self._series_from_facts(self._facts_for(cik, tags, unit), kind)

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        cik = self._resolve_cik(api_id)
        if not cik:
            return {}
        kind = CANONICAL[metric]["kind"]
        unit = "USD/shares" if CANONICAL[metric]["per_share"] else "USD"
        spec = (self._revenue_tag_order(cik) if metric == "REVENUE"
                else self.metric_map[metric])
        series = self._resolve(cik, spec, kind, unit)
        # Finance-arm filers (GM, Ford): ACCOUNTS_RECEIVABLE = trade receivables +
        # the captive-finance subsidiary's current receivables (from the instance).
        if metric == "ACCOUNTS_RECEIVABLE" and cik in US_FINANCE_RECEIVABLE:
            extra = self._dimensional().instant_series(
                cik, US_FINANCE_RECEIVABLE[cik], years)
            series = {k: v + extra.get(k, 0.0) for k, v in series.items()}
        # D&A with a custom-extension amortization/combined element (SLAB, CSCO):
        # add the instance-read custom series (union of keys, since CSCO's us-gaap
        # D&A series is empty and the custom line supplies the whole figure).
        if metric == "DEPRECIATION_AND_AMORTIZATION" and cik in US_CUSTOM_DA:
            cfg = US_CUSTOM_DA[cik]
            if "replace" in cfg:                # custom element IS the whole D&A
                series = self._dimensional().duration_series(cik, cfg["replace"], years)
            else:                               # add the custom amortization component
                extra = self._dimensional().duration_series(cik, cfg["add"], years)
                series = {k: series.get(k, 0.0) + extra.get(k, 0.0)
                          for k in set(series) | set(extra)}
        # OPERATING_EXPENSE = opex EXCLUDING cost of sales. A reported OperatingExpenses
        # tag is trusted only when it's genuinely opex-ex-COGS; some filers tag their
        # TOTAL costs-and-expenses under it (GM), detectable as OperatingExpenses ≈
        # Revenue − OperatingIncome. In that case, or when there is no such tag (the
        # SG&A+R&D fallback, which misses other opex lines like restructuring — STX),
        # derive it from the identity opex = Revenue − COGS − OperatingIncome, which
        # captures every operating-expense line regardless of (even custom) tagging.
        # COGS comes from companyconcept, or the filing instance for filers that tag
        # it only at the business-group segment level (GM). Left unchanged where the
        # identity's inputs (Revenue, OperatingIncome, COGS) aren't all available.
        if metric == "OPERATING_EXPENSE" and series:
            from_prefer = bool(self._resolve(cik, spec["prefer"], "flow", "USD"))
            rev = self.quarterly(api_id, "REVENUE", fye_month, years)
            oi = self.quarterly(api_id, "OPERATING_INCOME", fye_month, years)
            cogs = self.quarterly(api_id, "COGS", fye_month, years)
            cogs_dim = None
            for k in list(series):
                if k not in rev or k not in oi:
                    continue
                if from_prefer and abs(series[k] - (rev[k] - oi[k])) > 0.02 * abs(rev[k] or 1):
                    continue                       # genuine reported opex-ex-COGS -> keep
                c = cogs.get(k)
                if c is None:                      # COGS tagged only at segment level
                    if cogs_dim is None:
                        cogs_dim = self._dimensional().duration_series(
                            cik, ["CostOfGoodsAndServicesSold", "CostOfRevenue",
                                  "CostOfGoodsSold"], years, sum_dims=True)
                    c = cogs_dim.get(k)
                if c is None or c > rev[k]:         # need a positive gross profit
                    continue
                newv = rev[k] - c - oi[k]
                # Guard against de-cumulation artifacts: the identity relies on
                # Revenue/COGS/OperatingIncome each de-cumulating consistently; when a
                # restatement or vintage mismatch makes them disagree, it can go
                # negative/absurd. Accept it only when positive, and — for the
                # else-branch (SG&A+R&D was already a sane lower bound) — only when it
                # doesn't fall BELOW that baseline (it must add opex lines, never drop).
                if newv <= 0:
                    continue
                if not from_prefer and newv < series[k] - 0.005 * abs(rev[k]):
                    continue
                series[k] = newv
        return series

    def frames(self, api_id, metric, fye_month=12, years=None):
        """All distinct as-reported vintages of each directly-filed period, so a
        restated quarter surfaces alongside the as-originally-filed one instead of
        the latest silently overwriting it. E.g. Dell CY2021Q3 COGS: $20,335M as
        first filed (incl. VMware) and $20,890M as later restated to continuing
        operations after the Nov-2021 spin-off — both are returned. Covers only
        directly-reported facts (~90d discrete quarters and point-in-time balances)
        — the quarter where a company files two figures; derived Q4/YTD-ladder
        values aren't 'vintages' and are left to quarterly()."""
        cik = self._resolve_cik(api_id)
        if not cik:
            return {}
        spec = self.metric_map[metric]
        # vintages only apply to a single reported line; computed sums/prefer specs
        # (SG&A, OPERATING_EXPENSE) have no as-filed-vs-restated ambiguity.
        if isinstance(spec, dict):
            return {}
        per_share = CANONICAL[metric]["per_share"]
        kind = CANONICAL[metric]["kind"]
        unit = "USD/shares" if per_share else "USD"
        facts = []
        tag_order = (self._revenue_tag_order(cik) if metric == "REVENUE"
                     else spec)  # same tag quarterly() settled on
        for tag in tag_order:
            d = self._concept(cik, tag)
            if d and d.get("units", {}).get(unit):
                facts = d["units"][unit]
                break
        if not facts:
            return {}
        from collections import defaultdict
        vals = defaultdict(list)                  # (cal_y,cal_q) -> [values, in file order]
        for x in facts:
            if not x.get("end"):
                continue
            if kind == "stock":
                if not x.get("start"):
                    vals[cal_key_from_date(x["end"])].append(float(x["val"]))
                else:
                    s = datetime.date.fromisoformat(x["start"])
                    e = datetime.date.fromisoformat(x["end"])
                    if (e - s).days <= 5:
                        vals[cal_key_from_date(x["end"])].append(float(x["val"]))
            else:
                if not x.get("start"):
                    continue
                s = datetime.date.fromisoformat(x["start"])
                e = datetime.date.fromisoformat(x["end"])
                if 80 <= (e - s).days <= 100:
                    vals[cal_key_from_date(x["end"])].append(float(x["val"]))
        out = {}
        for k, lst in vals.items():
            seen, distinct = set(), []
            for v in lst:
                r = round(v, 2)                   # collapse exact repeats across filings
                if r not in seen:
                    seen.add(r)
                    distinct.append(v)
            if len(distinct) > 1:                 # only ambiguous periods matter
                out[k] = distinct
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

    def members(self, cik, axis_kw, years):
        """All dimensional members present on the segment/geo axis (for enumerating
        a company's disclosed segments/regions when dumping a reference)."""
        out = set()
        for acc, doc in self._filings(cik, years):
            for tag, s, e, dims, val in self._facts(cik, acc, doc):
                if tag not in (self.REV_TAGS + self.OPINC_TAGS):
                    continue
                for k, v in dims.items():
                    if axis_kw in k:
                        out.add(v)
        return out

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

    def _instant_facts(self, cik, acc, doc):
        """All point-in-time (instant) facts in a filing's XBRL instance, as
        [local-name, end-date, dims, value]. Unlike `_facts` (segment/geo durations
        only), this keeps arbitrary balance-sheet tags — including undimensioned
        ones — so line items that never surface in the companyconcept API (e.g. a
        captive-finance subsidiary's receivables) can be read directly."""
        ck = f"edgar_inst_{acc}"
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
                dims, end, is_instant = {}, None, False
                for x in cc.iter():
                    l = _xloc(x.tag)
                    if l == "explicitMember":
                        dims[_xloc(x.get("dimension"))] = _xloc((x.text or "").strip())
                    elif l == "instant":
                        end = x.text
                        is_instant = True
                ctx[cc.get("id")] = (end, is_instant, dims)
            for el in root.iter():
                cr = el.get("contextRef")
                if not cr or cr not in ctx:
                    continue
                end, is_instant, dims = ctx[cr]
                if not is_instant or not end:
                    continue
                try:
                    val = float(el.text)
                except (TypeError, ValueError):
                    continue
                facts.append([_xloc(el.tag), end, dims, val])
        except Exception:
            pass
        _cache_put(ck, facts)
        return facts

    def instant_series(self, cik, localnames, years):
        """Discrete point-in-time series {(cal_year, cal_q): summed value} for the
        given element local-names, read from each filing's balance-sheet instant.
        For a given element at a given instant the fact with the FEWEST dimensions
        is taken (the aggregate line, never a portfolio/segment sub-breakdown), then
        the wanted elements are summed. Handles both undimensioned facts (Ford
        Credit's line) and single-axis facts (GM Financial's BusinessGroupAxis)."""
        want = set(localnames)
        out = {}
        for acc, doc in self._filings(cik, years):
            best = {}   # (end, local-name) -> (n_dims, value)
            for tag, end, dims, val in self._instant_facts(cik, acc, doc):
                if tag not in want:
                    continue
                k = (end, tag)
                if k not in best or len(dims) < best[k][0]:
                    best[k] = (len(dims), val)
            by_end = {}
            for (end, tag), (_nd, val) in best.items():
                by_end[end] = by_end.get(end, 0.0) + val
            for end, total in by_end.items():
                out[cal_key_from_date(end)] = total
        return out

    def _duration_facts(self, cik, acc, doc):
        """All period (duration) facts in a filing's XBRL instance, as
        [local-name, start, end, dims, value]. The duration analogue of
        `_instant_facts` — keeps arbitrary flow tags (income-statement / cash-flow
        lines), including dimensioned ones and company custom-extension elements the
        companyconcept API can't serve."""
        ck = f"edgar_dur_{acc}"
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
                dims, s, e = {}, None, None
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
                if not s or not e:
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

    def duration_series(self, cik, localnames, years, sum_dims=False):
        """Discrete-quarter flow series {(cal_y, cal_q): value} for the given element
        local-names, read from filing instances — for flow facts the companyconcept
        API doesn't serve: a company custom-extension line (sum_dims=False → use the
        undimensioned fact, e.g. Silicon Labs' amortization, Cisco's combined D&A) or
        a line tagged only at the segment / business-group level (sum_dims=True →
        take the undimensioned total if present, else sum across members, e.g. GM's
        cost of sales). Collected per (start,end) across all filings, then handed to
        the shared flow de-cumulator so both ~90d-discrete and YTD-ladder filers
        de-cumulate exactly as the companyconcept path does."""
        want = set(localnames)
        raw = {}                       # (start, end) -> value
        for acc, doc in self._filings(cik, years):
            per = {}                   # (start, end) -> {dim-signature: value}
            for tag, s, e, dims, val in self._duration_facts(cik, acc, doc):
                if tag not in want:
                    continue
                if not sum_dims and dims:
                    continue           # custom top-level line: undimensioned only
                per.setdefault((s, e), {})[frozenset(dims.items())] = val
            for key, sig in per.items():
                raw[key] = sig[frozenset()] if frozenset() in sig else sum(sig.values())
        facts = [{"start": s, "end": e, "val": v, "form": "10-Q"}
                 for (s, e), v in raw.items()]
        return EdgarSource._series_from_facts(facts, "flow")


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
    """Read the '(단위: 백만원)' / '(單位：新台幣仟元)' hint. Returns
    local-currency-per-reported-unit. Covers both the Korean DART notes and the
    Taiwanese MOPS financial-report book (which reports in NT$ thousands, 仟元)."""
    # Korean
    if "조원" in text:
        return 1e12
    if "억원" in text:
        return 1e8
    if "백만원" in text:
        return 1e6
    if "천원" in text:
        return 1e3
    # Taiwanese (Traditional Chinese): 仟元/千元 = thousands, 佰萬元/百萬元 = millions
    if "佰萬元" in text or "百萬元" in text:
        return 1e6
    if "仟元" in text or "千元" in text:
        return 1e3
    return 1.0


def _seg_norm(s: str) -> str:
    return re.sub(r"[\s()\.\-_/*]", "", s).upper()


# Canonicalize a region label (Korean or English) so a user's geographic label
# matches the row label in a Korean 지역별 table.
_REGION_CANON = {
    "한국": "KR", "국내": "KR", "대한민국": "KR", "KOREA": "KR", "SOUTHKOREA": "KR",
    "중국": "CN", "CHINA": "CN", "중화인민공화국": "CN", "대중국": "CN", "중화권": "CN",
    "미국": "US", "미주": "US", "USA": "US", "UNITEDSTATES": "US", "US": "US",
    "대만": "TW", "TAIWAN": "TW",
    "일본": "JP", "JAPAN": "JP",
    "홍콩": "HK", "HONGKONG": "HK",
    # Traditional Chinese region names (Taiwan MOPS financial-report book 地區別)
    "台灣": "TW", "臺灣": "TW",
    "美國": "US", "北美": "US", "北美洲": "US", "美洲": "US", "NORTHAMERICA": "US",
    "AMERICA": "US", "AMERICAS": "US",
    "中國": "CN", "中國大陸": "CN", "大陸": "CN",
    # China A-share 主营构成 geographic labels (domestic / overseas split)
    "境內": "DOMESTIC", "境内": "DOMESTIC", "國內": "DOMESTIC", "国内": "DOMESTIC",
    "中國境內": "DOMESTIC", "中国境内": "DOMESTIC", "大陸地區": "DOMESTIC",
    "DOMESTIC": "DOMESTIC", "境外": "OVERSEAS", "國外": "OVERSEAS", "国外": "OVERSEAS",
    "海外": "OVERSEAS", "中國境外": "OVERSEAS", "中国境外": "OVERSEAS", "OVERSEAS": "OVERSEAS",
    "日本": "JP",
    "歐洲、中東及非洲": "EMEA", "歐洲中東及非洲": "EMEA", "歐非中東": "EMEA",
    "歐中非": "EMEA", "EMEA": "EMEA",
    "歐洲": "EU",
    "亞洲": "ASIA", "其他亞洲": "ASIA",
    "其他": "OTHER", "其它": "OTHER", "其他地區": "OTHER",
    "싱가포르": "SG", "SINGAPORE": "SG",
    "유럽": "EU", "EUROPE": "EU", "구주": "EU", "유럽연합": "EU",
    "독일": "DE", "GERMANY": "DE",
    "아시아": "ASIA", "ASIA": "ASIA", "아태": "ASIA", "아시아태평양": "ASIA",
    "북미": "NA", "북미주": "NA",   # Korean 'North America' region stays NA;
    # English 'North America'/'Americas' unify to US (above) — that is TSMC's
    # single Americas region (美國/North America), which must reconcile as US.
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
    key = re.sub(r"[\s()\.\-_/、，,&・･]", "", s).upper()
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
        # net income ATTRIBUTABLE TO PARENT first (지배기업 소유주 귀속 당기순이익), then
        # fall back to 당기순이익 (total) for a filer that reports no attribution
        # split (no NCI, where the two are equal). _val honours this priority even
        # though the total prints earlier in the statement. Total is NET_INCOME_INC_NCI.
        "NET_INCOME": (["지배기업의소유주에게귀속되는당기순이익",
                        "지배기업의소유주에게귀속되는분기순이익",
                        "지배기업의소유주에게귀속되는반기순이익",
                        "지배기업소유주지분순이익", "지배기업소유주지분",
                        "당기순이익", "연결당기순이익",
                        "연결분기순이익", "연결반기순이익", "분기순이익", "반기순이익"],
                       ("IS", "CIS")),
        "EPS_BASIC": (["기본주당이익", "기본주당이익(손실)", "기본주당순이익"], ("IS", "CIS")),
        "EPS_DILUTED": (["희석주당이익", "희석주당이익(손실)", "희석주당순이익"], ("IS", "CIS")),
        "ACCOUNTS_PAYABLE": (["매입채무", "매입채무및기타채무"], ("BS",)),
        "CURRENT_ASSETS": (["유동자산"], ("BS",)),
        "TOTAL_ASSETS": (["자산총계"], ("BS",)),
        "CURRENT_LIABILITIES": (["유동부채"], ("BS",)),
        "TOTAL_LIABILITIES": (["부채총계"], ("BS",)),
        "TOTAL_EQUITY": (["자본총계"], ("BS",)),
        "ACCOUNTS_RECEIVABLE": (["매출채권", "매출채권및기타채권",
                                 "매출채권및기타유동채권"], ("BS",)),
        "CASH_AND_CASH_EQUIVALENTS": (["현금및현금성자산"], ("BS",)),
        "INVENTORIES": (["재고자산"], ("BS",)),
        "PROPERTY_PLANT_AND_EQUIPMENT": (["유형자산"], ("BS",)),
        # parent-owners' equity (지배기업 소유주지분) — excludes NCI, unlike 자본총계.
        "SHAREHOLDERS_EQUITY": (["지배기업의소유주에게귀속되는자본", "지배기업소유주지분",
                                 "지배기업의소유주지분", "지배기업 소유주지분"], ("BS",)),
        "NON_CONTROL_INTEREST": (["비지배지분"], ("BS",)),
        "CONTRACT_LIABILITIES": (["계약부채"], ("BS",)),
        "SGA_EXPENSE": (["판매비와관리비", "판매비및관리비"], ("IS", "CIS")),
        "RD_EXPENSE": (["경상연구개발비", "연구개발비"], ("IS", "CIS")),
        "TAX_EXPENSE": (["법인세비용", "법인세비용(수익)"], ("IS", "CIS")),
        # total profit incl. NCI. Filers label it 당기/분기/반기순이익, with or
        # without the 연결 (consolidated) prefix — e.g. Hyundai uses 연결분기순이익.
        "NET_INCOME_INC_NCI": (["당기순이익", "당기순이익(손실)", "연결당기순이익",
                                "연결분기순이익", "연결반기순이익",
                                "분기순이익", "반기순이익"], ("IS", "CIS")),
        # cash-flow (sj_div 'CF'); YTD-cumulative like the income statement, so the
        # flow de-cumulation (_to_discrete) turns it into discrete quarters.
        "CASH_FROM_OPERATION": (["영업활동현금흐름", "영업활동으로인한현금흐름",
                                 "영업활동으로인한순현금흐름"], ("CF",)),
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

    @staticmethod
    def _norm_acct(s: str) -> str:
        """Normalize an account name for matching: drop all whitespace and a trailing
        '(손실)'/'(손익)' loss/PL parenthetical, so a candidate like '분기순이익' matches
        the filed row '분기순이익(손실)' (many filers append the loss suffix)."""
        s = re.sub(r"\s", "", s or "")
        return re.sub(r"\((손실|손익)\)$", "", s)

    def _val(self, data, names, sj_divs, field="thstrm_amount"):
        """Value of the first matching account line, from column `field`
        (thstrm_amount = current 3-month/period; thstrm_add_amount = YTD cumulative).
        Matching is in CANDIDATE-PRIORITY order (not statement order): a consolidated
        statement lists BOTH 당기순이익 (total) and the 지배기업 소유주 귀속 line, total
        first, so we prefer the earlier-listed candidate (parent). Whitespace and the
        '(손실)' loss suffix are normalized away."""
        if not data or data.get("status") != "000":
            return None
        rows = data.get("list", [])
        for name in names:
            want = self._norm_acct(name)
            for row in rows:
                nm = self._norm_acct(row.get("account_nm") or "")
                if nm == want and row.get("sj_div") in sj_divs:
                    raw = (row.get(field) or "").replace(",", "").strip()
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
            vals = {}       # q -> current 3-month/period column (thstrm_amount)
            cum = {}        # q -> YTD cumulative column (thstrm_add_amount), if filed
            for q, reprt in self.REPRT.items():
                # consolidated preferred; fall back to separate if no value
                data = self._fs(corp, year, reprt, "CFS")
                v = self._val(data, names, sj)
                a = self._val(data, names, sj, field="thstrm_add_amount")
                if v is None and a is None:
                    data = self._fs(corp, year, reprt, "OFS")
                    v = self._val(data, names, sj)
                    a = self._val(data, names, sj, field="thstrm_add_amount")
                if v is not None:
                    vals[q] = v
                if a is not None:
                    cum[q] = a
            if not vals:
                continue
            if kind == "stock":
                for q, v in vals.items():
                    out[(year, q)] = v
            else:
                for q, v in self._flow_discrete(vals, cum).items():
                    out[(year, q)] = v
        # The user's SG&A EXCLUDES R&D, but Korean 판매비와관리비 INCLUDES the R&D
        # portion (경상연구개발비/연구비, disclosed only in the notes). Subtract it so
        # SGA_EXPENSE = 판관비 − R&D-in-SG&A. Filers who instead carry R&D in COGS
        # have no SG&A R&D note row, so nothing is subtracted (SG&A unchanged).
        if metric == "SGA_EXPENSE":
            rd, drop = self._sga_rd_series(corp, year_range)
            out = {k: v - rd.get(k, 0.0)
                   for k, v in out.items() if k not in drop}
        return out

    def frames(self, api_id, metric, fye_month=12, years=None):
        """Restated (as-later-filed) vintages, recovered from the 전기 (prior-year)
        comparative columns every DART report carries. A report for year Y+1 restates
        period Y in its prior-year columns: on the income/CF statement interim reports
        carry frmtrm_q_amount (3-month) and frmtrm_add_amount (YTD), the annual carries
        frmtrm_amount (full year); on the balance sheet frmtrm_amount is the prior
        YEAR-END balance. So reading year Y+1's reports yields a SECOND vintage of each
        year-Y period — identical to quarterly()'s figure unless the filer restated it,
        in which case run() surfaces both. Flow lines are de-cumulated from the frmtrm
        columns with the SAME _flow_discrete/_to_discrete machinery quarterly() uses on
        the thstrm columns; stock lines take the prior year-end balance as (Y,4).

        Limits: comparatives expose only the immediately-preceding year, so just the
        latest restatement of a period is seen (no full vintage chain), and interim
        balance-sheet comparatives carry the prior YEAR-END only — restated interim
        balances (Y,1..3) aren't recoverable, only (Y,4). Per the handoff rule a period
        whose frmtrm columns are absent/ambiguous emits nothing: a false vintage would
        cause a false MATCH, worse than no vintage. SGA_EXPENSE (adjusted by an R&D
        note in quarterly()) has no clean frmtrm comparative, so no vintage is emitted."""
        if not self.available:
            return {}
        corp = self._corp_map().get(api_id.strip())
        if not corp:
            return {}
        # 판관비 is post-processed (R&D-in-SG&A subtracted from a note); the raw frmtrm
        # comparative is R&D-inclusive and would differ from the primary for a benign
        # reason -> a spurious differing vintage -> a false MATCH. Emit nothing.
        if metric == "SGA_EXPENSE":
            return {}
        names, sj = self.metric_map[metric]
        kind = CANONICAL[metric]["kind"]
        if years:
            year_range = sorted(set(int(y) for y in years))
        else:
            year_range = range(2015, datetime.date.today().year + 1)
        out = {}
        for year in year_range:
            ny = year + 1                # reports that carry `year` as their 전기 column
            if kind == "stock":
                # prior year-end balance (전기말) restated in year+1's annual report.
                data = self._fs(corp, ny, self.REPRT[4], "CFS")
                v = self._val(data, names, sj, field="frmtrm_amount")
                if v is None:
                    data = self._fs(corp, ny, self.REPRT[4], "OFS")
                    v = self._val(data, names, sj, field="frmtrm_amount")
                if v is not None:
                    out[(year, 4)] = [v]
                continue
            # flow: rebuild year `year`'s discrete series from year+1's frmtrm columns,
            # mirroring quarterly() exactly but reading the prior-year fields. The period
            # column is frmtrm_q_amount on interim reports and frmtrm_amount on the annual
            # (which has no _q field); the cumulative column is frmtrm_add_amount.
            vals, cum = {}, {}
            for q, reprt in self.REPRT.items():
                data = self._fs(corp, ny, reprt, "CFS")
                v = self._val(data, names, sj, field="frmtrm_q_amount")
                if v is None:
                    v = self._val(data, names, sj, field="frmtrm_amount")
                a = self._val(data, names, sj, field="frmtrm_add_amount")
                if v is None and a is None:
                    data = self._fs(corp, ny, reprt, "OFS")
                    v = self._val(data, names, sj, field="frmtrm_q_amount")
                    if v is None:
                        v = self._val(data, names, sj, field="frmtrm_amount")
                    a = self._val(data, names, sj, field="frmtrm_add_amount")
                if v is not None:
                    vals[q] = v
                if a is not None:
                    cum[q] = a
            # need at least one interim quarter to de-cumulate: a lone full-year figure
            # (annual frmtrm_amount only) can't be split into a Q4 discrete without the
            # 9-month ladder, and _to_discrete would wrongly emit FY as Q4. Skip it.
            if not any(q in vals for q in (1, 2, 3)):
                continue
            for q, v in self._flow_discrete(vals, cum).items():
                out[(year, q)] = [v]
        return out

    @staticmethod
    def _sga_note_rd(text):
        """R&D expense included in 판매비와관리비 (won), from the SG&A functional-
        breakdown note in a periodic report. Returns the note's current-period column
        for the R&D line (경상연구개발비 / 경상개발비 / 연구비, …) — discrete 3개월 in an
        interim report, full-year in an annual — or None when the filer discloses no
        SG&A R&D line (its R&D sits in COGS instead). The first qualifying table in
        document order is the CONSOLIDATED (연결) note. Returned values share the same
        cumulative-or-discrete basis across a year's reports, so they de-cumulate
        exactly like the SG&A total via _to_discrete."""
        for m in re.finditer(r"<TABLE\b", text, re.I):
            ts = m.start()
            te = text.find("</TABLE>", ts)
            if te < 0:
                continue
            rows = _html_tables(text[ts:te + 8])
            if not rows:
                continue
            t = rows[0]
            # SG&A functional breakdown: carries the 판매비와관리비 total row and a
            # 급여 (salaries) component row — distinguishes it from the income
            # statement (판관비 as one P&L line, no components) and the R&D-activity
            # note (no 판매비와관리비 total).
            if not any(_seg_norm(c) == _seg_norm("판매비와관리비") for r in t for c in r):
                continue
            if not any("급여" in c for r in t for c in r):
                continue
            rd_row = None
            for r in t:
                labels = []
                for c in r:
                    if _kr_num(c) is not None:
                        break
                    labels.append(c)
                lab = " ".join(labels)
                if ("연구" in lab or "개발" in lab) and "무형자산" not in lab \
                        and "개척" not in lab:
                    rd_row = r
                    break
            if rd_row is None:
                continue
            mult = _unit_multiplier(text[max(0, ts - 500):ts])
            nums = [n for n in (_kr_num(c) for c in rd_row) if n is not None]
            if nums:
                return nums[0] * mult
        return None

    def _sga_rd_series(self, corp, year_range):
        """Returns (rd, drop): rd is {(cal_y, cal_q): R&D_won_within_SG&A} discrete
        quarters to subtract from 판관비; drop is a set of (cal_y, cal_q) whose SG&A
        must be reported as no value. The R&D line is read from each report's
        판매비와관리비 note and de-cumulated with the same machinery as the SG&A total.

        A filer with NO SG&A R&D line (its R&D sits in COGS) yields empty rd/drop —
        SG&A is left unchanged. But once a year is known to carry R&D in SG&A (any
        interim report discloses it), every quarter of that year must be R&D-excluded
        to stay consistent; a quarter whose R&D can't be recovered — e.g. Q4 when the
        annual report doesn't repeat the SG&A functional breakdown the interim reports
        use — is added to `drop` (no value) rather than left R&D-inclusive (wrong)."""
        rd, drop = {}, set()
        for year in year_range:
            vals = {}
            for q, reprt in self.REPRT.items():
                rc = self._rcept_for(corp, year, q)
                if not rc:
                    continue
                try:
                    v = self._sga_note_rd(self._dart_document(rc))
                except Exception:
                    v = None
                if v is not None:
                    vals[q] = v
            if not vals:
                continue                       # no R&D in SG&A -> leave SG&A alone
            disc = self._to_discrete(vals)
            for q, v in disc.items():
                rd[(year, q)] = v
            # this year carries R&D in SG&A; any quarter we couldn't derive an R&D
            # value for can't be reliably R&D-excluded -> drop it.
            for q in (1, 2, 3, 4):
                if q not in disc:
                    drop.add((year, q))
        return rd, drop

    @classmethod
    def _flow_discrete(cls, vals: dict, cum: dict) -> dict:
        """Discrete quarters for a flow line. `vals` is the 3-month/period column
        (thstrm_amount) per report quarter 1..4; `cum` is the YTD-cumulative column
        (thstrm_add_amount) where the filer files one (interim reports only; the
        annual has no cumulative column, its full-year figure is vals[4]).

        When the cumulative column is available for any interim quarter we
        de-cumulate from it DETERMINISTICALLY (Q_n = YTD_n − YTD_{n-1};
        Q4 = FY − YTD_9M) instead of guessing whether the 3-month column is really
        cumulative. This fixes back-loaded filers where the Q3 3-month figure happens
        to be ~half the year (e.g. LX Semicon 2020: Q3 3-month 36,959 is 51% of the
        annual, which the ratio heuristic misread as a 9-month cumulative — turning a
        16,014 Q4 into 35,570). Falls back to the ratio-based `_to_discrete` on the
        3-month column when no cumulative column was filed."""
        if not any(q in cum for q in (1, 2, 3)):
            return cls._to_discrete(vals)
        fy = vals.get(4)
        # cumulative ladder Q1..Q3: prefer the YTD column, fall back to the 3-month
        # value only where the YTD column is missing for that quarter.
        C = {}
        for q in (1, 2, 3):
            if q in cum:
                C[q] = cum[q]
            elif q in vals:
                C[q] = vals[q]
        out = {}
        for q in (1, 2, 3):
            if q not in C:
                continue
            if q == 1:
                out[q] = C[q]
            elif (q - 1) in C:                 # need the contiguous prior YTD to diff
                out[q] = C[q] - C[q - 1]
        if fy is not None and 3 in C:
            out[4] = fy - C[3]                 # Q4 = full year − 9-month cumulative
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
    def _note_windows(text, is_geo):
        """Candidate slices of the report that may hold the 영업부문 / 지역별 note
        table, one per anchor occurrence (latest first). Document layout varies a
        lot by filer and between quarterly and annual reports, so a single fixed
        anchor is unreliable — the caller tries each window until one parses."""
        anchors = (["지역별 부문정보", "지역별 매출", "지역에 대한", "지역별"] if is_geo
                   else ["보고부문", "영업부문에 대한", "영업부문 정보", "부문별 정보"])
        # The note phrase appears in several places (business overview, the
        # consolidated note, the separate-financials note); their order varies by
        # filer and between quarterly vs annual reports. Rather than guess one
        # position, return a window around each occurrence and let the caller try
        # them in turn. Ordering: most-specific anchor first, and within an anchor
        # in DOCUMENT order — the consolidated (연결) note precedes the separate
        # (별도) one, and consolidated is what a reconciliation should use.
        seen, windows = [], []
        for a in anchors:
            occ, start = [], 0
            while True:
                i = text.find(a, start)
                if i < 0:
                    break
                occ.append(i)
                start = i + 1
            for i in occ:                       # document order
                if any(abs(i - p) < 3000 for p in seen):
                    continue
                seen.append(i)
                windows.append(text[i - 500:i + 25000])
                if len(windows) >= 10:
                    return windows
        return windows

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
        for note in self._note_windows(text, is_geo=True):
            v = self._region_value_in(note, region_canon, col)
            if v is not None:
                return v
        return None

    def _region_value_in(self, note, region_canon, col):
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

    # revenue-ish row/column labels in a Korean 영업부문 note table
    _SEG_REV_KW = ("매출액", "매출", "수익", "영업수익", "외부고객")
    _SEG_TOTAL = {"계", "합계", "소계", "합 계", "총계", "부문계", "연결"}

    @staticmethod
    def _read_segment_revenue(section, seg_target, mult):
        """Read a business-segment's revenue from a 영업부문 note table slice.
        Handles the transposed layout (segments across the header, a 매출액 row) and
        the segments-as-rows layout (segment labels down the first column)."""
        for tbl in _html_tables(section):
            # --- transposed: segments are column headers, 매출액 is a row ---
            for hdr in tbl:
                col_of = {}
                for j, c in enumerate(hdr):
                    cn = _seg_norm(c)
                    if cn and cn not in {_seg_norm(t) for t in
                                         OpenDartSource._SEG_TOTAL}:
                        col_of[cn] = j
                tj = next((j for cn, j in col_of.items()
                           if len(seg_target) >= 2 and seg_target in cn), None)
                if tj is None:
                    continue
                for r in tbl:                       # the revenue row
                    if r and any(k in r[0] for k in OpenDartSource._SEG_REV_KW):
                        if tj < len(r):
                            v = _kr_num(r[tj])
                            if v is not None:
                                return v * mult
            # --- segments as rows: label in col 0, a revenue column ---
            rev_j = None
            if tbl:
                for j, h in enumerate(tbl[0]):
                    if any(k in h for k in OpenDartSource._SEG_REV_KW):
                        rev_j = j
                        break
            for r in tbl:
                if not r:
                    continue
                ln = _seg_norm(r[0])
                if len(seg_target) < 2 or seg_target not in ln:
                    continue
                if ln in {_seg_norm(t) for t in OpenDartSource._SEG_TOTAL}:
                    continue
                nums = [n for n in (_kr_num(c) for c in r[1:]) if n is not None]
                if not nums:
                    continue
                idx = (rev_j - 1) if (rev_j and 0 <= rev_j - 1 < len(nums)) else 0
                return nums[idx] * mult
        return None

    def _segment_revenue(self, text, seg_target, cumulative):
        """Business-segment revenue (won) from the 영업부문 note. Interim reports
        carry a discrete 당분기(3개월) table and a 당분기(누적) table; annual reports
        carry a single current-year (당기) table. Tries each candidate note window."""
        for note in self._note_windows(text, is_geo=False):
            mult = _unit_multiplier(note)
            cut = note.find("누적")          # discrete section precedes the cumulative
            if cumulative:
                if cut < 0:
                    section = note        # annual report: single full-year table
                else:
                    end = min([p for p in (note.find("전분기", cut),
                                           note.find("전기", cut)) if p > 0]
                              or [len(note)])
                    section = note[cut:end]
            else:
                section = note[:cut] if cut > 0 else note
            v = self._read_segment_revenue(section, seg_target, mult)
            if v is not None:
                return v
        return None

    def segment_quarterly(self, api_id, fye_month, years, label, want, is_geo):
        """Discrete-quarter {(cal_y, cal_q): value_won} for a Korean GEOGRAPHIC
        (지역별) region or business-SEGMENT (영업부문) revenue label. Q1–Q3 read the
        note's discrete value directly; Q4 = full-year − 9-month cumulative.

        Scope: **revenue** only. Korean filings don't break operating income out by
        region, and only some filers disclose it by segment, so op-income returns
        empty. Geographic uses the 지역별 note; business segments use the 영업부문
        note's 당분기(3개월) / 당분기(누적) tables. Business-segment labels must be
        mapped to the note's 부문 name in segment_members.csv."""
        if not self.available or want == "opincome":
            return {}
        corp = self._corp_map().get(api_id.strip())
        if not corp:
            return {}
        region_canon = _canon_region(label) if is_geo else None
        seg_target = None if is_geo else _seg_norm(label)

        def discrete(text):
            return (self._region_value(text, region_canon, "discrete") if is_geo
                    else self._segment_revenue(text, seg_target, cumulative=False))

        def cumulative(text):
            return (self._region_value(text, region_canon, "cumulative") if is_geo
                    else self._segment_revenue(text, seg_target, cumulative=True))

        def full_year(text):
            return (self._region_value(text, region_canon, "current") if is_geo
                    else self._segment_revenue(text, seg_target, cumulative=True))

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
                        v = discrete(self._dart_document(rc))
                    else:  # Q4 = full-year − 9-month cumulative
                        rc4 = self._rcept_for(corp, year, 4)
                        rc3 = self._rcept_for(corp, year, 3)
                        if not rc4 or not rc3:
                            continue
                        fy = full_year(self._dart_document(rc4))
                        c3 = cumulative(self._dart_document(rc3))
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
        # net income ATTRIBUTABLE TO PARENT (母公司業主淨利), matching how "net
        # income" is universally reported. Falls back to IncomeAfterTaxes (本期淨利,
        # total) only for a filer that reports no attributable split (no NCI).
        # The total-incl-NCI figure is NET_INCOME_INC_NCI. This matters a lot for
        # high-NCI filers (Pegatron, Hon Hai): the two differ by 10%+.
        "NET_INCOME": (IS, ["EquityAttributableToOwnersOfParent", "IncomeAfterTaxes"]),
        "EPS_BASIC": (IS, "EPS"),
        # trade payables (應付帳款) + payables to related parties (應付帳款-關係人):
        # Taiwan files these as two lines; data providers report the sum as
        # "accounts payable", so sum them to match.
        "ACCOUNTS_PAYABLE": (BS, {"sum": ["AccountsPayable",
                                          "AccountsPayableToRelatedParties"]}),
        "CURRENT_ASSETS": (BS, "CurrentAssets"),
        "TOTAL_ASSETS": (BS, "TotalAssets"),
        "CURRENT_LIABILITIES": (BS, "CurrentLiabilities"),
        "TOTAL_LIABILITIES": (BS, "Liabilities"),
        "TOTAL_EQUITY": (BS, "Equity"),
        # trade receivables (應收帳款) + receivables from related parties
        # (應收帳款-關係人); summed to match how providers/the user's data report
        # "accounts receivable" — same convention as ACCOUNTS_PAYABLE above.
        "ACCOUNTS_RECEIVABLE": (BS, {"sum": ["AccountsReceivableNet",
                                             "AccountsReceivableDuefromRelatedPartiesNet"]}),
        "CASH_AND_CASH_EQUIVALENTS": (BS, "CashAndCashEquivalents"),
        "INVENTORIES": (BS, "Inventories"),
        "PROPERTY_PLANT_AND_EQUIPMENT": (BS, "PropertyPlantAndEquipment"),
        "SHAREHOLDERS_EQUITY": (BS, "EquityAttributableToOwnersOfParent"),
        "NON_CONTROL_INTEREST": (BS, "NoncontrollingInterests"),
        # income statement is discrete-quarterly in FinMind; TAX = 所得稅費用,
        # OperatingExpenses = 營業費用, IncomeAfterTaxes = 本期淨利 (incl. NCI).
        "OPERATING_EXPENSE": (IS, "OperatingExpenses"),
        "TAX_EXPENSE": (IS, "TAX"),
        "NET_INCOME_INC_NCI": (IS, "IncomeAfterTaxes"),
    }
    # NOTE: FinMind's cash-flow dataset is YTD-cumulative and this source doesn't
    # de-cumulate, so CASH_FROM_OPERATION / CAPEX / D&A are intentionally left off
    # (they'd produce false Q2–Q4 mismatches) and report UNSUPPORTED_METRIC for TW.

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
        rows = d.get("data", [])
        # {"sum": [...]} -> ADD the sub-lines per period (e.g. trade accounts
        # payable + accounts payable to related parties). A period keeps whatever
        # sub-lines it reports (a missing one just contributes nothing).
        if isinstance(typ, dict) and "sum" in typ:
            out = {}
            for r in rows:
                if r.get("type") in typ["sum"]:
                    try:
                        k = cal_key_from_date(r["date"])
                        out[k] = out.get(k, 0.0) + float(r["value"])
                    except (TypeError, ValueError):
                        pass
            return out
        # list -> try the types in priority order, first with any data wins
        types = typ if isinstance(typ, (list, tuple)) else [typ]
        for t in types:
            out = {cal_key_from_date(r["date"]): float(r["value"])
                   for r in rows if r.get("type") == t}
            if out:
                return out
        return {}


# ---- China A-shares: Eastmoney F10 (the data AKShare wraps) ---------------- #
class AKShareSource(Source):
    """China A-share quarterly statements via the Eastmoney F10 abstract endpoints
    (`RPT_DMSK_FN_INCOME` / `RPT_DMSK_FN_BALANCE`) — the same data AKShare wraps,
    called directly with stdlib so the tool stays dependency-light (AKShare needs
    pandas + a build-fragile antlr4/jsonpath chain). Chinese income statements are
    filed **year-to-date cumulative** (Q1=3M, H1=6M, 9M, FY), so flow metrics are
    de-cumulated into discrete quarters; balance-sheet items are point-in-time.
    Values are full RMB. Segment/geo (分部/地区) is not covered here — it lives in the
    annual-report notes on cninfo (巨潮), which would need PDF parsing like Taiwan."""
    market = "cn"
    EM = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    # DMSK_* are the compact "abstract" (摘要) reports; G* are the FULL
    # income/balance/cash-flow statements (many more line items). Both are the
    # same datacenter API and both YTD-cumulative for flow items.
    INC, BAL = "RPT_DMSK_FN_INCOME", "RPT_DMSK_FN_BALANCE"
    GINC, GBAL, GCF = ("RPT_F10_FINANCE_GINCOME", "RPT_F10_FINANCE_GBALANCE",
                       "RPT_F10_FINANCE_GCASHFLOW")
    metric_map = {   # canonical -> (reportName, field | [fields to sum])
        "REVENUE":          (INC, "TOTAL_OPERATE_INCOME"),   # 营业总收入
        "COGS":             (INC, "OPERATE_COST"),           # 营业成本
        "OPERATING_INCOME": (INC, "OPERATE_PROFIT"),         # 营业利润
        "PRE_TAX_INCOME":   (INC, "TOTAL_PROFIT"),           # 利润总额
        "NET_INCOME":       (INC, "PARENT_NETPROFIT"),       # 归母净利润
        "TAX_EXPENSE":      (INC, "INCOME_TAX"),             # 所得税费用
        "TOTAL_ASSETS":       (BAL, "TOTAL_ASSETS"),         # 资产总计
        "TOTAL_LIABILITIES":  (BAL, "TOTAL_LIABILITIES"),    # 负债合计
        "TOTAL_EQUITY":       (BAL, "TOTAL_EQUITY"),         # 股东权益合计
        "ACCOUNTS_PAYABLE":   (BAL, "ACCOUNTS_PAYABLE"),     # 应付账款
        "ACCOUNTS_RECEIVABLE":          (BAL, "ACCOUNTS_RECE"),   # 应收账款
        "CASH_AND_CASH_EQUIVALENTS":    (BAL, "MONETARYFUNDS"),   # 货币资金
        "INVENTORIES":                  (BAL, "INVENTORY"),       # 存货
        "PROPERTY_PLANT_AND_EQUIPMENT": (BAL, "FIXED_ASSET"),     # 固定资产
        # ---- from the full statements (G*) ----
        "SHAREHOLDERS_EQUITY":  (GBAL, "TOTAL_PARENT_EQUITY"),  # 归属母公司股东权益合计
        "NON_CONTROL_INTEREST": (GBAL, "MINORITY_EQUITY"),      # 少数股东权益
        "CONTRACT_LIABILITIES": (GBAL, "CONTRACT_LIAB"),        # 合同负债
        "RD_EXPENSE":           (GINC, "RESEARCH_EXPENSE"),     # 研发费用
        # 销售费用 + 管理费用 (Chinese GAAP files these as two lines; SG&A = their sum)
        "SGA_EXPENSE":          (GINC, ["SALE_EXPENSE", "MANAGE_EXPENSE"]),
        "NET_INCOME_INC_NCI":   (GINC, "NETPROFIT"),           # 净利润 (含少数股东损益)
        "CASH_FROM_OPERATION":  (GCF, "NETCASH_OPERATE"),      # 经营活动现金流量净额
        "CAPEX":                (GCF, "CONSTRUCT_LONG_ASSET"),  # 购建固定/无形/其他长期资产
        # DEPRECIATION_AND_AMORTIZATION is intentionally NOT mapped for China: the
        # depreciation/amortization add-backs (FA_IR_DEPR / IA_AMORTIZE / …) live in
        # the cash-flow *supplementary* schedule (补充资料), which A-share issuers
        # disclose only semi-annually (06-30 H1 + 12-31 FY), never in the Q1/Q3
        # reports — so it can't be de-cumulated to a discrete quarter. Reports
        # UNSUPPORTED_METRIC rather than a perpetual (misleading) MISSING.
    }
    available = True

    @staticmethod
    def _secucode(api_id):
        s = api_id.strip().upper()
        if "." in s:
            return s
        if s[0] == "6":
            return s + ".SH"        # Shanghai
        if s[0] in "489":
            return s + ".BJ"        # Beijing (also 8/4 boards)
        return s + ".SZ"            # Shenzhen (0/3)

    def _fetch(self, report, secucode):
        ck = f"akshare_em_{report}_{secucode}"
        c = _cache_get(ck)
        if c is not None:
            return c
        q = {"reportName": report, "columns": "ALL", "source": "HSF10",
             "client": "PC", "filter": f'(SECUCODE="{secucode}")',
             "pageNumber": "1", "pageSize": "300",
             "sortColumns": "REPORT_DATE", "sortTypes": "-1"}
        rows = []
        try:
            r = SESSION.get(self.EM + "?" + urllib.parse.urlencode(q),
                            headers={"User-Agent": "Mozilla/5.0",
                                     "Referer": "https://emweb.securities.eastmoney.com/"},
                            timeout=60)
            rows = (r.json().get("result") or {}).get("data") or []
        except Exception:
            rows = []
        _cache_put(ck, rows)
        return rows

    def quarterly(self, api_id, metric, fye_month=12, years=None):
        report, field = self.metric_map[metric]
        fields = field if isinstance(field, (list, tuple)) else [field]
        rows = self._fetch(report, self._secucode(api_id))
        vals = {}
        for r in rows:
            rd = r.get("REPORT_DATE")
            if not rd:
                continue
            parts = [r.get(f) for f in fields]
            if all(p is None for p in parts):      # metric absent for this filer
                continue
            try:                                   # sum the sub-lines present
                vals[rd[:10]] = sum(float(p) for p in parts if p is not None)
            except (TypeError, ValueError):
                pass
        if CANONICAL[metric]["kind"] == "stock":
            return {cal_key_from_date(d): v for d, v in vals.items()}
        # flow: YTD cumulative -> de-cumulate within each calendar year
        from collections import defaultdict
        by_year = defaultdict(dict)
        for d, v in vals.items():
            by_year[int(d[:4])][quarter_of(int(d[5:7]))] = (d, v)
        out = {}
        for qm in by_year.values():
            for q in (1, 2, 3, 4):
                if q not in qm:
                    continue
                d, cum = qm[q]
                if q == 1:
                    out[cal_key_from_date(d)] = cum
                elif (q - 1) in qm:
                    out[cal_key_from_date(d)] = cum - qm[q - 1][1]
                # else: prior quarter missing -> can't de-cumulate, skip
        return out

    # ---- segment / geographic revenue (主营构成, 主营业务分地区/分产品/分行业) ---- #
    # Chinese issuers disclose the main-business breakdown only in the ANNUAL and
    # HALF-YEAR reports (report dates 12-31 and 06-30) — never in the Q1/Q3 reports.
    # So this returns CUMULATIVE half-year (mapped to Q2) and full-year (mapped to
    # Q4) revenue, NOT discrete quarters. Geography is usually a 境内/境外
    # (domestic/overseas) split; segments are 产品 (product) / 行业 (industry).
    MAINOP = "RPT_F10_FN_MAINOP"

    def _mainop(self, secucode):
        return self._fetch(self.MAINOP, secucode)

    def semiannual_composition(self, api_id, fye_month, years, label, want, is_geo):
        """{(cal_y, cal_q): value_RMB} for a Chinese GEOGRAPHIC region (分地区) or
        business SEGMENT (分产品/分行业) main-business revenue, keyed H1->Q2 and
        FY->Q4, both CUMULATIVE. This is the *only* granularity Chinese issuers
        disclose. NOT used by the discrete-quarter reconciliation (see
        segment_quarterly) — exposed for manual/aggregate spot-checks (e.g. confirm
        the user's Q1+Q2 sums to the disclosed H1, or Q1..Q4 to the full year).
        Geographic labels auto-match (境内/境外 and named regions); segment labels
        match the 产品/行业 item name (substring)."""
        if want == "opincome":
            return {}
        rows = self._mainop(self._secucode(api_id))
        seg_target = None if is_geo else _seg_norm(label)
        region = _canon_region(label) if is_geo else None
        types = ("3",) if is_geo else ("2", "1")   # 3=地区; 2=产品 then 1=行业

        def collect(mainop_type):
            out = {}
            for r in rows:
                if r.get("MAINOP_TYPE") != mainop_type:
                    continue
                item = r.get("ITEM_NAME") or ""
                v = r.get("MAIN_BUSINESS_INCOME")
                rd = r.get("REPORT_DATE")
                if v is None or not rd:
                    continue
                if is_geo:
                    if _canon_region(item) != region:
                        continue
                else:
                    if not seg_target or seg_target not in _seg_norm(item):
                        continue
                mo = int(rd[5:7])
                q = {6: 2, 12: 4}.get(mo)          # H1 -> Q2, FY -> Q4
                if q is None:
                    continue
                try:
                    out[(int(rd[:4]), q)] = float(v)
                except (TypeError, ValueError):
                    pass
            return out

        for t in types:                            # product preferred over industry
            got = collect(t)
            if got:
                return got
        return {}

    def segment_quarterly(self, api_id, fye_month, years, label, want, is_geo):
        """China discloses segment/geographic revenue ONLY in the half-year and
        annual reports (cumulative) — never for discrete quarters. This tool
        reconciles DISCRETE quarters, so there is no comparable figure: return
        empty so the row is reported MISSING (never a false discrete match against
        a cumulative H1/FY value). The semi-annual figures remain available via
        semiannual_composition() for aggregate spot-checks."""
        return {}


# ---- Taiwan segment/geo: MOPS financial-report book (PDF notes) ------------ #
# TW segment & geographic revenue is disclosed only in the notes to the financial
# statements — the 營業收入 disaggregation (地區別, revenue by region) and the
# 部門資訊 note (來自外部客戶收入, external-customer revenue by reportable segment).
# Neither the TWSE/FinMind statement APIs nor the MOPS t164 XBRL-derived HTML view
# carry the note; it lives only in the PDF financial-report book (財務報告書) on the
# TWSE document server. We download the consolidated IFRS book (…_AI1.pdf) and
# parse the note tables from its text layer.
#
# Revenue only (op-income by segment/region is not consistently disclosed — same
# as Korea, and skipped by request). Interim books carry a discrete 3-month column
# (地區別) so Q1–Q3 are read directly; Q4 = full-year − 9-month. Segment tables give
# a discrete 3-month table per quarter, so Q4 = full-year − (Q1+Q2+Q3).
class MopsTwSource(Source):
    market = "tw"
    DOC = "https://doc.twse.com.tw/server-java/t57sb01"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120 Safari/537.36")
    _NUMTOK = re.compile(r"\(?\$?\s?-?[\d,]+\)?")
    _Q_START = {1: 1, 2: 4, 3: 7, 4: 10}
    _DISC_RANGE = {1: (1, 3), 2: (4, 6), 3: (7, 9)}

    def __init__(self):
        try:
            import pdfplumber  # noqa: F401
            self.available = True
            self.note = ""
        except ImportError:
            self.available = False
            self.note = "pdfplumber not installed (pip install -r requirements.txt)"

    # -- download the consolidated book and cache only the two note texts -- #
    def _pdf_bytes(self, coid, gy, gq):
        fn = f"{gy}{gq:02d}_{coid}_AI1.pdf"
        try:
            r = SESSION.post(self.DOC,
                             data={"step": "9", "kind": "A", "co_id": coid,
                                   "filename": fn},
                             headers={"User-Agent": self.UA, "Referer": self.DOC},
                             timeout=90)
            m = re.search(r"/pdf/[0-9A-Za-z_]+\.pdf", r.text)
            if not m:
                return None
            p = SESSION.get("https://doc.twse.com.tw" + m.group(0),
                            headers={"User-Agent": self.UA, "Referer": self.DOC},
                            timeout=180)
            if p.status_code != 200 or p.content[:4] != b"%PDF":
                return None
            return p.content
        except Exception:
            return None

    def _anchor_ok(self, kind, tn):
        if kind == "geo":
            return ("地區別" in tn
                    and ("美國" in tn or "台灣" in tn or "臺灣" in tn))
        return "來自外部客戶收入" in tn                # kind == "seg"

    def _note_text(self, coid, gy, gq, kind):
        """The 地區別 (kind='geo') or 部門資訊 (kind='seg') note text of this
        company's consolidated book, or None. Extracts page-by-page and stops at
        the note (they sit deep in the book, so this avoids parsing every page).
        Caches the extracted slice only (the book itself is several MB)."""
        ck = f"mops_tw_note_{kind}_{coid}_{gy}{gq:02d}"
        c = _cache_get(ck)
        if c is not None:
            return c or None            # a cached miss is stored as ""
        data = self._pdf_bytes(coid, gy, gq)
        if not data:
            _cache_put(ck, "")
            return None
        import pdfplumber
        found = ""
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = pdf.pages
                prev = ""
                for i in range(len(pages)):
                    t = pages[i].extract_text() or ""
                    if self._anchor_ok(kind, re.sub(r"\s", "", t)):
                        nxt = (pages[i + 1].extract_text() or ""
                               if i + 1 < len(pages) else "")
                        found = prev + "\n" + t + "\n" + nxt
                        break
                    prev = t
        except Exception:
            pass
        _cache_put(ck, found)
        return found or None

    @staticmethod
    def _tw_mult(text):
        """NT$ per reported unit for a MOPS book note. The consolidated financial-
        report book is filed in thousands (仟元) by regulation; a note only rarely
        restates the unit, and the per-page slice may omit the '(單位：新台幣仟元)'
        header entirely — so default to thousands rather than to 1 (which
        _unit_multiplier does), and only override when a note explicitly prints a
        millions unit."""
        if "佰萬元" in text or "百萬元" in text:
            return 1e6
        return 1e3

    @staticmethod
    def _num(tok):
        s = tok.replace(",", "").replace("$", "").strip()
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        try:
            v = float(s)
        except ValueError:
            return None
        return -v if neg else v

    def _nums(self, line):
        return [v for v in (self._num(t) for t in self._NUMTOK.findall(line)
                            if any(c.isdigit() for c in t)) if v is not None]

    @staticmethod
    def _row_region(line):
        label = re.sub(r"[^一-鿿A-Za-z、]", "", line)
        return _canon_region(label) if label else None

    # ---- geographic (地區別) ---- #
    def _geo_table(self, coid, gy, q):
        """{region_canon: {'discrete'|'ytd'|'fullyear': value_NT$}} for the
        report covering calendar-year gy quarter q, or None. Values already
        multiplied by the note's unit (仟元 → 1e3). Rejects the parse if the
        region rows don't sum to the printed total (guards against misreads)."""
        text = self._note_text(coid, gy, q, "geo")
        if not text:
            return None
        lines = text.split("\n")
        hdr_k = prev = None
        for k, ln in enumerate(lines):
            if re.sub(r"\s", "", ln).find("地區別") < 0:
                continue
            regions = {self._row_region(w) for w in lines[k + 1:k + 12]}
            if len(regions & _SPECIFIC_REGIONS) >= 2:
                hdr_k, prev = k, (lines[k - 1] if k else "")
                break
        if hdr_k is None:
            return None
        mult = self._tw_mult(text)
        if "年度" in lines[hdr_k]:                       # annual book: full year
            cols = {"fullyear": 0}
        else:                                            # interim: pick columns
            starts = [(int(y), int(m)) for y, m in
                      re.findall(r"(\d+)年(\d+)月\d+日", prev)]
            if not starts:
                return None
            cy = max(y for y, _ in starts)
            disc = next((i for i, (y, m) in enumerate(starts)
                         if y == cy and m == self._Q_START[q]), None)
            ytd = next((i for i, (y, m) in enumerate(starts)
                        if y == cy and m == 1), None)
            cols = {"discrete": disc, "ytd": ytd}
        rows, total = {}, None
        for ln in lines[hdr_k + 1:]:
            reg = self._row_region(ln)
            nums = self._nums(ln)
            if reg in _SPECIFIC_REGIONS or reg == "OTHER":
                if nums:
                    rows[reg] = nums
            elif reg is None and nums and rows:          # the $-total line
                total = nums
                break
            elif reg is None and not nums:
                continue
            elif rows:
                break
        if len(rows) < 2 or total is None:
            return None
        for role, idx in cols.items():                   # consistency guard
            if idx is None or idx >= len(total):
                continue
            s = sum(r[idx] for r in rows.values() if idx < len(r))
            if abs(s - total[idx]) > max(1.0, abs(total[idx]) * 1e-4):
                return None
        out = {}
        for reg, nums in rows.items():
            out[reg] = {role: nums[idx] * mult
                        for role, idx in cols.items()
                        if idx is not None and idx < len(nums)}
        return out

    # ---- business segment (部門資訊 → 來自外部客戶收入) ---- #
    def _seg_table(self, coid, gy, q):
        """{'fullyear'|'discrete': {seg_header: value_NT$}, 'total': value} for the
        external-customer revenue row of the report, or None. Interim books show a
        discrete 3-month table; annual books a full-year table."""
        text = self._note_text(coid, gy, q, "seg")
        if not text:
            return None
        lines = text.split("\n")
        mult = self._tw_mult(text)
        annual = (q == 4)
        # find the period header, then the segment-name header, then the
        # 來自外部客戶收入 row directly under it
        role = "fullyear" if annual else "discrete"
        want_range = None if annual else self._DISC_RANGE[q]
        for k, ln in enumerate(lines):
            lnn = re.sub(r"\s", "", ln)
            if annual:
                if not re.search(r"\d+年度", lnn):
                    continue
            else:
                m = re.search(r"\d+年(\d+)月至(\d+)月", lnn)
                if not m or (int(m.group(1)), int(m.group(2))) != want_range:
                    continue
            hdr = lines[k + 1] if k + 1 < len(lines) else ""
            row = None
            for j in range(k + 1, min(k + 6, len(lines))):
                if "來自外部客戶收入" in re.sub(r"\s", "", lines[j]):
                    row = lines[j]
                    break
            if row is None:
                continue
            seg_headers = [t for t in hdr.split()
                           if not any(kw in t for kw in
                                      ("調整", "沖銷", "調節", "沖轉", "合", "計", "總"))]
            vals = self._nums(row)
            if len(vals) < 2 or not seg_headers:
                continue
            total = vals[-1]
            seg_vals = vals[:-1]                          # pre-total columns
            if len(seg_vals) != len(seg_headers):
                continue                                  # can't align → skip
            if abs(sum(seg_vals) - total) > max(1.0, abs(total) * 1e-4):
                continue                                  # inconsistent → skip
            return {role: {h: v * mult for h, v in zip(seg_headers, seg_vals)},
                    "total": total * mult}
        return None

    def segment_quarterly(self, api_id, fye_month, years, label, want, is_geo):
        """Discrete-quarter {(cal_y, cal_q): value_NT$} for a Taiwanese GEOGRAPHIC
        region (地區別) or business-SEGMENT (部門資訊) revenue label. Revenue only.
        Geographic labels are auto-matched by region name; business-segment labels
        must be mapped to the note's Chinese 部門 name in segment_members.csv."""
        if not self.available or want == "opincome":
            return {}
        coid = api_id.strip()
        region = _canon_region(label) if is_geo else None
        seg_target = None if is_geo else _seg_norm(label)
        yrs = (sorted(set(int(y) for y in years)) if years
               else range(2018, datetime.date.today().year + 1))
        out = {}
        for year in yrs:
            for q in (1, 2, 3, 4):
                try:
                    v = (self._geo_quarter(coid, year, q, region) if is_geo
                         else self._seg_quarter(coid, year, q, seg_target))
                except Exception:
                    v = None
                if v is not None:
                    out[(year, q)] = v
        return out

    def all_labels(self, coid, years, is_geo):
        """Region canons (地區別) or segment header names (部門資訊) this company
        actually discloses, for enumerating a reference. Scans Q1–Q3 tables."""
        labels = set()
        for y in years:
            for q in (1, 2, 3):
                try:
                    if is_geo:
                        t = self._geo_table(coid, y, q)
                        if t:
                            labels |= set(t.keys())
                    else:
                        t = self._seg_table(coid, y, q)
                        if t:
                            labels |= set((t.get("discrete") or {}).keys())
                except Exception:
                    pass
        return labels

    def _geo_quarter(self, coid, year, q, region):
        if q in (1, 2, 3):
            t = self._geo_table(coid, year, q)
            return t.get(region, {}).get("discrete") if t else None
        fy = self._geo_table(coid, year, 4)               # annual book
        yt = self._geo_table(coid, year, 3)               # 9-month cumulative
        if not fy or not yt:
            return None
        a = fy.get(region, {}).get("fullyear")
        c = yt.get(region, {}).get("ytd")
        return (a - c) if (a is not None and c is not None) else None

    def _seg_quarter(self, coid, year, q, seg_target):
        def pick(tbl):
            if not tbl:
                return None
            body = tbl.get("fullyear") or tbl.get("discrete") or {}
            for h, v in body.items():
                if seg_target and seg_target in _seg_norm(h):
                    return v
            return None
        if q in (1, 2, 3):
            return pick(self._seg_table(coid, year, q))
        fy = pick(self._seg_table(coid, year, 4))
        if fy is None:
            return None
        parts = [pick(self._seg_table(coid, year, i)) for i in (1, 2, 3)]
        if any(p is None for p in parts):
            return None
        return fy - sum(parts)


# ---- Japan: EDINET (annual + quarterly securities reports, XBRL) ---------- #
def _month_last_day(y, m):
    nxt = datetime.date(y + (m == 12), (m % 12) + 1, 1)
    return nxt - datetime.timedelta(days=1)


class EdinetSource(Source):
    market = "jp"
    # canonical -> XBRL element id(s), tried in order. Read at CurrentYTDDuration
    # (quarterly report) or CurrentYearDuration (annual securities report); YTD
    # values de-cumulated. Each metric lists the Japanese-GAAP element (jppfs_cor)
    # AND the IFRS element (jpigp_cor, suffixed …IFRS) because a filer uses one
    # accounting standard or the other — big manufacturers (Toyota, AGC, Panasonic,
    # …) file IFRS, so the JGAAP-only name alone silently missed them. Only
    # absolute-yen flow metrics: their YTD values de-cumulate exactly and are
    # immune to share-count changes. EPS is deliberately excluded because YTD EPS
    # is restated across stock splits (Japan EPS comes from J-Quants instead).
    metric_map = {
        # flow (income statement): read at *Duration contexts, de-cumulated
        "REVENUE": ["jppfs_cor:NetSales", "jpigp_cor:NetSalesIFRS",
                    "jpigp_cor:RevenueIFRS", "jpigp_cor:Revenue2IFRS"],
        "COGS": ["jppfs_cor:CostOfSales", "jpigp_cor:CostOfSalesIFRS"],
        "GROSS_PROFIT": ["jppfs_cor:GrossProfit", "jpigp_cor:GrossProfitIFRS"],
        "OPERATING_INCOME": ["jppfs_cor:OperatingIncome",
                             "jpigp_cor:OperatingProfitLossIFRS"],
        "PRE_TAX_INCOME": ["jppfs_cor:IncomeBeforeIncomeTaxes",
                           "jpigp_cor:ProfitLossBeforeTaxIFRS"],
        "NET_INCOME": ["jppfs_cor:ProfitLossAttributableToOwnersOfParent",
                       "jpigp_cor:ProfitLossAttributableToOwnersOfParentIFRS"],
        "TAX_EXPENSE": ["jppfs_cor:IncomeTaxes", "jpigp_cor:IncomeTaxExpenseIFRS"],
        "NET_INCOME_INC_NCI": ["jppfs_cor:ProfitLoss", "jpigp_cor:ProfitLossIFRS"],
        "SGA_EXPENSE": ["jppfs_cor:SellingGeneralAndAdministrativeExpenses",
                        "jpigp_cor:SellingGeneralAndAdministrativeExpensesIFRS"],
        # stock (balance sheet): read at *Instant contexts, point-in-time
        "ACCOUNTS_PAYABLE": ["jppfs_cor:AccountsPayableTrade",
                             "jpigp_cor:TradeAndOtherPayablesCLIFRS",
                             "jpigp_cor:TradePayablesCLIFRS"],
        "CURRENT_ASSETS": ["jppfs_cor:CurrentAssets", "jpigp_cor:CurrentAssetsIFRS"],
        "TOTAL_ASSETS": ["jppfs_cor:Assets", "jpigp_cor:AssetsIFRS"],
        "CURRENT_LIABILITIES": ["jppfs_cor:CurrentLiabilities",
                                "jpigp_cor:CurrentLiabilitiesIFRS"],
        "TOTAL_LIABILITIES": ["jppfs_cor:Liabilities", "jpigp_cor:LiabilitiesIFRS"],
        "TOTAL_EQUITY": ["jppfs_cor:NetAssets", "jpigp_cor:EquityIFRS"],
        "ACCOUNTS_RECEIVABLE": ["jppfs_cor:NotesAndAccountsReceivableTrade",
                                "jpigp_cor:TradeAndOtherReceivablesCAIFRS",
                                "jpigp_cor:TradeReceivablesCAIFRS"],
        "CASH_AND_CASH_EQUIVALENTS": ["jppfs_cor:CashAndDeposits",
                                      "jpigp_cor:CashAndCashEquivalentsIFRS"],
        "INVENTORIES": ["jppfs_cor:Inventories", "jpigp_cor:InventoriesCAIFRS"],
        # JGAAP filers break inventory out on the face BS; IFRS filers usually
        # don't, so those land on MISSING (aggregate INVENTORIES still resolves).
        "FINISHED_GOODS": ["jppfs_cor:FinishedGoods",
                           "jppfs_cor:MerchandiseAndFinishedGoods",
                           "jppfs_cor:Merchandise"],
        "RAW_MATERIALS": ["jppfs_cor:RawMaterials",
                          "jppfs_cor:RawMaterialsAndSupplies",
                          "jppfs_cor:MerchandiseAndRawMaterials"],
        "WORK_IN_PROCESS": ["jppfs_cor:WorkInProcess",
                            "jppfs_cor:WorkInProcessAndSemifinishedGoods"],
        "PROPERTY_PLANT_AND_EQUIPMENT": ["jppfs_cor:PropertyPlantAndEquipment",
                                         "jpigp_cor:PropertyPlantAndEquipmentIFRS"],
        # parent-owners' equity (excludes NCI), vs TOTAL_EQUITY = NetAssets/Equity.
        "SHAREHOLDERS_EQUITY": ["jppfs_cor:ShareholdersEquity",
                                "jpigp_cor:EquityAttributableToOwnersOfParentIFRS"],
        "NON_CONTROL_INTEREST": ["jppfs_cor:NonControllingInterests",
                                 "jpigp_cor:NonControllingInterestsIFRS"],
        "CONTRACT_LIABILITIES": ["jppfs_cor:ContractLiabilities",
                                 "jpigp_cor:ContractLiabilitiesCLIFRS",
                                 "jpigp_cor:ContractLiabilitiesIFRS"],
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

    # R&D expense INCLUDED IN SG&A (JGAAP element; 一般管理費に含まれる研究開発費). The
    # user's SG&A excludes R&D, but Japanese 販管費 includes it — so where this line
    # is disclosed we subtract it. EDINET discloses it only in the annual (and some
    # half-year) securities report, never reliably per quarter, so most discrete
    # quarters can't be R&D-excluded; those are dropped (reported no value) rather
    # than returned with R&D still in — per the user's "no value over wrong value".
    SGA_RD_ELEMENTS = ("ResearchAndDevelopmentExpensesSGA",)

    def _flow_element(self, doc, localname_suffixes, is_annual):
        """Value of the first consolidated fact whose element local-name ends with one
        of `localname_suffixes`, at the flow (YTD / full-year) context — or None if
        the report doesn't tag it."""
        text = self._csv_text(doc)
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        hdr = rows[0]
        eid, ctxi, vali = hdr.index("要素ID"), hdr.index("コンテキストID"), hdr.index("値")
        ctx = "CurrentYearDuration" if is_annual else "CurrentYTDDuration"
        for r in rows[1:]:
            if len(r) <= vali or r[ctxi] != ctx:   # exact ctx excludes NonConsolidated
                continue
            ln = r[eid].split(":")[-1]
            if any(ln.endswith(s) for s in localname_suffixes):
                try:
                    return float(r[vali])
                except ValueError:
                    return None
        return None

    def _report_value(self, doc, metric, is_annual):
        text = self._csv_text(doc)
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        hdr = rows[0]
        eid, ctxi, vali = hdr.index("要素ID"), hdr.index("コンテキストID"), hdr.index("値")
        elt = self.metric_map[metric]
        cands = elt if isinstance(elt, list) else [elt]   # JGAAP + IFRS variants
        if CANONICAL[metric]["kind"] == "stock":   # balance sheet: point-in-time
            ctx = "CurrentYearInstant" if is_annual else "CurrentQuarterInstant"
        else:                                       # income statement: period flow
            ctx = "CurrentYearDuration" if is_annual else "CurrentYTDDuration"
        # index the consolidated facts for this context, then take the first
        # candidate present (a filer reports under one standard, so only its
        # JGAAP or its IFRS element exists — no ambiguity between the two).
        vals = {}
        for r in rows[1:]:
            if r[ctxi] == ctx and "NonConsolidated" not in r[ctxi]:
                vals.setdefault(r[eid], r[vali])
        for c in cands:
            if c in vals:
                try:
                    return float(vals[c])
                except ValueError:
                    return None
        return None

    def _prior_report_value(self, doc, metric, is_annual):
        """Value of `metric` at the report's PRIOR-year comparative context —
        Prior1YearDuration (annual) / Prior1YTDDuration (quarterly YTD) for flows.
        This is the period one fiscal year before `doc`'s own, as re-presented a
        year later; comparing it with that period's as-originally-filed figure
        surfaces restatements. Mirrors _report_value on the Prior1 context. Returns
        None if the report doesn't tag the prior column for this element. Flow only
        — a quarterly balance sheet's prior column is the prior fiscal YEAR-END, not
        the prior same-quarter instant (Prior1QuarterInstant is absent), so stock
        vintages are not extractable this way and are left to quarterly()."""
        text = self._csv_text(doc)
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        hdr = rows[0]
        eid, ctxi, vali = hdr.index("要素ID"), hdr.index("コンテキストID"), hdr.index("値")
        elt = self.metric_map[metric]
        cands = elt if isinstance(elt, list) else [elt]
        ctx = "Prior1YearDuration" if is_annual else "Prior1YTDDuration"
        vals = {}
        for r in rows[1:]:
            if r[ctxi] == ctx and "NonConsolidated" not in r[ctxi]:
                vals.setdefault(r[eid], r[vali])
        for c in cands:
            if c in vals:
                try:
                    return float(vals[c])
                except ValueError:
                    return None
        return None

    def frames(self, api_id, metric, fye_month=12, years=None):
        """Prior-year-comparative vintage of each period. Every EDINET securities/
        quarterly report carries the previous fiscal year as a 前期 ("Prior1")
        column — that period's figure as re-presented one year later. Where the
        filer restated it, this differs from the period's own as-filed value
        (quarterly()'s primary), so both vintages surface and a restatement can't be
        silently overwritten.

        Approach: for each report in the requested years (and the year after, whose
        prior column covers the last requested year), read the Prior1 flow context
        (Prior1YearDuration / Prior1YTDDuration), key it to the period one fiscal
        year earlier, then de-cumulate that prior-year YTD ladder into discrete
        quarters exactly as quarterly() does the current year.

        Limits: FLOW metrics only — a quarterly balance sheet's comparative is the
        prior fiscal year-end (not the prior same quarter), so stock vintages aren't
        reliably locatable and return {}. SGA_EXPENSE is skipped: its user-
        convention value subtracts R&D-in-SG&A, a split the prior column doesn't
        expose, so it can't be reproduced without risking an R&D-inclusive (wrong)
        figure. EPS is excluded as in quarterly(). Post-Apr-2024 EDINET dropped
        quarterly (四半期) reports, so a fiscal year with only an annual filing
        cannot de-cumulate its prior ladder and yields nothing there. Nothing is
        emitted where the prior context/element is absent — never a guessed value.
        Returns {(cal_y, cal_q): [restated_value]} for the requested years."""
        if not self.available:
            return {}
        if metric not in self.metric_map or CANONICAL[metric]["kind"] == "stock":
            return {}
        if metric == "SGA_EXPENSE":
            return {}
        sec5 = api_id.strip()
        if len(sec5) == 4:
            sec5 += "0"
        yrs = (sorted(set(int(y) for y in years)) if years
               else list(range(2018, datetime.date.today().year + 1)))
        # a period in year Y is the prior-year column of the report whose OWN
        # period-end is in year Y+1, so discover those reports too.
        disc_years = sorted(set(yrs) | {y + 1 for y in yrs})
        docs = self._discover(sec5, fye_month, disc_years)
        # read each report's prior-year YTD, keyed by the PRIOR period-end (same
        # month, one year earlier), forming the prior fiscal year's YTD ladder.
        prior_ytd = {}
        for pe_iso, (doc, is_annual) in docs.items():
            try:
                v = self._prior_report_value(doc, metric, is_annual)
            except Exception:
                v = None
            if v is None:
                continue
            pe = datetime.date.fromisoformat(pe_iso)
            prior_pe = _month_last_day(pe.year - 1, pe.month)
            prior_ytd[prior_pe.isoformat()] = v
        if not prior_ytd:
            return {}
        # de-cumulate exactly like quarterly(): group by (prior) fiscal year,
        # index quarters, subtract the preceding YTD rung.
        from collections import defaultdict
        groups = defaultdict(dict)
        for pe_iso, v in prior_ytd.items():
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
                    continue                 # missing prior quarter -> can't derive
                k = cal_key_from_date(pe.isoformat())
                if k[0] in yrs:              # restrict to the requested years
                    out[k] = [disc]
        return out

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
            if v is None:
                continue
            if metric == "SGA_EXPENSE":
                # exclude R&D to match the user's convention. Only a report that
                # actually discloses R&D-in-SG&A yields a usable YTD point; one that
                # doesn't is skipped, so the discrete quarter is reported as no value
                # rather than R&D-inclusive (which would be wrong for the user).
                rd = self._flow_element(doc, self.SGA_RD_ELEMENTS, is_annual)
                if rd is None:
                    continue
                v = v - rd
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


def resolve_data_file(data_dir: Path, fname: str) -> Path:
    """Locate a data file, tolerating the `.csv` suffix being present or absent
    (the user's Seg_* files are named without an extension). Returns the path that
    exists, else the `.csv` form for a clean 'not found' message."""
    stem = fname[:-4] if fname.endswith(".csv") else fname
    for cand in (data_dir / fname, data_dir / stem, data_dir / (stem + ".csv")):
        if cand.exists():
            return cand
    return data_dir / fname


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


def compare_multi(file_val, api_locals, per_share):
    """Compare the file value against several candidate API values — the distinct
    vintages a source reports for one period (EDGAR as-filed vs restated). MATCH
    if the file agrees with ANY vintage (each is a figure the company actually
    filed, so agreeing with either is correct); otherwise MISMATCH reported
    against the primary (first) candidate, which is quarterly()'s latest frame.
    Returns (status, matched_local, api_millions, matched_flag)."""
    fallback = None
    for a in api_locals:
        status, api_m = compare(file_val, a, per_share)
        if status == "MATCH":
            return "MATCH", a, api_m, True
        if fallback is None:
            fallback = (status, a, api_m)
    status, a, api_m = fallback
    return status, a, api_m, False


def _vfmt(x, per_share):
    """Format one vintage value the way the note/columns do: raw for per-share,
    millions (full local currency / 1e6) for money."""
    return f"{x:.4f}" if per_share else f"{x / 1e6:.3f}"


def vintage_columns(cand, primary, matched_local, matched, per_share):
    """Transparency columns for one statement row. Returns
    (api_vintages, vintage_match):
      api_vintages  — every distinct candidate vintage, formatted like the note
                      ("1445.000 / 1392.000"); a lone value is just that value.
      vintage_match — "latest" if the file matched the primary/latest value,
                      "superseded" if it matched an older value the company has
                      since restated, "none" on MISMATCH, "" when not applicable
                      (per-share, or a single vintage with nothing to supersede).
    Superseded == matched a non-latest: matched and round(matched_local,2) !=
    round(primary,2)."""
    api_vintages = " / ".join(_vfmt(c, per_share) for c in cand)
    if per_share:
        return api_vintages, ""
    if not matched:
        return api_vintages, "none"
    if len(cand) <= 1:
        return api_vintages, ""
    return api_vintages, ("superseded"
                          if round(matched_local, 2) != round(primary, 2)
                          else "latest")


def export_files(export_dir: Path, results: list, files_map: dict):
    """Write the API-fetched values back out in the user's own CSV schema (one file
    per input file, ';'-delimited), so they can diff it against their originals.
    `financial_report_value` holds the as-filed API value (millions of local
    currency; per-share direct); `financial_value` is left blank (their FX-to-USD
    column can't be reproduced without their conversion method)."""
    export_dir.mkdir(parents=True, exist_ok=True)
    FA_COLS = ["company_id", "fiscal_year", "fiscal_quarter", "calendar_year",
               "calendar_quarter", "financial_code", "financial_value",
               "financial_report_value"]
    SEG_COLS = ["company_id", "calendar_year", "calendar_quarter", "segment_code",
                "financial_code", "financial_value", "financial_report_value"]
    for logical, fname in files_map.items():
        rows = [r for r in results if r["file"] == logical]
        if not rows:
            continue
        cols = SEG_COLS if logical in SEG_FILES else FA_COLS
        out_name = fname if fname.endswith(".csv") else fname + ".csv"
        with open(export_dir / out_name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
            w.writeheader()
            for r in rows:
                val = r.get("api_value_millions")
                if val == "" or val is None:      # per-share metrics carry local
                    val = r.get("api_value_local", "")
                w.writerow({**{c: "" for c in cols},
                            **{c: r.get(c, "") for c in cols if c in r},
                            "financial_value": "",
                            "financial_report_value": val})


# common region canons to probe for markets whose geo auto-matches by name (KR/TW)
_GEO_PROBE = ["US", "CN", "JP", "TW", "KR", "DE", "EU", "EMEA", "ASIA", "NA",
              "LATAM", "HK", "SG", "IN", "VN", "ME", "AF", "OCEANIA", "OTHER"]


def dump_segments(ref: Path, items, sources, edgar_dim, mops_tw, seg_members, yset):
    """Enumerate disclosed geographic + business-segment revenue for each company
    and stream to reference/Seg_Geo_Revenue.csv and reference/Seg_Seg_Revenue.csv.
    US: all XBRL members on the geo/segment axes. TW: all 地區別 regions / 部門資訊
    segments from the PDF book. KR: geographic regions by name-probe. JP and KR
    business segments need per-company member names, so they're covered by the
    row-driven verify path rather than enumerated here (noted in the console)."""
    cols = ["company_id", "calendar_year", "calendar_quarter", "segment_code",
            "financial_code", "financial_value", "financial_report_value"]
    geo_f = open(ref / "Seg_Geo_Revenue.csv", "w", newline="", encoding="utf-8-sig")
    seg_f = open(ref / "Seg_Seg_Revenue.csv", "w", newline="", encoding="utf-8-sig")
    geo_w = csv.DictWriter(geo_f, fieldnames=cols, delimiter=";"); geo_w.writeheader()
    seg_w = csv.DictWriter(seg_f, fieldnames=cols, delimiter=";"); seg_w.writeheader()

    def emit(writer, cid, ser, label, code):
        for (y, q), v in sorted(ser.items()):
            if y in yset:
                writer.writerow({"company_id": cid, "calendar_year": y,
                                 "calendar_quarter": q,
                                 "segment_code": f"{y}Q{q}_{label}",
                                 "financial_code": code, "financial_value": "",
                                 "financial_report_value": round(v / 1e6, 4)})

    for cid, comp in items:
        mk, api, fye = comp["market"], comp["api_id"], comp["fye_month"]
        if mk == "cn":
            continue                       # China seg/geo is semi-annual only
        print(f"  seg/geo {cid} {mk}:{api} {comp['name'][:28]}", flush=True)
        try:
            if mk == "us":
                cik = sources["us"]._resolve_cik(api)
                if not cik:
                    continue
                for member in edgar_dim.members(cik, "Geograph", yset):
                    emit(geo_w, cid, edgar_dim.series(
                        cik, EdgarDimensional.REV_TAGS, "Geograph", member, yset),
                        member, "GEO_REVENUE")
                for member in edgar_dim.members(cik, "Segment", yset):
                    emit(seg_w, cid, edgar_dim.series(
                        cik, EdgarDimensional.REV_TAGS, "Segment", member, yset),
                        member, "SEG_REVENUE")
            elif mk == "tw":
                for region in mops_tw.all_labels(api, sorted(yset), is_geo=True):
                    emit(geo_w, cid, mops_tw.segment_quarterly(
                        api, fye, yset, region, "revenue", True), region, "GEO_REVENUE")
                for seg in mops_tw.all_labels(api, sorted(yset), is_geo=False):
                    emit(seg_w, cid, mops_tw.segment_quarterly(
                        api, fye, yset, seg, "revenue", False), seg, "SEG_REVENUE")
            elif mk == "kr":
                for region in _GEO_PROBE:
                    ser = sources["kr"].segment_quarterly(
                        api, fye, yset, region, "revenue", True)
                    if ser:
                        emit(geo_w, cid, ser, region, "GEO_REVENUE")
            # jp: reportable-segment members are per-company; use the verify path
        except Exception as e:
            print(f"     ! {str(e)[:70]}", flush=True)
        geo_f.flush(); seg_f.flush()
    geo_f.close(); seg_f.close()
    print(f"\nSegment/geo reference -> {ref/'Seg_Geo_Revenue.csv'}, "
          f"{ref/'Seg_Seg_Revenue.csv'}", flush=True)


def dump_reference(out_dir: Path, registry: dict, years, include_seg: bool = False,
                   seg_members: dict = None):
    """Pull a STANDALONE reference of the as-filed figures for every configured
    company (independent of any input file) and write it in the user's own CSV
    schema, so a file can be diffed/fuzzy-matched against it. Enumerates every
    statement metric each market's source supports, across `years`; with
    include_seg, also enumerates disclosed geographic + business-segment revenue
    (US/JP/KR/TW; China excluded — semi-annual only). financial_value (FX->USD) is
    left blank. Progress is printed per company; results stream to disk so a long
    (hours) run leaves partial output if interrupted."""
    import sys
    sources = {s.market: s for s in
               (EdgarSource(), OpenDartSource(), FinMindSource(), JapanSource(),
                AKShareSource())}
    edgar_dim = EdgarDimensional(sources["us"])
    mops_tw = MopsTwSource()
    seg_members = seg_members or {}
    ref = out_dir / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    yset = set(int(y) for y in years)

    FA_COLS = ["company_id", "fiscal_year", "fiscal_quarter", "calendar_year",
               "calendar_quarter", "financial_code", "financial_value",
               "financial_report_value"]
    fa = open(ref / "FA.csv", "w", newline="", encoding="utf-8-sig")
    faw = csv.DictWriter(fa, fieldnames=FA_COLS, delimiter=";"); faw.writeheader()

    items = sorted(registry.items(),
                   key=lambda kv: ({"us": 0, "tw": 1, "kr": 2, "cn": 3, "jp": 4}
                                   .get(kv[1]["market"], 9), kv[0]))
    n = 0
    for cid, comp in items:
        src = sources.get(comp["market"])
        if not src or not getattr(src, "available", False):
            continue
        n += 1
        print(f"[{n}] statements {cid} {comp['market']}:{comp['api_id']} "
              f"{comp['name'][:32]}", flush=True)
        for metric in sorted(getattr(src, "metric_map", {})):
            if metric not in CANONICAL:
                continue
            try:
                series = src.quarterly(comp["api_id"], metric, comp["fye_month"],
                                       years=yset)
            except Exception as e:
                print(f"     ! {metric}: {str(e)[:60]}", flush=True); continue
            ps = CANONICAL[metric]["per_share"]
            sign = -1 if metric in SIGN_FLIP_METRICS else 1   # negative-CAPEX convention
            for (y, q), v in sorted(series.items()):
                if y not in yset:
                    continue
                v *= sign
                faw.writerow({"company_id": cid, "fiscal_year": "",
                              "fiscal_quarter": "", "calendar_year": y,
                              "calendar_quarter": q, "financial_code": metric,
                              "financial_value": "",
                              "financial_report_value": round(v if ps else v / 1e6, 4)})
        fa.flush()
    fa.close()
    print(f"\nStatements reference -> {ref/'FA.csv'}", flush=True)

    if include_seg:
        dump_segments(ref, [kv for kv in items], sources, edgar_dim, mops_tw,
                      seg_members, yset)


def _mismatch_pct_diff(rec):
    """Percentage difference (file_value - api_value) / api_value * 100 for one result
    row, or None if either value is unparseable or api_value is 0 (no meaningful base).
    Money metrics use millions (file_value vs api_value_millions); per-share metrics
    leave api_value_millions blank, so api_value_local (already per-share) is used —
    the ratio is unit-free either way."""
    try:
        fv = float(str(rec.get("file_value", "")).replace(",", ""))
    except (TypeError, ValueError):
        return None
    api = rec.get("api_value_millions", "")
    if api == "" or api is None:
        api = rec.get("api_value_local", "")
    try:
        av = float(str(api).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if av == 0:
        return None
    return (fv - av) / av * 100.0


def mismatch_code_summary(mism, out_dir):
    """Per-financial_code breakdown of the MISMATCH rows: how many times each unique
    financial_code shows up as a mismatch, and the average percentage difference
    (file_value - api_value) / api_value across those rows. Rows whose values can't be
    parsed (or whose api_value is 0) still count toward the tally but are skipped from
    the average (n_with_diff = how many rows the average is over). Writes
    mismatch_code_summary.csv; returns (rows, path)."""
    code_stats = {}
    for r in mism:
        st = code_stats.setdefault(r.get("financial_code", ""),
                                   {"count": 0, "diffs": []})
        st["count"] += 1
        d = _mismatch_pct_diff(r)
        if d is not None:
            st["diffs"].append(d)
    code_rows = []
    for code in sorted(code_stats, key=lambda c: (-code_stats[c]["count"], c)):
        st = code_stats[code]
        diffs = st["diffs"]
        avg = sum(diffs) / len(diffs) if diffs else ""
        code_rows.append({"financial_code": code, "mismatch_count": st["count"],
                          "n_with_diff": len(diffs),
                          "avg_pct_difference": round(avg, 3) if diffs else ""})
    codesum_csv = out_dir / "mismatch_code_summary.csv"
    with open(codesum_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["financial_code", "mismatch_count",
                                          "n_with_diff", "avg_pct_difference"])
        w.writeheader()
        for cr in code_rows:
            w.writerow(cr)
    return code_rows, codesum_csv


def print_mismatch_code_summary(code_rows):
    """Console rendering of the per-financial_code mismatch breakdown."""
    if not code_rows:
        return
    print("\n=== MISMATCHES by financial_code (count + avg file-vs-api % diff) ===")
    print("  (avg_pct_difference = mean((file_value - api_value) / api_value * 100))")
    print(f"  {'financial_code':32} {'count':>6} {'avg_pct_difference':>20}")
    for cr in code_rows:
        avg = cr["avg_pct_difference"]
        avg_s = f"{avg:,.3f}%" if avg != "" else "n/a"
        print(f"  {cr['financial_code']:32} {cr['mismatch_count']:>6} {avg_s:>20}")


def run(data_dir: Path, out_dir: Path, compare_col: str,
        registry: dict, metric_map: dict, files_map: dict, seg_members: dict = None,
        mapping: dict = None, export: bool = False):
    sources = {s.market: s for s in
               (EdgarSource(), OpenDartSource(), FinMindSource(), JapanSource(),
                AKShareSource())}
    edgar_dim = EdgarDimensional(sources["us"])
    mops_tw = MopsTwSource()
    seg_members = seg_members or {}
    mapping = mapping or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    unconfigured = {}   # company_id -> mapped name, for the end-of-run to-do list

    # pre-scan: which calendar years does each company appear in? (lets per-year
    # sources like OpenDART fetch only what's needed instead of all history)
    years_by_company = {}
    for logical, fname in files_map.items():
        fpath = resolve_data_file(data_dir, fname)
        if not fpath.exists():
            continue
        with open(fpath, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                cid = (row.get("company_id") or "").strip()
                cy = (row.get("calendar_year") or "").strip()
                if cid and cy.isdigit():
                    years_by_company.setdefault(cid, set()).add(int(cy))

    series_cache = {}   # (market, api_id, metric) -> {(y,q): value}
    frames_cache = {}   # (market, api_id, metric) -> {(y,q): [distinct vintages]}

    def get_series(src, comp, cid, metric):
        k = (comp["market"], comp["api_id"], metric)
        if k not in series_cache:
            s = src.quarterly(
                comp["api_id"], metric, comp["fye_month"],
                years=years_by_company.get(cid))
            if metric in SIGN_FLIP_METRICS:      # match the user's negative-CAPEX sign
                s = {kk: -vv for kk, vv in s.items()}
            series_cache[k] = s
        return series_cache[k]

    def get_frames(src, comp, cid, metric):
        """Distinct as-reported vintages per period (EDGAR as-filed vs restated);
        {} for sources that expose only one value."""
        k = (comp["market"], comp["api_id"], metric)
        if k not in frames_cache:
            try:
                fr = src.frames(
                    comp["api_id"], metric, comp["fye_month"],
                    years=years_by_company.get(cid))
                if metric in SIGN_FLIP_METRICS:
                    fr = {kk: [-vv for vv in lst] for kk, lst in fr.items()}
                frames_cache[k] = fr
            except Exception:
                frames_cache[k] = {}
        return frames_cache[k]

    for logical, fname in files_map.items():
        fpath = resolve_data_file(data_dir, fname)
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
                       "fiscal_year": (row.get("fiscal_year") or "").strip(),
                       "fiscal_quarter": (row.get("fiscal_quarter") or "").strip(),
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
                    elif market == "kr" and not is_geo and not member:
                        # KR business segments need the note's 부문 name (labels are
                        # company-specific); geographic matches by region name.
                        rec["status"] = "NO_SEGMENT_MAPPING"
                        rec["note"] = (f"map '{label}' in config/segment_members.csv "
                                       "(KR: the 영업부문 name in the note, e.g. 'DS')")
                        results.append(rec); continue
                    elif market == "tw" and not is_geo and not member:
                        # TW business segments need the note's 部門 name (labels are
                        # company-specific); geographic matches by region name.
                        rec["status"] = "NO_SEGMENT_MAPPING"
                        rec["note"] = (f"map '{label}' in config/segment_members.csv "
                                       "(TW: the 部門 name in the 部門資訊 note, "
                                       "e.g. '資通訊產品事業群')")
                        results.append(rec); continue
                    elif market == "cn" and not is_geo and not member:
                        # CN business segments need the 产品/行业 item name (geographic
                        # 境内/境外 auto-matches).
                        rec["status"] = "NO_SEGMENT_MAPPING"
                        rec["note"] = (f"map '{label}' in config/segment_members.csv "
                                       "(CN: the 产品/行业 name in 主营构成, e.g. "
                                       "'集成电路封装测试')")
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
                        elif market == "tw":
                            # TW geographic revenue: matched by region name; business
                            # segment: mapped 部門 name. Parsed from the MOPS PDF book.
                            ser = mops_tw.segment_quarterly(
                                comp["api_id"], comp["fye_month"],
                                years_by_company.get(cid), member or label, want, is_geo)
                        elif market == "cn":
                            # CN main-business composition (主营构成): geographic 境内/境外
                            # auto-matches; segment = mapped 产品/行业 name. Half-year and
                            # full-year cumulative only (Q2/Q4), never discrete quarters.
                            ser = sources["cn"].segment_quarterly(
                                comp["api_id"], comp["fye_month"],
                                years_by_company.get(cid), member or label, want, is_geo)
                        else:
                            rec["status"] = "UNSUPPORTED_SEGMENT"
                            rec["note"] = f"no segment source for market {market}"
                            results.append(rec); continue
                        api_local = ser.get((int(cy), int(cq)))
                        if api_local is None:
                            rec["status"] = "MISSING_IN_API"
                            rec["note"] = (
                                "China discloses segment/geo only semi-annually "
                                "(cumulative H1 + full-year); no discrete-quarter figure"
                                if market == "cn" else
                                "no discrete-quarter dimensional fact "
                                "(annual-only disclosure or period absent)")
                            results.append(rec); continue
                        status, api_m = compare(fv, api_local, False)
                        rec["api_value_local"] = api_local
                        rec["api_value_millions"] = round(api_m, 3)
                        rec["status"] = status
                        # single dimensional value: no restatement vintages exist,
                        # but keep the columns consistent across all rows
                        rec["api_vintages"] = _vfmt(api_local, False)
                        rec["vintage_match"] = ("latest" if status == "MATCH"
                                                else "none")
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
                if is_nonfinancial_code(code, canonical):
                    rec["status"] = "UNSUPPORTED_NONFINANCIAL"
                    rec["note"] = ("operational KPI or non-GAAP figure (headcount, "
                                   "wafer volume/ASP, utilization, backlog/bookings, "
                                   "FX, non-GAAP) — not an audited-statement line "
                                   "item, so not reconcilable against filings")
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
                    per_share = CANONICAL[canonical]["per_share"]
                    series = get_series(src, comp, cid, canonical)
                    primary = series.get(key)
                    if primary is None and comp["market"] == "jp":
                        rec["note"] = sources["jp"].note
                    if primary is None:
                        rec["status"] = "MISSING_IN_API"
                        results.append(rec); continue
                    # collect every distinct vintage this source reports for the
                    # period (EDGAR as-filed vs restated) — primary/latest first —
                    # and accept the file if it matches ANY of them.
                    cand, seen = [primary], {round(primary, 2)}
                    for v in get_frames(src, comp, cid, canonical).get(key, []):
                        r = round(v, 2)
                        if r not in seen:
                            seen.add(r); cand.append(v)
                    status, matched_local, api_m, matched = compare_multi(
                        fv, cand, per_share)
                    rec["api_value_local"] = matched_local
                    rec["api_value_millions"] = "" if per_share else round(api_m, 3)
                    rec["status"] = status
                    rec["api_vintages"], rec["vintage_match"] = vintage_columns(
                        cand, primary, matched_local, matched, per_share)
                    if len(cand) > 1:             # surface both vintages
                        if (matched and not per_share
                                and round(matched_local, 2) != round(primary, 2)):
                            # MATCH against a superseded vintage: the file is an
                            # as-filed value the company has since restated.
                            rec["note"] = (
                                f"matches as-filed {_vfmt(matched_local, per_share)}; "
                                f"restated since to {_vfmt(primary, per_share)}"
                                + ("" if per_share else " (millions)"))
                        else:
                            rec["note"] = (
                                f"{len(cand)} filed vintages for this period "
                                f"(as-filed vs restated): "
                                + " / ".join(_vfmt(c, per_share) for c in cand)
                                + ("" if per_share else " (millions)")
                                + (f"; file matches {_vfmt(matched_local, per_share)}"
                                   if matched else "; file matches none"))
                except Exception as e:
                    rec["status"] = "ERROR"; rec["note"] = str(e)[:120]
                results.append(rec)

    # write outputs
    cols = ["file", "company_id", "company_name", "calendar_year", "calendar_quarter",
            "financial_code", "segment_code", "file_value", "api_value_local",
            "api_value_millions", "api_vintages", "vintage_match", "status", "note"]
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

    code_rows, codesum_csv = mismatch_code_summary(mism, out_dir)

    # optional export: the API-fetched values written into YOUR file schema, so you
    # can diff it against your own files. financial_report_value = the as-filed value
    # (millions of local currency; per-share direct); financial_value (your FX-to-USD
    # column) is left blank — we can't reproduce your conversion. Blank where the API
    # has no value (derived code, unconfigured company, or not disclosed).
    if export:
        export_files(out_dir / "export", results, files_map)

    # console summary
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print("\n=== Summary ===")
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {st:22} {n}")
    print(f"\nFull results : {all_csv}")
    print(f"Mismatches   : {mm_csv}  ({len(mism)} rows)")
    print(f"Mismatch code summary : {codesum_csv}")
    if export:
        print(f"Export (your schema, API values): {out_dir / 'export'}/")
    print_mismatch_code_summary(code_rows)
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
# Compare against a pulled reference (offline) with fuzzy label matching
# --------------------------------------------------------------------------- #
def _norm_lbl(s):
    return re.sub(r"[\s()\.\-_/、,，&]", "", s or "").upper()


def fuzzy_match(user_label, candidates, is_geo):
    """Pick the reference row whose label best matches the user's segment/geo label.
    candidates: list of (label, value). Tries, in order: canonical region equality
    (geo only), exact normalized string, substring either way, then difflib
    similarity above a threshold. Returns (label, value, how) or None."""
    import difflib
    if not candidates:
        return None
    if is_geo:
        uc = _canon_region(user_label)
        for lab, val in candidates:
            if _canon_region(lab) == uc:
                return (lab, val, "region")
    un = _norm_lbl(user_label)
    for lab, val in candidates:
        if _norm_lbl(lab) == un:
            return (lab, val, "exact")
    for lab, val in candidates:
        ln = _norm_lbl(lab)
        if ln and un and (ln in un or un in ln):
            return (lab, val, "substring")
    best, best_r = None, 0.0
    for lab, val in candidates:
        r = difflib.SequenceMatcher(None, un, _norm_lbl(lab)).ratio()
        if r > best_r:
            best, best_r = (lab, val), r
    if best and best_r >= (0.7 if is_geo else 0.6):
        return (best[0], best[1], f"fuzzy:{best_r:.2f}")
    return None


def _strip_seg_prefix(code):
    return re.sub(r"^\d{4}Q\d_", "", code or "").strip()


def compare_against_reference(data_dir: Path, ref_dir: Path, out_dir: Path,
                              registry: dict, metric_map: dict, files_map: dict,
                              mapping: dict = None):
    """Reconcile the user's files against a previously pulled reference/ dump —
    offline, no API calls — with fuzzy matching: financial_code via metric_map,
    segment/geo labels via fuzzy_match(), values within the usual tolerances
    (reference values are already in millions / per-share)."""
    from collections import defaultdict
    mapping = mapping or {}
    out_dir.mkdir(parents=True, exist_ok=True)

    def _read(fname):
        for cand in (ref_dir / fname, ref_dir / (fname if fname.endswith(".csv")
                                                 else fname + ".csv")):
            if cand.exists():
                with open(cand, encoding="utf-8-sig", newline="") as f:
                    return list(csv.DictReader(f, delimiter=";"))
        return []

    ref_fa = {}                                   # (cid, cy, cq, canonical) -> value
    for r in _read("FA.csv"):
        try:
            ref_fa[(r["company_id"].strip(), r["calendar_year"].strip(),
                    r["calendar_quarter"].strip(), r["financial_code"].strip())] = \
                float(r["financial_report_value"])
        except (ValueError, KeyError, TypeError):
            pass
    ref_seg = {"Seg_Geo_Revenue": defaultdict(list),
               "Seg_Seg_Revenue": defaultdict(list)}
    for logical in ref_seg:
        for r in _read(logical + ".csv"):
            try:
                v = float(r["financial_report_value"])
            except (ValueError, KeyError, TypeError):
                continue
            key = (r["company_id"].strip(), r["calendar_year"].strip(),
                   r["calendar_quarter"].strip())
            ref_seg[logical][key].append(
                (_strip_seg_prefix(r.get("segment_code")), v))

    def cmp(fv, ref, per_share):
        if per_share:
            return "MATCH" if abs(fv - ref) <= EPS_ABS_TOL else "MISMATCH"
        if max(abs(fv), abs(ref)) < NEAR_ZERO:
            return "MATCH" if abs(fv - ref) < NEAR_ZERO else "MISMATCH"
        return ("MATCH" if abs(fv - ref) / max(abs(fv), abs(ref)) <= REL_TOL
                else "MISMATCH")

    results = []
    for logical, fname in files_map.items():
        fpath = resolve_data_file(data_dir, fname)
        if not fpath.exists():
            continue
        is_seg = logical in SEG_FILES
        is_geo = logical.startswith("Seg_Geo")
        seg_key = "Seg_Geo_Revenue" if is_geo else "Seg_Seg_Revenue"
        with open(fpath, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                cid = (row.get("company_id") or "").strip()
                cy = (row.get("calendar_year") or "").strip()
                cq = (row.get("calendar_quarter") or "").strip()
                code = (row.get("financial_code") or "").strip()
                rec = {"file": logical, "company_id": cid,
                       "company_name": (registry.get(cid, {}).get("name")
                                        or mapping.get(cid, "")),
                       "calendar_year": cy, "calendar_quarter": cq,
                       "financial_code": code,
                       "segment_code": (row.get("segment_code") or "").strip(),
                       "file_value": row.get("financial_report_value", ""),
                       "api_value_local": "", "api_value_millions": "",
                       "status": "", "note": ""}
                try:
                    fv = float(str(rec["file_value"]).replace(",", ""))
                except ValueError:
                    rec["status"] = "BAD_FILE_VALUE"; results.append(rec); continue

                if is_seg and logical.endswith("Operating_Income"):
                    rec["status"] = "MISSING_IN_REFERENCE"
                    rec["note"] = "reference covers revenue only"
                    results.append(rec); continue

                if is_seg:
                    label = _strip_seg_prefix(rec["segment_code"])
                    cand = ref_seg[seg_key].get((cid, cy, cq), [])
                    m = fuzzy_match(label, cand, is_geo)
                    if not m:
                        rec["status"] = "MISSING_IN_REFERENCE"
                        rec["note"] = (f"no reference label matched '{label}'"
                                       if cand else "no reference rows for period")
                        results.append(rec); continue
                    ref_lab, ref_val, how = m
                    rec["api_value_millions"] = round(ref_val, 3)
                    rec["status"] = cmp(fv, ref_val, False)
                    rec["note"] = f"matched '{ref_lab}' ({how})"
                    results.append(rec); continue

                # FA statement row
                canonical = metric_map.get(code)
                if is_derived_code(code, canonical):
                    rec["status"] = "UNSUPPORTED_DERIVED"; results.append(rec); continue
                if not canonical:
                    rec["status"] = "NO_MAPPING"; results.append(rec); continue
                ref_val = ref_fa.get((cid, cy, cq, canonical))
                if ref_val is None:
                    rec["status"] = "MISSING_IN_REFERENCE"
                    rec["note"] = f"{canonical} not in reference for this period"
                    results.append(rec); continue
                per_share = CANONICAL.get(canonical, {}).get("per_share", False)
                rec["api_value_millions"] = "" if per_share else round(ref_val, 3)
                rec["api_value_local"] = ref_val if per_share else ""
                rec["status"] = cmp(fv, ref_val, per_share)
                rec["note"] = f"matched {canonical}"
                results.append(rec)

    cols = ["file", "company_id", "company_name", "calendar_year", "calendar_quarter",
            "financial_code", "segment_code", "file_value", "api_value_local",
            "api_value_millions", "status", "note"]
    with open(out_dir / "verification_results.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in cols})
    mism = [r for r in results if r["status"] == "MISMATCH"]
    with open(out_dir / "mismatches.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in mism:
            w.writerow({k: r.get(k, "") for k in cols})
    code_rows, codesum_csv = mismatch_code_summary(mism, out_dir)
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    print("\n=== Summary (offline vs reference, fuzzy-matched) ===")
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {st:22} {n}")
    print(f"\nFull results : {out_dir/'verification_results.csv'}")
    print(f"Mismatches   : {out_dir/'mismatches.csv'}  ({len(mism)} rows)")
    print(f"Mismatch code summary : {codesum_csv}")
    print_mismatch_code_summary(code_rows)
    for r in mism[:50]:
        print(f"  [{r['company_id']} {r['company_name']}] {r['financial_code']} "
              f"{r['segment_code'] or ''} {r['calendar_year']}Q{r['calendar_quarter']}: "
              f"file={r['file_value']} vs ref={r['api_value_millions']}  ({r['note']})")
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
                    help="run live against the 6 validated companies in sample_data/")
    ap.add_argument("--export", action="store_true",
                    help="also write the API-fetched values in your own file schema "
                         "to <out-dir>/export/, for diffing against your originals")
    ap.add_argument("--dump", action="store_true",
                    help="pull a STANDALONE reference of all configured companies "
                         "(no input file needed) to <out-dir>/reference/. Slow.")
    ap.add_argument("--dump-seg", action="store_true",
                    help="with --dump, also enumerate segment/geographic revenue "
                         "(US/TW fully, KR geographic; slower)")
    ap.add_argument("--dump-years", default="2019-2025",
                    help="year range for --dump, e.g. 2019-2025 or 2022-2025")
    ap.add_argument("--reference", default=None,
                    help="compare your --data-dir files against a pulled reference "
                         "dir (offline, no API calls) with fuzzy label matching, "
                         "e.g. --reference ./out/reference")
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
    if args.dump:
        lo, hi = (int(x) for x in args.dump_years.split("-"))
        dump_reference(Path(args.out_dir), registry, range(lo, hi + 1),
                       include_seg=args.dump_seg, seg_members=seg_members)
        return
    if args.reference:
        compare_against_reference(Path(args.data_dir), Path(args.reference),
                                  Path(args.out_dir), registry, metric_map,
                                  DEFAULT_FILES, mapping)
        return
    run(Path(args.data_dir), Path(args.out_dir), args.compare_column,
        registry, metric_map, DEFAULT_FILES, seg_members, mapping, args.export)


if __name__ == "__main__":
    main()
