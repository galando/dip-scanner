"""Holding a past book forward instead of selling it."""
import json
from datetime import date

import pytest

import src.whatif as whatif


@pytest.fixture
def cache(tmp_path):
    dates = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    (tmp_path / "_dates.json").write_text(json.dumps(dates))
    for ticker, closes in {"AAA": [10, 10, 12, 20], "BBB": [5, 5, 4, 4]}.items():
        (tmp_path / f"{ticker}.json").write_text(json.dumps({
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [1] * len(closes),
        }))
    return str(tmp_path)


@pytest.fixture
def state():
    return {
        "positions": [],
        "closed": [
            {"ticker": "AAA", "entry_date": "2026-01-05", "exit_date": "2026-01-06",
             "entry_price": 10.0, "shares": 100.0, "cost_basis": 1000.0},
            {"ticker": "BBB", "entry_date": "2026-01-06", "exit_date": "2026-01-07",
             "entry_price": 4.0, "shares": 250.0, "cost_basis": 1000.0},
        ],
    }


def test_only_positions_actually_held_that_day_are_counted(state):
    held = whatif.positions_open_on(state, date(2026, 1, 5))
    assert [p["ticker"] for p in held] == ["AAA"]


def test_a_position_sold_that_same_day_is_no_longer_held(state):
    held = whatif.positions_open_on(state, date(2026, 1, 6))
    assert [p["ticker"] for p in held] == ["BBB"]


def test_holding_forward_marks_at_the_later_close(state, cache):
    out = whatif.hold_forward(state, date(2026, 1, 5), date(2026, 1, 7), cache_dir=cache)
    assert out["until"] == "2026-01-07"
    assert out["rows"][0]["ticker"] == "AAA"
    assert out["rows"][0]["current_price"] == 20.0
    assert out["rows"][0]["price_pct"] == pytest.approx(100.0)
    assert out["pnl"] == pytest.approx(1000.0)
    assert out["pnl_pct"] == pytest.approx(100.0)


def test_defaults_to_the_last_cached_session(state, cache):
    out = whatif.hold_forward(state, date(2026, 1, 5), cache_dir=cache)
    assert out["until"] == "2026-01-07"


def test_total_return_uses_the_adjusted_entry_bar(state, cache):
    # BBB's entry was recorded live at 4.00 but the cached 6 Jan bar is 4.00 too,
    # so both figures agree; AAA's recorded entry (10.00) matches its bar as well.
    out = whatif.hold_forward(state, date(2026, 1, 6), date(2026, 1, 7), cache_dir=cache)
    row = out["rows"][0]
    assert row["price_pct"] == pytest.approx(row["total_return_pct"])


def test_a_position_with_no_cached_prices_is_reported_not_dropped(tmp_path):
    """A basket total that quietly describes a subset reads as the whole book."""
    import json
    import src.pricecache as pricecache
    import src.whatif as whatif

    cache = tmp_path
    dates = ["2026-01-05", "2026-01-06"]
    (cache / "_dates.json").write_text(json.dumps(dates))
    (cache / "AAA.json").write_text(json.dumps(
        {"open": [10.0, 11.0], "high": [10.0, 11.0], "low": [10.0, 11.0],
         "close": [10.0, 11.0], "volume": [1, 1]}))
    pricecache.clear_cache()

    state = {"positions": [], "closed": [
        {"ticker": "AAA", "entry_price": 10.0, "entry_date": "2026-01-05",
         "exit_date": "2026-01-06", "shares": 100.0, "cost_basis": 1000.0},
        {"ticker": "ZZZ", "entry_price": 5.0, "entry_date": "2026-01-05",
         "exit_date": "2026-01-06", "shares": 200.0, "cost_basis": 1000.0},
    ]}

    out = whatif.hold_forward(state, date(2026, 1, 5), date(2026, 1, 6),
                              cache_dir=str(cache))
    assert [r["ticker"] for r in out["rows"]] == ["AAA"]
    assert out["unpriced"] == ["ZZZ"]
    assert out["total_cost"] == 1000.0        # and the total says so, via unpriced


def test_a_row_without_a_cost_basis_uses_the_runs_slot_size_not_live_config(tmp_path):
    """Month-end rows written before cost_basis was recorded must not be rescaled."""
    import json
    import types
    import src.pricecache as pricecache
    import src.whatif as whatif

    cache = tmp_path
    (cache / "_dates.json").write_text(json.dumps(["2026-01-05", "2026-01-06"]))
    (cache / "AAA.json").write_text(json.dumps(
        {"open": [10.0, 11.0], "high": [10.0, 11.0], "low": [10.0, 11.0],
         "close": [10.0, 11.0], "volume": [1, 1]}))
    pricecache.clear_cache()

    state = {"cash_per_stock": 1000.0, "positions": [], "closed": [
        {"ticker": "AAA", "entry_price": 10.0, "entry_date": "2026-01-05",
         "exit_date": "2026-01-06", "shares": 100.0},      # no cost_basis
    ]}
    cfg = types.SimpleNamespace(SIM_CASH_PER_STOCK=2000.0)  # raised since that run

    out = whatif.hold_forward(state, date(2026, 1, 5), date(2026, 1, 6),
                              cfg=cfg, cache_dir=str(cache))
    assert out["total_cost"] == 1000.0
    assert out["rows"][0]["pnl"] == 100.0                   # 100 sh x $11 - $1000
