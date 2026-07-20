#!/usr/bin/env python3
"""Pull the Yahoo Finance earnings calendar for a set of tickers.

Follows the approach of https://github.com/gregfrasco/yahoo-finance-api
(which scrapes Yahoo Finance's earnings calendar), but talks to Yahoo's
JSON quoteSummary API instead of screen-scraping HTML.  For each ticker it
reports the upcoming earnings date plus the trailing quarters of reported
vs. estimated EPS and the surprise -- the same fields the reference library
exposes (epsEstimate, epsReported, epsSurprise).

Usage:
    python3 earnings_calendar.py                 # NVDA, AVGO, QCOM
    python3 earnings_calendar.py AAPL MSFT       # custom tickers
    python3 earnings_calendar.py --json          # machine-readable output
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_TICKERS = ["NVDA", "AVGO", "QCOM"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MODULES = "calendarEvents,earnings,earningsHistory"


class YahooFinance:
    """Minimal Yahoo Finance client that handles the cookie + crumb handshake."""

    def __init__(self) -> None:
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj)
        )
        self._crumb: str | None = None

    def _get(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return self._opener.open(req, timeout=25)

    def _ensure_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        # Seed authentication cookies, then request a crumb token.  fc.yahoo.com
        # returns a 404 but still sets the cookies we need, so ignore its errors.
        try:
            self._get("https://fc.yahoo.com")
        except urllib.error.HTTPError:
            pass
        self._crumb = self._get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb"
        ).read().decode().strip()
        if not self._crumb:
            raise RuntimeError("Failed to obtain a Yahoo Finance crumb token")
        return self._crumb

    def get_earnings(self, symbol: str) -> dict:
        crumb = self._ensure_crumb()
        url = (
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
            f"{urllib.parse.quote(symbol)}?modules={MODULES}"
            f"&crumb={urllib.parse.quote(crumb)}"
        )
        payload = json.load(self._get(url))
        result = payload.get("quoteSummary", {}).get("result")
        if not result:
            err = payload.get("quoteSummary", {}).get("error")
            raise RuntimeError(f"No data for {symbol}: {err}")
        return result[0]


def _fmt_date(node) -> str | None:
    """Yahoo dates arrive either as {'raw','fmt'} or a list of those."""
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        if node.get("fmt"):
            return node["fmt"]
        if node.get("raw") is not None:
            return datetime.fromtimestamp(
                node["raw"], tz=timezone.utc
            ).strftime("%Y-%m-%d")
    return None


def _num(node):
    if isinstance(node, dict):
        return node.get("raw")
    return None


def extract(symbol: str, data: dict) -> dict:
    cal = data.get("calendarEvents", {}).get("earnings", {})
    earnings = data.get("earnings", {})
    history = data.get("earningsHistory", {}).get("history", [])

    next_date = _fmt_date(cal.get("earningsDate"))
    is_estimate = cal.get("isEarningsDateEstimate")

    quarters = []
    for h in history:
        actual = _num(h.get("epsActual"))
        estimate = _num(h.get("epsEstimate"))
        surprise = _num(h.get("epsDifference"))
        surprise_pct = _num(h.get("surprisePercent"))
        quarters.append(
            {
                "quarter": h.get("quarter", {}).get("fmt"),
                "epsEstimate": estimate,
                "epsReported": actual,
                "epsSurprise": surprise,
                "epsSurprisePct": surprise_pct,
            }
        )

    return {
        "symbol": symbol,
        "nextEarningsDate": next_date,
        "nextDateIsEstimate": is_estimate,
        "epsEstimate": _num(cal.get("earningsAverage")),
        "epsEstimateLow": _num(cal.get("earningsLow")),
        "epsEstimateHigh": _num(cal.get("earningsHigh")),
        "revenueEstimate": _num(cal.get("revenueAverage")),
        "currentQuarter": earnings.get("earningsChart", {}).get("currentQuarterEstimateDate"),
        "history": quarters,
    }


def _fmt(v, spec="+.4f", dash="  -   "):
    return format(v, spec) if isinstance(v, (int, float)) else dash


def print_report(rows: list[dict]) -> None:
    for r in rows:
        est = " (estimated)" if r["nextDateIsEstimate"] else ""
        print(f"\n{'=' * 66}")
        print(f"  {r['symbol']}")
        print(f"{'=' * 66}")
        print(f"  Next earnings date : {r['nextEarningsDate'] or 'n/a'}{est}")
        eps = r["epsEstimate"]
        lo, hi = r["epsEstimateLow"], r["epsEstimateHigh"]
        if isinstance(eps, (int, float)):
            rng = ""
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                rng = f"  (range {lo:.2f} – {hi:.2f})"
            print(f"  Consensus EPS est. : {eps:.2f}{rng}")
        rev = r["revenueEstimate"]
        if isinstance(rev, (int, float)):
            print(f"  Consensus revenue  : {rev / 1e9:.2f}B")

        if r["history"]:
            print("\n  Recent reported quarters:")
            print(f"    {'Quarter':<9} {'Estimate':>9} {'Reported':>9} "
                  f"{'Surprise':>9} {'Surprise%':>10}")
            for q in r["history"]:
                pct = q["epsSurprisePct"]
                pct_s = f"{pct * 100:+.1f}%" if isinstance(pct, (int, float)) else "   -"
                print(f"    {q['quarter'] or '-':<9} "
                      f"{_fmt(q['epsEstimate'], '.4f', '    -   '):>9} "
                      f"{_fmt(q['epsReported'], '.4f', '    -   '):>9} "
                      f"{_fmt(q['epsSurprise']):>9} {pct_s:>10}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Yahoo Finance earnings calendar.")
    parser.add_argument("tickers", nargs="*", default=None,
                        help="ticker symbols (default: NVDA AVGO QCOM)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    tickers = [t.upper() for t in (args.tickers or DEFAULT_TICKERS)]
    client = YahooFinance()

    rows, errors = [], []
    for t in tickers:
        try:
            rows.append(extract(t, client.get_earnings(t)))
        except Exception as e:  # noqa: BLE001 - report and continue
            errors.append((t, str(e)))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"Yahoo Finance earnings calendar  "
              f"(pulled {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
        print_report(rows)

    for t, msg in errors:
        print(f"\n[!] {t}: {msg}", file=sys.stderr)
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
