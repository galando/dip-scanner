"""Replay mechanics: signal timing, slot discipline, and the performance maths."""
import json
from datetime import date

import pytest

import config
import src.pricecache as pricecache
import src.replay as replay


@pytest.fixture
def cache(tmp_path):
    # 20 sessions with a two-day gap at 10-11, so rolling a signal forward onto
    # the next session is exercised the way a weekend exercises it.
    dates = [f"2026-01-{d:02d}" for d in list(range(2, 10)) + list(range(12, 24))]
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    n = len(dates)
    for ticker, closes in {
        "AAA": [100 + i for i in range(n)],   # rises every day
        "BBB": [50 - i for i in range(n)],    # falls every day
        "CCC": [30] * n,                      # flat: no exit rule ever fires
        "SPY": [500 + 5 * i for i in range(n)],
    }.items():
        (tmp_path / f"{ticker}.json").write_text(json.dumps({
            "open": closes, "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes], "close": closes,
            "volume": [1000] * len(closes),
        }))
    return str(tmp_path)


def sessions(cache):
    return pricecache.trading_days(date(2026, 1, 2), date(2026, 1, 23), cache)


def test_weekend_alert_rolls_to_the_next_session(cache):
    out = replay._actionable({"2026-01-10": ["AAA"]}, sessions(cache))
    assert out == {"2026-01-12": ["AAA"]}


def test_alert_on_a_trading_day_stays_put(cache):
    out = replay._actionable({"2026-01-06": ["AAA"]}, sessions(cache))
    assert out == {"2026-01-06": ["AAA"]}


def test_stale_alerts_are_dropped_not_dragged_forward(cache):
    out = replay._actionable({"2025-11-01": ["AAA"]}, sessions(cache))
    assert out == {}


def test_an_alert_fired_before_the_window_belongs_to_the_previous_window(cache):
    """Otherwise adjacent windows share entries and stop being independent."""
    later = pricecache.trading_days(date(2026, 1, 12), date(2026, 1, 23), cache)
    assert replay._actionable({"2026-01-09": ["AAA"]}, later) == {}
    # ...but the same alert is picked up by the window it actually fired in.
    earlier = pricecache.trading_days(date(2026, 1, 2), date(2026, 1, 9), cache)
    assert replay._actionable({"2026-01-09": ["AAA"]}, earlier) == {"2026-01-09": ["AAA"]}


def test_alerts_landing_on_the_same_session_merge_without_duplicates(cache):
    out = replay._actionable(
        {"2026-01-10": ["AAA"], "2026-01-11": ["AAA", "BBB"]}, sessions(cache)
    )
    assert out == {"2026-01-12": ["AAA", "BBB"]}


def test_most_oversold_ranks_first(cache):
    prices = pricecache.fetch_prices_asof(["AAA", "BBB"], date(2026, 1, 21), cache)
    # BBB falls every day, AAA rises every day, so BBB is the weaker of the two.
    assert replay._rank_signals(["AAA", "BBB"], prices)[0] == "BBB"


def test_replay_buys_the_days_signals_and_closes_the_book(cache, tmp_path, monkeypatch):
    alerts = tmp_path / "alerts.json"
    alerts.write_text(json.dumps({"2026-01-19": ["CCC"]}))
    state = replay.replay(date(2026, 1, 2), date(2026, 1, 21),
                          cache_dir=cache, alerts_path=str(alerts))
    assert state["status"] == "DONE"
    assert state["positions"] == []
    assert [c["ticker"] for c in state["closed"]] == ["CCC"]
    assert state["closed"][0]["entry_date"] == "2026-01-19"
    assert state["closed"][0]["exit_date"] == "2026-01-21"
    assert "month ended" in state["closed"][0]["sell_reason"]


def test_free_slots_cap_how_many_signals_are_taken(cache, tmp_path, monkeypatch):
    alerts = tmp_path / "alerts.json"
    alerts.write_text(json.dumps({"2026-01-19": ["AAA", "BBB"]}))
    monkeypatch.setattr(config, "SIM_MAX_POSITIONS", 1)
    state = replay.replay(date(2026, 1, 2), date(2026, 1, 21),
                          cache_dir=cache, alerts_path=str(alerts))
    # Only one slot, so only the more oversold name is bought.
    assert [c["ticker"] for c in state["closed"]] == ["BBB"]


def test_a_signal_with_no_cached_prices_is_reported_not_silently_skipped(cache, tmp_path):
    alerts = tmp_path / "alerts.json"
    alerts.write_text(json.dumps({"2026-01-19": ["ZZZ"]}))
    state = replay.replay(date(2026, 1, 2), date(2026, 1, 21),
                          cache_dir=cache, alerts_path=str(alerts))
    assert state["missing_prices"] == ["ZZZ"]
    assert state["closed"] == []


def test_performance_separates_capital_from_turnover(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SIM_MAX_POSITIONS", 2)
    monkeypatch.setattr(config, "SIM_CASH_PER_STOCK", 1000.0)
    state = {
        "start_date": "2026-01-02", "end_date": "2026-01-21",
        "closed": [
            {"pnl": 100.0, "cost_basis": 1000.0},
            {"pnl": -50.0, "cost_basis": 1000.0},
            {"pnl": 25.0, "cost_basis": 1000.0},
        ],
    }
    perf = replay.performance(state, cache_dir=cache)
    assert perf["trades"] == 3 and perf["winners"] == 2
    assert perf["capital"] == 2000.0 and perf["turnover"] == 3000.0
    assert perf["return_on_capital_pct"] == pytest.approx(3.75)
    assert perf["return_on_turnover_pct"] == pytest.approx(2.5)
    assert perf["benchmark_pct"] == pytest.approx(17.0)


def test_performance_without_a_benchmark_series(cache, tmp_path):
    state = {"start_date": "2026-01-02", "end_date": "2026-01-21", "closed": []}
    perf = replay.performance(state, benchmark="NOPE", cache_dir=cache)
    assert perf["benchmark_pct"] is None
