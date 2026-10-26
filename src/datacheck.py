"""Check the live data sources against what the gates expect.

    PYTHONPATH=. python -m src.datacheck              # a default handful of tickers
    PYTHONPATH=. python -m src.datacheck AAPL JPM KO
    PYTHONPATH=. python -m src.datacheck --tags JPM   # also every debt-like tag filed

For each ticker it prints what Gate 1 and Gate 3 will read: the next earnings
date from Yahoo's calendar, the 8-K items SEC lists for the last 90 days, and
the four quality fields from Yahoo and from the filings side by side. Yahoo
and SEC measure ROE and debt/equity differently, so this is where to look
before turning QUALITY_SOURCE to "sec" changes which stocks alert.

Exits 1 when a source the scanner relies on returned nothing usable for any
ticker: no earnings date at all, or SEC switched on but no figures came back.
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import yfinance as yf

import config
import src.data as data
import src.sec as sec

DEFAULT_TICKERS = ["AAPL", "MSFT", "JPM", "KO", "NVDA", "BRK-B"]


def _fmt(field: str, value) -> str:
    if value is None:
        return "n/a"
    if field == "marketCap":
        return f"${value / 1e9:,.0f}B"
    if field == "debtToEquity":
        return f"{value:.0f}"
    return f"{value * 100:.1f}%"


def check(ticker: str, today: date, tags: bool = False) -> dict:
    """Everything the gates would read for one ticker, from both sources."""
    out = {"ticker": ticker, "earnings": None, "yahoo": {}, "sec": None,
           "filings": [], "tags": None, "sic": None, "errors": []}
    price = None
    try:
        t = yf.Ticker(ticker)
        info = dict(t.info or {})
        out["yahoo"] = {f: info.get(f) for f in sec.QUALITY_FIELDS}
        out["earnings"] = data._next_earnings_date(t, info, today)
        price = info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception as e:
        out["errors"].append(f"Yahoo: {e}")

    if sec.enabled():
        company = sec.load_company(ticker, include_facts=True)
        if company is None:
            out["errors"].append("SEC: no data (no CIK, or the request failed)")
        else:
            out["sec"] = sec.fundamentals_asof(company, known_by=today, price=price)
            out["sic"] = company.sic
            since = (today - timedelta(days=90)).isoformat()
            out["filings"] = [f for f in company.filings if f["date"] >= since]
            end = out["sec"].get("periodEnd")
            if tags and end:
                out["tags"] = sec.debt_like_tags(sec.fetch_raw_facts(company.cik), end)
    return out


def render(result: dict) -> str:
    lines = [f"{result['ticker']}"]
    lines.append(f"  next earnings (Yahoo calendar): {result['earnings'] or 'none found'}")
    if result["sec"] is not None:
        events = ", ".join(f"{f['date']} {'/'.join(f['items'])}" for f in result["filings"])
        lines.append(f"  8-K, last 90 days (SEC): {events or 'none'}")
        excluded = sec.excluded_industry(result["sic"], config)
        lines.append(f"  SEC industry code: {result['sic'] or 'n/a'}"
                     + ("  (a bank: Gate 1 rejects it)" if excluded else ""))
        lines.append(f"  quality, as of the {result['sec'].get('periodEnd') or '?'} quarter:")
        lines.append(f"    {'field':18}{'Yahoo':>12}{'SEC':>12}")
        for f in sec.QUALITY_FIELDS:
            lines.append(f"    {f:18}{_fmt(f, result['yahoo'].get(f)):>12}"
                         f"{_fmt(f, result['sec'].get(f)):>12}")
        if result["sec"].get("negativeEquity"):
            lines.append("    SEC: equity is zero or negative, so Gate 1 fails this stock")
        parts = result["sec"].get("debtParts")
        if parts:
            borrowed = ("unknown" if parts["borrowings"] is None
                        else f"${parts['borrowings'] / 1e9:,.2f}B")
            lines.append(f"    debt (SEC): borrowings {borrowed} + leases "
                         f"${parts['leases'] / 1e9:,.2f}B  [{', '.join(parts['tags']) or 'no tags'}]")
        if result["tags"] is not None:
            lines.append(f"  debt-like tags filed for {result['sec'].get('periodEnd')}:")
            for concept, val in result["tags"] or [("(none)", 0.0)]:
                lines.append(f"    {concept:72}{val / 1e9:>12,.2f}B")
    else:
        lines.append("  quality (Yahoo): " + ", ".join(
            f"{f} {_fmt(f, result['yahoo'].get(f))}" for f in sec.QUALITY_FIELDS))
    for e in result["errors"]:
        lines.append(f"  ! {e}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.datacheck")
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--tags", action="store_true",
                        help="also list every debt-like tag each company filed (SEC only)")
    args = parser.parse_args(argv)
    tickers = args.tickers or DEFAULT_TICKERS
    today = datetime.now(timezone.utc).date()
    if sec.enabled():
        print(f"SEC: on (QUALITY_SOURCE = {config.QUALITY_SOURCE!r})")
    else:
        print("SEC: off (SEC_USER_AGENT is not set), so Yahoo only")
    print()
    results = [check(t, today, tags=args.tags) for t in tickers]
    for r in results:
        print(render(r))
        print()

    problems = []
    if not any(r["earnings"] for r in results):
        problems.append("no ticker returned an earnings date from Yahoo's calendar")
    if sec.enabled() and not any(r["sec"] and any(r["sec"].get(f) is not None
                                                    for f in sec.QUALITY_FIELDS)
                                 for r in results):
        problems.append("SEC is on but returned no quality figures for any ticker")
    if not sec.enabled() and sec.user_agent():
        problems.append("SEC refused SEC_USER_AGENT (403); see the log above")
    for p in problems:
        print(f"PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
