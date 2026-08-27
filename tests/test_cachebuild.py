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


def test_nan_padded_rows_count_as_missing_not_as_bars(tmp_path):
    """A batch download pads absent sessions with NaN instead of omitting them."""
    import numpy as np
    import pandas as pd
    from src.cachebuild import write_ticker

    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    idx = pd.to_datetime(dates)
    closes = [1.0, 2.0, np.nan, 4.0]
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                       "Close": closes, "Volume": [10, 10, np.nan, 10]}, index=idx)

    bars, missing = write_ticker("AAA", df, dates, str(tmp_path))
    assert bars == 0
    assert missing == ["2026-01-07"]
    assert not (tmp_path / "AAA.json").exists()


def test_a_ticker_skipped_for_a_gap_is_not_left_behind_by_a_longer_calendar(tmp_path, monkeypatch):
    """The calendar must not be written while a cached ticker keeps its old array."""
    import json
    import numpy as np
    import pandas as pd
    import src.cachebuild as cachebuild
    import src.pricecache as pricecache

    cache = tmp_path
    old_dates = ["2026-01-05", "2026-01-06"]
    (cache / "_dates.json").write_text(json.dumps(old_dates))
    (cache / "BBB.json").write_text(json.dumps(
        {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
         "close": [1.0, 2.0], "volume": [10, 10]}))

    # BBB comes back with a hole, so it is skipped; the fetch also adds a session
    # at the end of the calendar, which would re-date BBB's untouched array.
    idx = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"])
    holed = [1.0, np.nan, 3.0]
    fetched = {"BBB": pd.DataFrame(
        {"Open": holed, "High": holed, "Low": holed, "Close": holed,
         "Volume": [10, np.nan, 10]}, index=idx)}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    with pytest.raises(ValueError, match="left behind"):
        cachebuild.build(["BBB"], cache_dir=str(cache))

    pricecache.clear_cache()
    assert json.loads((cache / "_dates.json").read_text()) == old_dates


def test_a_shallower_refetch_is_refused_not_silently_written(tmp_path, monkeypatch):
    """`--period 2y` over a 5y cache is a clean overwrite that deletes 3 years."""
    import json
    import pandas as pd
    import src.cachebuild as cachebuild
    import src.pricecache as pricecache

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    (cache / "_dates.json").write_text(json.dumps(dates))
    (cache / "AAA.json").write_text(json.dumps(
        {"open": [1.0, 2.0, 3.0, 4.0], "high": [1.0, 2.0, 3.0, 4.0],
         "low": [1.0, 2.0, 3.0, 4.0], "close": [1.0, 2.0, 3.0, 4.0],
         "volume": [10, 10, 10, 10]}))

    shallow = [3.0, 4.0]
    fetched = {"AAA": pd.DataFrame(
        {"Open": shallow, "High": shallow, "Low": shallow, "Close": shallow,
         "Volume": [10, 10]}, index=pd.to_datetime(dates[2:]))}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    with pytest.raises(ValueError, match="fewer bars than the cache"):
        cachebuild.build(["AAA"], cache_dir=str(cache))

    pricecache.clear_cache()
    assert len(pricecache.load_frame("AAA", str(cache))) == 4


def test_a_frame_with_no_usable_bars_is_skipped_not_a_crash(tmp_path, monkeypatch):
    import json
    import numpy as np
    import pandas as pd
    import src.cachebuild as cachebuild

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06"]
    (cache / "_dates.json").write_text(json.dumps(dates))
    nan = [np.nan, np.nan]
    fetched = {"AAA": pd.DataFrame(
        {"Open": nan, "High": nan, "Low": nan, "Close": [1.0, 2.0],
         "Volume": nan}, index=pd.to_datetime(dates))}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    summary = cachebuild.build(["AAA"], cache_dir=str(cache))
    assert "AAA" in summary["skipped"]
    assert not (cache / "AAA.json").exists()


