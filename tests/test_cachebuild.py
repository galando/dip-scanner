"""The cache builder must never silently overwrite data that disagrees with it."""
import json
import os

import pandas as pd
import pytest

import src.cachebuild as cachebuild
import src.pricecache as pricecache


def _frame(closes, start="2026-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [100] * len(closes)}, index=idx)


@pytest.fixture
def cache(tmp_path):
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-02", periods=5)]
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    (tmp_path / "AAA.json").write_text(json.dumps({
        "open": [10, 11, 12, 13, 14], "high": [10, 11, 12, 13, 14],
        "low": [10, 11, 12, 13, 14], "close": [10, 11, 12, 13, 14],
        "volume": [100] * 5}))
    pricecache.clear_cache()
    yield str(tmp_path)
    pricecache.clear_cache()


def test_identical_data_is_not_a_conflict(cache):
    existing = pricecache.load_frame("AAA", cache)
    assert cachebuild.compare(existing, existing) == []


def test_a_rounding_difference_is_tolerated(cache):
    existing = pricecache.load_frame("AAA", cache)
    nudged = existing.copy()
    nudged["Close"] = nudged["Close"] + 0.005
    assert cachebuild.compare(existing, nudged) == []


def test_a_real_disagreement_is_reported_with_both_values(cache):
    existing = pricecache.load_frame("AAA", cache)
    changed = existing.copy()
    changed.iloc[2, changed.columns.get_loc("Close")] = 99.0
    problems = cachebuild.compare(existing, changed)
    assert len(problems) == 1
    assert "cached 12.0000" in problems[0] and "fetched 99.0000" in problems[0]


def test_build_refuses_to_write_over_conflicting_data(cache, monkeypatch):
    import src.data as data
    monkeypatch.setattr(data, "fetch_prices",
                        lambda tickers, period=None: {"AAA": _frame([10, 11, 99, 13, 14])})
    before = (open(f"{cache}/AAA.json").read())
    with pytest.raises(ValueError, match="contradicts the cache"):
        cachebuild.build(["AAA"], cache_dir=cache)
    assert open(f"{cache}/AAA.json").read() == before, "cache was modified despite the conflict"


def test_build_fails_loudly_when_the_feed_returns_nothing(cache, monkeypatch):
    import src.data as data
    monkeypatch.setattr(data, "fetch_prices", lambda tickers, period=None: {})
    with pytest.raises(RuntimeError, match="no network access"):
        cachebuild.build(["AAA"], cache_dir=cache)


def test_build_extends_history_backwards_and_keeps_the_calendar(cache, monkeypatch):
    import src.data as data
    # Three extra sessions in front, and the shared tail must still read 10..14.
    deeper = _frame([7, 8, 9, 10, 11, 12, 13, 14], start="2025-12-30")
    monkeypatch.setattr(data, "fetch_prices", lambda tickers, period=None: {"AAA": deeper})
    summary = cachebuild.build(["AAA"], cache_dir=cache)
    assert summary["sessions"] == 8
    assert summary["tickers"]["AAA"] == 8
    refreshed = pricecache.load_frame("AAA", cache)
    assert len(refreshed) == 8
    assert refreshed["Close"].iloc[-1] == 14


def test_a_partial_refetch_that_would_shift_the_calendar_is_refused(cache, monkeypatch):
    """Series are addressed by position, so a longer calendar re-dates whatever
    is not rewritten. Extending only one of two cached tickers must not do that."""
    (open(f"{cache}/BBB.json", "w")
     .write(json.dumps({"open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5],
                        "low": [1, 2, 3, 4, 5], "close": [1, 2, 3, 4, 5],
                        "volume": [1] * 5})))
    pricecache.clear_cache()
    import src.data as data
    longer = _frame([10, 11, 12, 13, 14, 15])          # one extra session at the end
    monkeypatch.setattr(data, "fetch_prices", lambda tickers, period=None: {"AAA": longer})
    with pytest.raises(ValueError, match="re-dates"):
        cachebuild.build(["AAA"], cache_dir=cache)


def test_deepening_only_the_front_leaves_other_tickers_alone(cache, monkeypatch):
    (open(f"{cache}/BBB.json", "w")
     .write(json.dumps({"open": [1, 2, 3, 4, 5], "high": [1, 2, 3, 4, 5],
                        "low": [1, 2, 3, 4, 5], "close": [1, 2, 3, 4, 5],
                        "volume": [1] * 5})))
    pricecache.clear_cache()
    import src.data as data
    deeper = _frame([7, 8, 9, 10, 11, 12, 13, 14], start="2025-12-30")
    monkeypatch.setattr(data, "fetch_prices", lambda tickers, period=None: {"AAA": deeper})
    cachebuild.build(["AAA"], cache_dir=cache)
    bbb = pricecache.load_frame("BBB", cache)
    assert bbb["Close"].iloc[-1] == 5
    assert bbb.index[-1].strftime("%Y-%m-%d") == pricecache.load_dates(cache)[-1]


def test_a_ticker_with_a_hole_is_skipped_rather_than_invented(cache, monkeypatch):
    import src.data as data
    full = _frame([10, 11, 12, 13, 14])
    gapped = full.drop(full.index[2])                   # a session the feed never returned
    monkeypatch.setattr(data, "fetch_prices",
                        lambda tickers, period=None: {"AAA": full, "GAP": gapped})
    summary = cachebuild.build(["AAA", "GAP"], cache_dir=cache)
    assert "GAP" in summary["skipped"] and "GAP" not in summary["tickers"]
    assert not os.path.exists(f"{cache}/GAP.json"), "wrote a series containing invented bars"
