"""Validation helpers: window construction, walk-forward split, bootstrap."""
import json
from datetime import date

import pytest

import src.pricecache as pricecache
import src.validate as validate


@pytest.fixture
def cache(tmp_path):
    # 120 consecutive weekday-ish sessions so 30-day windows have bars in them.
    import datetime as dt
    day, dates = dt.date(2026, 1, 1), []
    while len(dates) < 120:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    n = len(dates)
    for ticker, series in {"RISE": [100 * (1.005 ** i) for i in range(n)],
                           "SPY": [500.0] * n}.items():
        (tmp_path / f"{ticker}.json").write_text(json.dumps({
            "open": series, "high": series, "low": series,
            "close": series, "volume": [1000] * n}))
    pricecache.clear_cache()
    yield str(tmp_path)
    pricecache.clear_cache()


@pytest.fixture
def alerts(tmp_path):
    dates = json.loads((tmp_path / "_dates.json").read_text())
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({d: ["RISE"] for d in dates[::11]}))
    return str(path)


def test_non_overlapping_windows_share_no_sessions(cache):
    dates = pricecache.load_dates(cache)
    wins = validate.non_overlapping(date.fromisoformat(dates[0]),
                                    date.fromisoformat(dates[-1]), cache_dir=cache)
    assert len(wins) >= 2
    for (_, end_a), (start_b, _) in zip(wins, wins[1:]):
        assert start_b > end_a
        shared = set(pricecache.trading_days(start_b, wins[-1][1], cache)) & \
                 set(pricecache.trading_days(wins[0][0], end_a, cache))
        assert not shared


def test_rolling_windows_do_overlap(cache):
    dates = pricecache.load_dates(cache)
    wins = validate.rolling(date.fromisoformat(dates[0]),
                            date.fromisoformat(dates[-1]), cache_dir=cache)
    assert len(wins) > len(validate.non_overlapping(
        date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), cache_dir=cache))
    assert wins[1][0] < wins[0][1], "second window should start before the first ends"


def test_walk_forward_splits_chronologically_and_never_tunes_on_test(cache, alerts):
    dates = pricecache.load_dates(cache)
    wins = validate.rolling(date.fromisoformat(dates[0]),
                            date.fromisoformat(dates[-1]), cache_dir=cache)
    out = validate.walk_forward({"SIM_MIN_HOLD_SESSIONS": [0, 5]}, wins,
                                {"SIM_MIN_HOLD_SESSIONS": 0},
                                cache_dir=cache, alerts_path=alerts)
    assert out["train_windows"] and out["test_windows"]
    assert not set(map(tuple, out["train_windows"])) & set(map(tuple, out["test_windows"]))
    assert out["train_windows"][-1][0] < out["test_windows"][0][0]
    assert out["chosen"]["SIM_MIN_HOLD_SESSIONS"] in (0, 5)


def test_walk_forward_needs_enough_windows_to_split(cache, alerts):
    with pytest.raises(ValueError):
        validate.walk_forward({"SIM_MIN_HOLD_SESSIONS": [0]},
                              [(date(2026, 1, 1), date(2026, 1, 31))],
                              {"SIM_MIN_HOLD_SESSIONS": 0},
                              cache_dir=cache, alerts_path=alerts)


def test_bootstrap_of_a_setting_against_itself_is_centred_on_zero(cache, alerts):
    dates = pricecache.load_dates(cache)
    wins = validate.rolling(date.fromisoformat(dates[0]),
                            date.fromisoformat(dates[-1]), cache_dir=cache)
    b = validate.bootstrap({"SIM_MIN_HOLD_SESSIONS": 0}, {"SIM_MIN_HOLD_SESSIONS": 0},
                           wins, draws=200, cache_dir=cache, alerts_path=alerts)
    assert b["observed_mean_delta"] == pytest.approx(0.0)
    assert b["ci_low"] == pytest.approx(0.0) and b["ci_high"] == pytest.approx(0.0)
    assert b["windows_improved"] == 0 and b["windows_worsened"] == 0


def test_bootstrap_is_deterministic_for_a_seed(cache, alerts):
    dates = pricecache.load_dates(cache)
    wins = validate.rolling(date.fromisoformat(dates[0]),
                            date.fromisoformat(dates[-1]), cache_dir=cache)
    kw = dict(draws=200, cache_dir=cache, alerts_path=alerts)
    a = validate.bootstrap({"SIM_MIN_HOLD_SESSIONS": 5}, {"SIM_MIN_HOLD_SESSIONS": 0}, wins, seed=7, **kw)
    b = validate.bootstrap({"SIM_MIN_HOLD_SESSIONS": 5}, {"SIM_MIN_HOLD_SESSIONS": 0}, wins, seed=7, **kw)
    assert (a["ci_low"], a["ci_high"]) == (b["ci_low"], b["ci_high"])


def test_bootstrap_counts_add_up(cache, alerts):
    dates = pricecache.load_dates(cache)
    wins = validate.rolling(date.fromisoformat(dates[0]),
                            date.fromisoformat(dates[-1]), cache_dir=cache)
    b = validate.bootstrap({"SIM_MIN_HOLD_SESSIONS": 10}, {"SIM_MIN_HOLD_SESSIONS": 0},
                           wins, draws=200, cache_dir=cache, alerts_path=alerts)
    assert (b["windows_improved"] + b["windows_unchanged"] + b["windows_worsened"]
            == b["n_windows"] == len(b["per_window_delta"]))
    assert b["ci_low"] <= b["observed_mean_delta"] <= b["ci_high"]


class TestVerdictFollowsTheNumbers:
    """The verdict used to be fixed text that survived the numbers changing."""

    def test_consistent_only_when_nothing_got_worse(self):
        from src.validate import direction_phrase
        assert direction_phrase(4, 0, 8).startswith("Direction is consistent")
        assert not direction_phrase(4, 3, 8).startswith("Direction is consistent")

    def test_leans_positive_when_more_helped_than_hurt(self):
        from src.validate import direction_phrase
        assert direction_phrase(4, 3, 8).startswith("Direction leans positive")
        assert "helps in 4 of 8" in direction_phrase(4, 3, 8)

    def test_a_tie_is_called_a_tie(self):
        from src.validate import direction_phrase
        assert direction_phrase(3, 3, 8).startswith("Direction is a coin flip")

    def test_a_losing_change_is_called_negative(self):
        from src.validate import direction_phrase
        assert direction_phrase(2, 5, 8).startswith("Direction is negative")
