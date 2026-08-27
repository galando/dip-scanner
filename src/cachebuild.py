"""Build and extend the offline price cache from the live feed.

The replay, tuning and validation tools all read `data/prices/`. That cache was
bootstrapped by hand, which is fine once and unbearable to repeat: extending it
to two years across the demo universe is tens of thousands of bars. This module
fills it from yfinance instead, so depth becomes a command rather than a chore.

It is deliberately separate from `src/data.py`. That module serves the live
scanner and returns whatever the feed gives it; this one is a maintenance tool
that writes to disk, and it refuses to write data that disagrees with what is
already cached.

Usage:
    PYTHONPATH=. python -m src.cachebuild --period 2y            # universe in data/prices
    PYTHONPATH=. python -m src.cachebuild --period 5y --tickers MSFT NFLX SPY
    PYTHONPATH=. python -m src.cachebuild --check                # verify, write nothing

Needs network access to the price feed. In a sandbox without it the command
fails loudly rather than writing a half-built cache.
"""
import argparse
import json
import logging
import os

import pandas as pd

import src.pricecache as pricecache

logger = logging.getLogger(__name__)

# A bar already in the cache and the same bar re-fetched should agree to about a
# cent. They can differ slightly: the feed restates history after a corporate
# action, and the cache may predate one.
PRICE_TOLERANCE = 0.011
FIELDS = ("open", "high", "low", "close", "volume")


def _frame_to_arrays(df: pd.DataFrame) -> dict[str, list]:
    return {
        "open": [round(float(v), 4) for v in df["Open"]],
        "high": [round(float(v), 4) for v in df["High"]],
        "low": [round(float(v), 4) for v in df["Low"]],
        "close": [round(float(v), 4) for v in df["Close"]],
        "volume": [int(v) for v in df["Volume"].fillna(0)],
    }


def compare(existing: pd.DataFrame, fresh: pd.DataFrame,
            tolerance: float = PRICE_TOLERANCE) -> list[str]:
    """Bars present in both that disagree. Empty list means the two agree."""
    shared = existing.index.intersection(fresh.index)
    problems = []
    for column in ("Open", "High", "Low", "Close"):
        a, b = existing.loc[shared, column], fresh.loc[shared, column]
        drift = (a - b).abs()
        for ts in drift[drift > tolerance].index:
            problems.append(
                f"{ts.date()} {column}: cached {a[ts]:.4f} vs fetched {b[ts]:.4f}"
            )
    return problems


def write_ticker(ticker: str, df: pd.DataFrame, dates: list[str],
                 cache_dir: str = pricecache.CACHE_DIR) -> tuple[int, list[str]]:
    """Write one ticker right-aligned against `dates`.

    Returns (bars written, missing sessions). The store addresses bars by
    position — a series of N bars means the last N calendar dates — so it cannot
    represent a hole. Rather than filling one in, which would put invented
    prices and volumes on disk indistinguishable from real ones, a ticker with
    gaps is reported and not written.
    """
    df = df[df.index.isin(pd.to_datetime(dates))].sort_index()
    if df.empty:
        return 0, []
    tail = [d for d in dates if pd.Timestamp(d) >= df.index[0]]
    missing = [d for d in tail if pd.Timestamp(d) not in df.index]
    if missing:
        return 0, missing
    df = df.reindex(pd.to_datetime(tail))
    with open(os.path.join(cache_dir, f"{ticker}.json"), "w") as f:
        json.dump(_frame_to_arrays(df), f)
    return len(df), []


def build(tickers: list[str], period: str = "2y", check_only: bool = False,
          cache_dir: str = pricecache.CACHE_DIR) -> dict:
    """Fetch `tickers` and refresh the cache. Returns a per-ticker summary.

    Raises before writing anything if fresh data contradicts what is cached —
    a silent overwrite would invalidate every result already published from it.
    """
    import src.data as data                      # imported late: needs network

    fetched = data.fetch_prices(tickers, period=period)
    if not fetched:
        raise RuntimeError(
            f"the price feed returned nothing for {len(tickers)} tickers — "
            "no network access, or the feed rejected the request"
        )

    conflicts: dict[str, list[str]] = {}
    for ticker, fresh in fetched.items():
        existing = pricecache.load_frame(ticker, cache_dir)
        if existing is None:
            continue
        problems = compare(existing, fresh)
        if problems:
            conflicts[ticker] = problems[:5]
    if conflicts:
        detail = "; ".join(f"{t}: {p[0]}" for t, p in list(conflicts.items())[:5])
        raise ValueError(
            f"fresh data contradicts the cache for {len(conflicts)} ticker(s) "
            f"({detail}). Results already published from this cache would be "
            f"invalidated by overwriting it — investigate before rebuilding."
        )

    # The calendar is the union of every session any ticker traded, merged with
    # whatever the cache already covers, which for a US equity universe is the
    # exchange calendar.
    try:
        old_calendar = pricecache.load_dates(cache_dir)
    except FileNotFoundError:
        old_calendar = []
    calendar = sorted(set(old_calendar) |
                      {ts.strftime("%Y-%m-%d")
                       for df in fetched.values() for ts in df.index})

    # Every cached ticker is addressed by position against this calendar, so
    # changing it re-dates every series that is not being rewritten. Growing the
    # calendar at the front is harmless (the tail each series occupies is
    # unchanged); anything else silently shifts the tickers left behind.
    stale = sorted(set(pricecache.available_tickers(cache_dir)) - set(fetched))
    shifts_the_tail = bool(old_calendar) and calendar[-len(old_calendar):] != old_calendar
    if stale and shifts_the_tail:
        raise ValueError(
            f"this fetch adds sessions at the end of the calendar, which re-dates "
            f"every cached series it does not rewrite ({len(stale)} would be left "
            f"behind: {', '.join(stale[:6])}{'...' if len(stale) > 6 else ''}). "
            f"Refetch the whole cache instead of a subset."
        )

    summary = {"tickers": {}, "skipped": {}, "sessions": len(calendar),
               "first": calendar[0], "last": calendar[-1]}
    if check_only:
        return summary

    with open(os.path.join(cache_dir, "_dates.json"), "w") as f:
        json.dump(calendar, f)
    for ticker, fresh in fetched.items():
        written, missing = write_ticker(ticker, fresh, calendar, cache_dir)
        if missing:
            summary["skipped"][ticker] = missing
            logger.warning("%s: not written, %d session(s) missing from the feed "
                           "(first %s)", ticker, len(missing), missing[0])
        else:
            summary["tickers"][ticker] = written
    pricecache.clear_cache()
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--period", default="2y",
                        help="history depth to fetch (yfinance period, default 2y)")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="tickers to fetch (default: whatever is already cached)")
    parser.add_argument("--check", action="store_true",
                        help="compare against the cache and write nothing")
    args = parser.parse_args()

    tickers = args.tickers or pricecache.available_tickers()
    if not tickers:
        raise SystemExit("no tickers given and the cache is empty")
    summary = build(tickers, period=args.period, check_only=args.check)

    verb = "would cover" if args.check else "cached"
    print(f"{verb} {summary['sessions']} sessions, "
          f"{summary['first']} .. {summary['last']}")
    if not args.check:
        short = {t: n for t, n in summary["tickers"].items()
                 if n < summary["sessions"]}
        print(f"{len(summary['tickers'])} tickers written"
              + (f"; {len(short)} with a shorter history than the calendar" if short else ""))
        if summary["skipped"]:
            print(f"{len(summary['skipped'])} skipped for gaps in the feed: "
                  + ", ".join(sorted(summary["skipped"])))


if __name__ == "__main__":
    main()