def test_build_creates_the_cache_directory(tmp_path, monkeypatch):
    """Bootstrapping used to fail after the whole network fetch was spent."""
    import pandas as pd
    import src.cachebuild as cachebuild
    import src.pricecache as pricecache

    cache = tmp_path / "prices"          # does not exist
    dates = ["2026-01-05", "2026-01-06"]
    closes = [1.0, 2.0]
    fetched = {"AAA": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": [10, 10]}, index=pd.to_datetime(dates))}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    summary = cachebuild.build(["AAA"], cache_dir=str(cache))
    assert summary["tickers"] == {"AAA": 2}
    pricecache.clear_cache()
    assert len(pricecache.load_frame("AAA", str(cache))) == 2


def test_check_only_reports_a_refusal_instead_of_raising(tmp_path, monkeypatch):
    """--check is documented as 'compare and write nothing', not 'abort'."""
    import json
    import pandas as pd
    import src.cachebuild as cachebuild

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    (cache / "_dates.json").write_text(json.dumps(dates))
    (cache / "AAA.json").write_text(json.dumps(
        {"open": [1.0, 2.0, 3.0, 4.0], "high": [1.0, 2.0, 3.0, 4.0],
         "low": [1.0, 2.0, 3.0, 4.0], "close": [1.0, 2.0, 3.0, 4.0],
         "volume": [10, 10, 10, 10]}))

    shallow = [3.0, 4.0]
    fetched = {"AAA": pd.DataFrame(
        {"Open": shallow, "High": shallow, "Low": shallow, "Close": shallow,
         "Volume": [10, 10]}, index=pd.to_datetime(dates[2:]))}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    summary = cachebuild.build(["AAA"], check_only=True, cache_dir=str(cache))
    assert any("would lose bars" in r for r in summary["would_refuse"])


def test_check_only_reports_a_conflict_instead_of_raising(tmp_path, monkeypatch):
    """A conflict is what --check exists to find, not a reason to abort."""
    import json
    import pandas as pd
    import src.cachebuild as cachebuild
    import src.pricecache as pricecache

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06"]
    (cache / "_dates.json").write_text(json.dumps(dates))
    (cache / "AAA.json").write_text(json.dumps(
        {"open": [10.0, 11.0], "high": [10.0, 11.0], "low": [10.0, 11.0],
         "close": [10.0, 11.0], "volume": [1, 1]}))
    pricecache.clear_cache()

    disagrees = [10.0, 99.0]
    fetched = {"AAA": pd.DataFrame(
        {"Open": disagrees, "High": disagrees, "Low": disagrees,
         "Close": disagrees, "Volume": [1, 1]}, index=pd.to_datetime(dates))}
    monkeypatch.setattr("src.data.fetch_prices", lambda *a, **k: fetched)

    summary = cachebuild.build(["AAA"], check_only=True, cache_dir=str(cache))
    assert any("contradict the cache" in r for r in summary["would_refuse"])
    assert summary["sessions"] == 2          # the rest of the report survives

    with pytest.raises(ValueError, match="contradicts the cache"):
        cachebuild.build(["AAA"], cache_dir=str(cache))


def test_nothing_is_left_half_written(tmp_path, monkeypatch):
    """Staged writes: no ticker file appears against a calendar that is not final."""
    import json
    import pandas as pd
    import src.cachebuild as cachebuild
    import src.pricecache as pricecache

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06"]
    closes = [1.0, 2.0]
    frame = pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                          "Close": closes, "Volume": [1, 1]},
                         index=pd.to_datetime(dates))
    monkeypatch.setattr("src.data.fetch_prices",
                        lambda *a, **k: {"AAA": frame, "BBB": frame})
    cachebuild.build(["AAA", "BBB"], cache_dir=str(cache))

    assert not list(cache.glob("*.tmp"))
    pricecache.clear_cache()
    calendar = json.loads((cache / "_dates.json").read_text())
    for ticker in ("AAA", "BBB"):
        got = pricecache.load_frame(ticker, str(cache))
        assert [d.strftime("%Y-%m-%d") for d in got.index] == calendar[-len(got):]
