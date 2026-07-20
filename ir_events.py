#!/usr/bin/env python3
"""Pull upcoming Investor-Relations calendar events via the yfinance library.

Uses yfinance's ``Ticker.get_earnings_dates()`` as the primary source for the
next scheduled results/earnings announcement.  That endpoint is far more
reliable than Yahoo's ``calendarEvents`` blob (which serves the next date only
intermittently for some symbols, e.g. AMD) and it returns the announcement
timestamp plus the consensus EPS estimate for the upcoming quarter.  Ex-dividend
and dividend-payment dates are taken best-effort from ``Ticker.calendar``.

Company names are mapped to Yahoo tickers in COMPANIES below.  Some entries are
local subsidiaries that are not separately listed (e.g. ABB Taiwan); those fall
back to the listed parent, flagged in the notes column.

Network note: in this environment yfinance's curl_cffi backend must go through
the agent HTTPS proxy and impersonate Safari (Chrome's TLS fingerprint is reset
by the egress proxy).  make_session() wires that up from the environment.

Usage:
    python3 ir_events.py            # the built-in company list
    python3 ir_events.py --json     # machine-readable output
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

from curl_cffi import requests as cffi_requests  # noqa: E402
import yfinance as yf  # noqa: E402

DEFAULT_WATCHLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.csv")


def load_companies(path: str) -> list[tuple[str, str | None, str]]:
    """Load (company, ticker, note) rows from a CSV with those headers.
    An empty ticker cell means the company has no listed security."""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip() or None
            out.append(((row.get("company") or "").strip(), ticker,
                        (row.get("note") or "").strip()))
    return out


def make_session() -> cffi_requests.Session:
    """curl_cffi session configured for this environment's HTTPS proxy."""
    kwargs = {"impersonate": "safari"}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        kwargs["proxies"] = {"https": proxy, "http": proxy}
    ca = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca:
        kwargs["verify"] = ca
    return cffi_requests.Session(**kwargs)


def _recent(date_str: str | None, max_age_days: int = 730) -> str | None:
    """Drop obviously-stale dividend dates (Yahoo returns 1990s placeholders
    for non-dividend payers) while keeping recent/upcoming ones."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return date_str
    return None if (datetime.now(timezone.utc) - d).days > max_age_days else date_str


def _as_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d")
    except AttributeError:
        return str(value)


def get_ir_events(ticker: str, session) -> dict:
    t = yf.Ticker(ticker, session=session)
    today = datetime.now(timezone.utc).date()

    next_date = next_time = None
    next_eps = last_reported = None
    try:
        df = t.get_earnings_dates(limit=24)
    except Exception:
        df = None

    if df is not None and not df.empty:
        # Upcoming = earliest announcement dated today or later (not yet reported).
        upcoming = [ts for ts in df.index if ts.date() >= today]
        reported = [ts for ts in df.index if ts.date() < today]
        if upcoming:
            nxt = min(upcoming)
            next_date = nxt.date().isoformat()
            # tz-aware timestamp carries the announcement time (BMO/AMC).
            next_time = nxt.strftime("%H:%M %Z").strip() or None
            eps = df.loc[nxt, "EPS Estimate"]
            next_eps = float(eps) if eps == eps else None  # NaN check
        if reported:
            last_reported = max(reported).date().isoformat()

    # Dividend dates are secondary; take them best-effort from calendar.
    ex_div = div_pay = None
    try:
        cal = t.calendar or {}
        ex_div = _recent(_as_date(cal.get("Ex-Dividend Date")))
        div_pay = _recent(_as_date(cal.get("Dividend Date")))
    except Exception:
        pass

    return {
        "ticker": ticker,
        "nextEarningsDate": next_date,
        "nextEarningsTime": next_time,
        "nextEpsEstimate": next_eps,
        "lastReported": last_reported,
        "exDividendDate": ex_div,
        "dividendDate": div_pay,
    }


def build_rows(companies: list[tuple[str, str | None, str]]) -> tuple[list[dict], list[tuple[str, str]]]:
    session = make_session()
    rows, errors = [], []
    for name, ticker, note in companies:
        row = {"company": name, "ticker": ticker, "note": note}
        if ticker is None:
            row.update(nextEarningsDate=None, status="not listed")
            rows.append(row)
            continue
        try:
            row.update(get_ir_events(ticker, session))
            row["status"] = "ok"
        except Exception as e:  # noqa: BLE001
            row.update(nextEarningsDate=None, status="error")
            errors.append((name, str(e)))
        rows.append(row)
    return rows, errors


def print_table(rows: list[dict]) -> None:
    hdr = (f"{'Company':<34}{'Ticker':<11}{'Next earnings':<14}"
           f"{'EPS est.':<10}{'Ex-div':<12}{'Dividend pay':<12}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        ne = r.get("nextEarningsDate") or "-"
        eps = r.get("nextEpsEstimate")
        eps_s = f"{eps:.2f}" if isinstance(eps, (int, float)) else "-"
        print(f"{r['company']:<34}{str(r['ticker'] or '-'):<11}{ne:<14}"
              f"{eps_s:<10}{r.get('exDividendDate') or '-':<12}"
              f"{r.get('dividendDate') or '-':<12}")
        extra = []
        if not r.get("nextEarningsDate") and r.get("lastReported"):
            extra.append(f"no scheduled date; last reported {r['lastReported']}")
        if r.get("note"):
            extra.append(r["note"])
        for line in extra:
            print(f"{'':<34}   ↳ {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull IR calendar events via yfinance.")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST,
                        help="CSV of company,ticker,note (default: watchlist.csv)")
    args = parser.parse_args(argv)

    companies = load_companies(args.watchlist)
    rows, errors = build_rows(companies)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"Investor-Relations calendar events "
              f"(pulled {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}, "
              f"source: yfinance / Yahoo Finance)\n")
        print_table(rows)

    for name, msg in errors:
        print(f"\n[!] {name}: {msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
