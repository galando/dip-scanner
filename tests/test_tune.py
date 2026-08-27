"""Tuning helpers: config overrides, the exit-free hold curve, and robust picking."""
import json
from datetime import date

import pytest

import config
import src.pricecache as pricecache
import src.tune as tune


@pytest.fixture
def cache(tmp_path):
    # 30 sessions. RISE gains 1% a day; FALL loses 1% a day.
    dates = [f"2026-01-{d:02d}" for d in range(2, 32)]
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    n = len(dates)
    for ticker, series in {
        "RISE": [100 * (1.01 ** i) for i in range(n)],
        "FALL": [100 * (0.99 ** i) for i in range(n)],
        "SPY": [500.0] * n,
    }.items():
        (tmp_path / f"{ticker}.json").write_text(json.dumps({
            "open": series, "high": series, "low": series,
            "close": series, "volume": [1000] * n,
        }))
    pricecache.clear_cache()
    yield str(tmp_path)
    pricecache.clear_cache()


@pytest.fixture
def alerts(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"2026-01-05": ["RISE", "FALL"]}))
    return str(path)


def test_overrides_start_from_the_real_config():
    cfg = tune.with_overrides(SIM_RSI_EXIT=75)
    assert cfg.SIM_RSI_EXIT == 75
    assert cfg.SIM_MAX_POSITIONS == config.SIM_MAX_POSITIONS
    assert cfg.MIN_DRAWDOWN == config.MIN_DRAWDOWN


def test_overrides_reject_a_setting_that_does_not_exist():
    with pytest.raises(KeyError):
        tune.with_overrides(SIM_RSI_EXTI=75)


def test_hold_curve_measures_the_forward_move(cache, alerts):
    rows = tune.hold_curve([(date(2026, 1, 2), date(2026, 1, 31))],
                           horizons=range(1, 6), cache_dir=cache, alerts_path=alerts)
    five = next(r for r in rows if r["days"] == 5)
    assert five["signals"] == 2                       # RISE and FALL
    # +5.1% and -4.9% average out to roughly +0.1%.
    assert five["mean_pct"] == pytest.approx(0.1, abs=0.1)
    assert five["win_rate_pct"] == pytest.approx(50.0)


def test_hold_curve_rows_grow_with_the_holding_period(cache, tmp_path):
    path = tmp_path / "rise_only.json"
    path.write_text(json.dumps({"2026-01-05": ["RISE"]}))
    rows = tune.hold_curve([(date(2026, 1, 2), date(2026, 1, 31))],
                           horizons=range(1, 11), cache_dir=cache, alerts_path=str(path))
    means = [r["mean_pct"] for r in rows]
    assert means == sorted(means)
    assert means[-1] == pytest.approx(10.46, abs=0.05)   # 1.01**10 - 1


def test_balanced_curve_uses_one_signal_set_for_every_horizon(cache, tmp_path):
    # Two alerts, the second too late to have 10 sessions of forward data.
    path = tmp_path / "two.json"
    path.write_text(json.dumps({"2026-01-05": ["RISE"], "2026-01-29": ["RISE"]}))
    balanced = tune.hold_curve([(date(2026, 1, 2), date(2026, 1, 31))],
                               horizons=range(1, 11), balanced=True,
                               cache_dir=cache, alerts_path=str(path))
    ragged = tune.hold_curve([(date(2026, 1, 2), date(2026, 1, 31))],
                             horizons=range(1, 11), balanced=False,
                             cache_dir=cache, alerts_path=str(path))
    assert {r["signals"] for r in balanced} == {1}
    assert len({r["signals"] for r in ragged}) > 1


def test_evaluate_reports_each_window_separately(cache, alerts):
    out = tune.evaluate({"SIM_RSI_EXIT": 101}, [(date(2026, 1, 2), date(2026, 1, 20)),
                                                (date(2026, 1, 12), date(2026, 1, 31))],
                        cache_dir=cache, alerts_path=alerts)
    assert len(out["windows"]) == 2
    assert out["worst_pct"] <= out["mean_pct"]
    assert out["settings"] == {"SIM_RSI_EXIT": 101}


