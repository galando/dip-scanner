"""Offline daily-bar store, so a simulation can be replayed without a live feed.

The production path (src/data.py) downloads from yfinance on every run. That is
fine for the daily cron, but it makes a historical replay impossible: yfinance
cannot be asked "what did this stock look like on 3 August", and in restricted
network environments it is not reachable at all.

This module reads OHLCV bars that were snapshotted into data/prices/ and hands
back the same {ticker: DataFrame} shape src.data.fetch_prices returns, truncated
to a chosen as-of date. Every gate and indicator in the project therefore works
unchanged on cached data.

Layout:
    data/prices/_dates.json   ["2025-05-06", ...]  shared trading-day index
    data/prices/<TICKER>.json {"open": [...], "high": [...], "low": [...],
                               "close": [...], "volume": [...]}

A ticker's arrays are right-aligned against _dates.json: a series with N bars
covers the last N dates. Bars are as reported by the broker feed, which applies
corporate-action adjustments retroactively — see README for the one ticker
(SPGI) where that matters.
"""
import json
import os
from datetime import date

import pandas as pd

CACHE_DIR = os.path.join("data", "prices")
_DATES_FILE = "_dates.json"


def available_tickers(cache_dir: str = CACHE_DIR) -> list[str]:
    return sorted(
        f[:-5] for f in os.listdir(cache_dir)
        if f.endswith(".json") and not f.startswith("_")
    )


def load_dates(cache_dir: str = CACHE_DIR) -> list[str]:
    with open(os.path.join(cache_dir, _DATES_FILE)) as f:
        return json.load(f)


def load_frame(ticker: str, cache_dir: str = CACHE_DIR) -> pd.DataFrame | None:
    """One ticker's full cached history as an OHLCV frame indexed by date."""
    path = os.path.join(cache_dir, f"{ticker}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    dates = load_dates(cache_dir)[-len(raw["close"]):]
    return pd.DataFrame(
        {
            "Open": raw["open"],
            "High": raw["high"],
            "Low": raw["low"],
            "Close": raw["close"],
            "Volume": raw["volume"],
        },
        index=pd.to_datetime(dates),
    )


def fetch_prices_asof(tickers: list[str], as_of: date, cache_dir: str = CACHE_DIR,
                      lookback: int = 252) -> dict[str, pd.DataFrame]:
    """Bars up to and including `as_of` — the view a live run would have had.

    Anything after `as_of` is dropped, so a replay can never see the future.
    `lookback` caps the window at the same order of history the live scanner
    pulls (config.PRICE_HISTORY_PERIOD = 1y).
    """
    cutoff = pd.Timestamp(as_of)
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = load_frame(ticker, cache_dir)
        if df is None:
            continue
        df = df[df.index <= cutoff]
        if df.empty:
            continue
        out[ticker] = df.iloc[-lookback:]
    return out


def trading_days(start: date, end: date, cache_dir: str = CACHE_DIR) -> list[date]:
    """Cached sessions in [start, end] — the days a daily cron would have fired."""
    return [
        date.fromisoformat(d) for d in load_dates(cache_dir)
        if start.isoformat() <= d <= end.isoformat()
    ]
