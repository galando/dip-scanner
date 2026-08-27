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

    A batch download pads every ticker out to the union of all their sessions,
    so a ticker that did not trade that day arrives as a row of NaN rather than
    as an absent row. Those are dropped first: an all-NaN row is a hole wearing
    a date, and writing it would put `NaN` in the JSON, hand replay a price that
    silently books as a losing trade, and let an IPO present full calendar depth
    to BACKTEST_MIN_HISTORY.
    """
    arrays, missing = _encode_ticker(df, dates)
    if missing or arrays is None:
        return 0, missing
    with open(os.path.join(cache_dir, f"{ticker}.json"), "w") as f:
        json.dump(arrays, f)
    return len(arrays["close"]), []


def _encode_ticker(df: pd.DataFrame, dates: list[str]) -> tuple[dict | None, list[str]]:
    """Right-align one frame against `dates` without touching disk.

    Returns (arrays, missing sessions); arrays is None when there is nothing to
    write. Separated from write_ticker so `build` can decide whether the whole
    refresh is safe before any of it lands.
    """
    df = df[df.index.isin(pd.to_datetime(dates))].sort_index()
    serialized = [c for c in ("Open", "High", "Low", "Close", "Volume")
                  if c in df.columns]
    df = df.dropna(subset=serialized, how="any")
    if df.empty:
        return None, []
    tail = [d for d in dates if pd.Timestamp(d) >= df.index[0]]
    missing = [d for d in tail if pd.Timestamp(d) not in df.index]
    if missing:
        return None, missing
    return _frame_to_arrays(df.reindex(pd.to_datetime(tail))), []


def build(tickers: list[str], period: str = "2y", check_only: bool = False,
          cache_dir: str = pricecache.CACHE_DIR) -> dict:
    """Fetch `tickers` and refresh the cache. Returns a per-ticker summary.

    Raises before writing anything if fresh data contradicts what is cached —
    a silent overwrite would invalidate every result already published from it.
    """
    import src.data as data                      # imported late: needs network

    os.makedirs(cache_dir, exist_ok=True)
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
    # In check mode a conflict is the finding, not a reason to abort: raising
    # here suppressed the calendar summary and every other refusal the run had
    # already worked out, which is the opposite of what --check is for.
    conflict_detail = "; ".join(f"{t}: {p[0]}" for t, p in list(conflicts.items())[:5])
    would_refuse: list[str] = []
    if conflicts and check_only:
        would_refuse.append(
            f"{len(conflicts)} ticker(s) contradict the cache ({conflict_detail})")
    elif conflicts:
        detail = conflict_detail
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
    shifts_the_tail = bool(old_calendar) and calendar[-len(old_calendar):] != old_calendar
    cached = set(pricecache.available_tickers(cache_dir))

    # Encode everything before writing anything. A ticker skipped for a gap is
    # left behind by a calendar change exactly as a ticker that was never
    # fetched is, and that is only known after encoding — so the refusal below
    # has to come after this loop, and no file may be written before it.
    encoded: dict[str, dict] = {}
    summary = {"tickers": {}, "skipped": {}, "sessions": len(calendar),
               "first": calendar[0], "last": calendar[-1],
               "would_refuse": would_refuse}
    for ticker, fresh in fetched.items():
        arrays, missing = _encode_ticker(fresh, calendar)
        # arrays is None with no missing sessions when the feed returned nothing
        # usable at all — every row NaN, or none of them on the calendar. That is
        # a skip like any other, not a ticker to write and not a crash.
        if arrays is None:
            summary["skipped"][ticker] = missing
            if missing:
                logger.warning("%s: not written, %d session(s) missing from the "
                               "feed (first %s)", ticker, len(missing), missing[0])
            else:
                logger.warning("%s: not written, the feed returned no usable bars "
                               "on the calendar", ticker)
        else:
            encoded[ticker] = arrays
            summary["tickers"][ticker] = len(arrays["close"])

    # A shorter --period than the cache already holds arrives as a clean,
    # conflict-free, calendar-preserving overwrite that happens to delete years
    # of bars. Nothing above notices: compare() only sees the sessions the two
    # share, and the calendar is a union so it never shrinks. Refuse instead.
    truncated = []
    for ticker, arrays in encoded.items():
        old_frame = pricecache.load_frame(ticker, cache_dir)
        if old_frame is not None and len(arrays["close"]) < len(old_frame):
            truncated.append(f"{ticker} {len(old_frame)}->{len(arrays['close'])}")
    if truncated and check_only:
        summary["would_refuse"].append(
            f"{len(truncated)} ticker(s) would lose bars: {', '.join(sorted(truncated)[:6])}")
    elif truncated:
        raise ValueError(
            f"this fetch returns fewer bars than the cache already holds for "
            f"{len(truncated)} ticker(s) ({', '.join(sorted(truncated)[:6])}"
            f"{'...' if len(truncated) > 6 else ''}). Writing it would delete "
            f"history the published results were measured on. Fetch at least as "
            f"deep as the cache — the default --period is 2y."
        )

    left_behind = sorted((cached - set(encoded)) | (set(summary["skipped"]) & cached))
    if left_behind and shifts_the_tail and check_only:
        summary["would_refuse"].append(
            f"{len(left_behind)} cached ticker(s) would be left behind by a longer "
            f"calendar: {', '.join(left_behind[:6])}")
    elif left_behind and shifts_the_tail:
        raise ValueError(
            f"this fetch adds sessions at the end of the calendar, which re-dates "
            f"every cached series it does not rewrite ({len(left_behind)} would be "
            f"left behind: {', '.join(left_behind[:6])}"
            f"{'...' if len(left_behind) > 6 else ''}). "
            f"Refetch the whole cache instead of a subset."
        )

    if check_only:
        return summary

    with open(os.path.join(cache_dir, "_dates.json"), "w") as f:
        json.dump(calendar, f)
    for ticker, arrays in encoded.items():
        with open(os.path.join(cache_dir, f"{ticker}.json"), "w") as f:
            json.dump(arrays, f)
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
    if summary["skipped"]:
        verb = "would be skipped" if args.check else "skipped"
        print(f"{len(summary['skipped'])} {verb} for gaps in the feed: "
              + ", ".join(sorted(summary["skipped"])))
    # A check that stays quiet about what the real run would refuse on is not a
    # check — the user would find out by running it for real.
    for reason in summary.get("would_refuse", []):
        print(f"WOULD REFUSE: {reason}")
    if not args.check:
        short = {t: n for t, n in summary["tickers"].items()
                 if n < summary["sessions"]}
        print(f"{len(summary['tickers'])} tickers written"
              + (f"; {len(short)} with a shorter history than the calendar" if short else ""))


if __name__ == "__main__":
    main()