def test_sweep_covers_the_whole_grid(cache, alerts):
    results = tune.sweep({"SIM_RSI_EXIT": [60, 101], "SIM_STOP_LOSS_PCT": [10, 12]},
                         [(date(2026, 1, 2), date(2026, 1, 31))],
                         cache_dir=cache, alerts_path=alerts)
    assert len(results) == 4
    assert all(set(r["settings"]) == {"SIM_RSI_EXIT", "SIM_STOP_LOSS_PCT"} for r in results)


def test_sweep_ranks_worst_window_first(cache, alerts):
    results = tune.sweep({"SIM_RSI_EXIT": [60, 101]},
                         [(date(2026, 1, 2), date(2026, 1, 20)),
                          (date(2026, 1, 12), date(2026, 1, 31))],
                         cache_dir=cache, alerts_path=alerts)
    worst = [r["worst_excess_pct"] for r in results]
    assert worst == sorted(worst, reverse=True)


def test_pick_keeps_only_settings_that_improve_every_window():
    baseline = {"SIM_RSI_EXIT": 60}
    results = [
        {"settings": baseline,
         "windows": [{"start": "a", "return_pct": 1.0}, {"start": "b", "return_pct": 2.0}]},
        {"settings": {"SIM_RSI_EXIT": 70},          # better in both
         "windows": [{"start": "a", "return_pct": 3.0}, {"start": "b", "return_pct": 4.0}]},
        {"settings": {"SIM_RSI_EXIT": 80},          # much better in one, worse in the other
         "windows": [{"start": "a", "return_pct": 9.0}, {"start": "b", "return_pct": 1.0}]},
    ]
    out = tune.pick(results, baseline)
    assert out["baseline"]["settings"] == baseline
    chosen = [r["settings"]["SIM_RSI_EXIT"] for r in out["robust"]]
    assert 70 in chosen and 80 not in chosen


def test_rule_labels_come_from_the_english_marker():
    assert tune._rule_of("סטופ-לוס: ירדה -13% / stop-loss, down -13% from entry") == "stop-loss"
    assert tune._rule_of("יעד רווח / target hit, up +12.4% from entry") == "take-profit"
    assert tune._rule_of("ההתאוששות הושלמה / bounce done, RSI back to 64") == "bounce done (RSI)"
    assert tune._rule_of("הדיפ לא נגמר ... (Dip not over: down -6% ...)") == "thesis break"
    assert tune._rule_of("החודש הסתיים — month ended — book closed") == "month ended (not a rule)"
    assert tune._rule_of("something else entirely") == "other"


def test_exit_cost_compares_the_sale_against_leaving_it_alone(cache, tmp_path):
    path = tmp_path / "rise.json"
    path.write_text(json.dumps({"2026-01-05": ["RISE"]}))
    rows = tune.exit_cost([(date(2026, 1, 2), date(2026, 1, 31))],
                          cache_dir=cache, alerts_path=str(path))
    assert rows, "RISE should have been bought and sold"
    row = rows[0]
    # RISE climbs monotonically, so any early exit gives up the rest of the run.
    assert row["left_behind_pct"] > 0
    assert row["if_held_pct"] > row["realised_pct"]


def test_a_position_closed_at_month_end_leaves_nothing_behind(cache, tmp_path):
    path = tmp_path / "rise.json"
    path.write_text(json.dumps({"2026-01-05": ["RISE"]}))
    rows = tune.exit_cost([(date(2026, 1, 2), date(2026, 1, 31))], cfg=tune.with_overrides(
        SIM_RSI_EXIT=101, SIM_TAKE_PROFIT_PCT=tune.OFF,
        SIM_STOP_LOSS_PCT=tune.OFF, SIM_THESIS_BREAK_MIN_LOSS_PCT=tune.OFF),
        cache_dir=cache, alerts_path=str(path))
    assert [r["rule"] for r in rows] == ["month ended (not a rule)"]
    assert rows[0]["left_behind_pct"] == pytest.approx(0.0, abs=1e-9)
