"""Cache loading: alignment, as-of truncation, and the session calendar."""
import json
import os
from datetime import date

import pytest

import src.pricecache as pricecache


@pytest.fixture
def cache(tmp_path):
    dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    (tmp_path / "AAA.json").write_text(json.dumps({
        "open": [10, 11, 12, 13], "high": [11, 12, 13, 14],
        "low": [9, 10, 11, 12], "close": [10.5, 11.5, 12.5, 13.5],
        "volume": [100, 200, 300, 400],
    }))
    # Short series: right-aligned, so it covers only the last two dates.
    (tmp_path / "BBB.json").write_text(json.dumps({
        "open": [20, 21], "high": [21, 22], "low": [19, 20],
        "close": [20.5, 21.5], "volume": [10, 20],
    }))
    return str(tmp_path)


def test_available_tickers_skips_the_date_index(cache):
    assert pricecache.available_tickers(cache) == ["AAA", "BBB"]


def test_frame_is_indexed_by_trading_day(cache):
    df = pricecache.load_frame("AAA", cache)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index[0].strftime("%Y-%m-%d") == "2026-01-02"
    assert df["Close"].iloc[-1] == 13.5


def test_short_series_is_right_aligned(cache):
    df = pricecache.load_frame("BBB", cache)
    assert len(df) == 2
    assert df.index[0].strftime("%Y-%m-%d") == "2026-01-06"


def test_missing_ticker_is_none_not_an_error(cache):
    assert pricecache.load_frame("ZZZ", cache) is None


def test_asof_never_returns_future_bars(cache):
    prices = pricecache.fetch_prices_asof(["AAA"], date(2026, 1, 5), cache)
    assert len(prices["AAA"]) == 2
    assert prices["AAA"]["Close"].iloc[-1] == 11.5


def test_asof_drops_tickers_with_no_history_yet(cache):
    prices = pricecache.fetch_prices_asof(["AAA", "BBB"], date(2026, 1, 5), cache)
    assert "BBB" not in prices


def test_lookback_caps_the_window(cache):
    prices = pricecache.fetch_prices_asof(["AAA"], date(2026, 1, 7), cache, lookback=2)
    assert len(prices["AAA"]) == 2


def test_trading_days_are_inclusive_of_both_ends(cache):
    days = pricecache.trading_days(date(2026, 1, 5), date(2026, 1, 6), cache)
    assert days == [date(2026, 1, 5), date(2026, 1, 6)]
