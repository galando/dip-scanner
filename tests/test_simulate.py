"""Tests for simulate.py — monthly paper-trading simulation.

Mocks yfinance (data/regime), universe, and Telegram, mirroring test_scanner.py.
"""
from datetime import date
from unittest.mock import patch

import pandas as pd

import config
import src.simulate as simulate


def _make_price_df(closes, start="2023-06-01"):
    dates = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=dates, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1_000_000,
    })


def _make_passing_prices():
    """Deep dip + stabilization — passes gate 2 (same shape as test_scanner)."""
    base, low = 100.0, 60.0
    prices = [base] * 150
    prices += [base - (base - low) * (i + 1) / 30 for i in range(30)]
    prices += [low] * 5
    prices += [low + i * 0.15 for i in range(45)]
    return _make_price_df(prices)


def _make_spy_risk_on():
    return _make_price_df([440 + i * 0.1 for i in range(250)])


def _fundamentals():
    return {
        "returnOnEquity": 0.22, "operatingMargins": 0.18, "debtToEquity": 80,
        "marketCap": 200_000_000_000, "shortName": "Netflix Inc",
        "earningsGrowth": 0.05, "revenueGrowth": 0.03,
    }


# --------------------------------------------------------------------------- #
# Day 1 initialization
# --------------------------------------------------------------------------- #
@patch("src.simulate.os.environ.get", return_value="")  # no telegram creds -> print
@patch("src.simulate.data")
@patch("src.regime.data")
@patch("src.simulate.universe")
def test_initialize_buys_passing_stock(mock_universe, mock_regime_data, mock_data, _env, tmp_path):
    mock_universe.get_sp500_tickers.return_value = ["NFLX"]
    mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
    mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
    mock_data.fetch_fundamentals.return_value = _fundamentals()

    sp = str(tmp_path / "sim.json")
    state = simulate.run(today=date(2026, 6, 6), state_path=sp)

    assert state["status"] == "RUNNING"
    assert state["start_date"] == "2026-06-06"
    assert state["end_date"] == "2026-06-30"
    assert len(state["positions"]) == 1
    pos = state["positions"][0]
    assert pos["ticker"] == "NFLX"
    assert pos["cost_basis"] == config.SIM_CASH_PER_STOCK
    assert pos["entry_reason"]["drawdown_pct"] < 0  # captured the "why"


@patch("src.simulate.os.environ.get", return_value="")
@patch("src.simulate.data")
@patch("src.regime.data")
@patch("src.simulate.universe")
def test_max_positions_capped(mock_universe, mock_regime_data, mock_data, _env, tmp_path):
    tickers = [f"T{i}" for i in range(15)]
    mock_universe.get_sp500_tickers.return_value = tickers
    mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
    mock_data.fetch_prices.return_value = {t: _make_passing_prices() for t in tickers}
    mock_data.fetch_fundamentals.return_value = _fundamentals()

    state = simulate.run(today=date(2026, 6, 6), state_path=str(tmp_path / "s.json"))
    assert len(state["positions"]) == config.SIM_MAX_POSITIONS


@patch("src.simulate.os.environ.get", return_value="")
@patch("src.simulate.data")
@patch("src.regime.data")
@patch("src.simulate.universe")
def test_initialize_with_no_candidates(mock_universe, mock_regime_data, mock_data, _env, tmp_path):
    """No stock passes -> start with zero positions, still RUNNING."""
    mock_universe.get_sp500_tickers.return_value = ["FLAT"]
    mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
    mock_data.fetch_prices.return_value = {"FLAT": _make_price_df([100.0] * 250)}
    mock_data.fetch_fundamentals.return_value = _fundamentals()

    state = simulate.run(today=date(2026, 6, 6), state_path=str(tmp_path / "s.json"))
    assert state["status"] == "RUNNING"
    assert state["positions"] == []


# --------------------------------------------------------------------------- #
# Exit rules
# --------------------------------------------------------------------------- #
def _pos(entry=100.0):
    return {
        "ticker": "X", "name": "X Co", "entry_price": entry,
        "entry_date": "2026-06-06", "shares": 1000.0 / entry,
        "cost_basis": 1000.0, "entry_reason": {},
    }


def test_exit_stop_loss():
    df = _make_price_df([100.0] * 30 + [85.0])  # -15% from entry 100
    sell, reason, price = simulate.evaluate_exit(_pos(100.0), df, config)
    assert sell and "stop-loss" in reason.lower()


def test_exit_take_profit():
    df = _make_price_df([100.0] * 30 + [115.0])  # +15%
    sell, reason, price = simulate.evaluate_exit(_pos(100.0), df, config)
    assert sell and "target" in reason.lower()


def test_exit_hold_in_band():
    # Entry 100, last 96 (-4%: within stop/target band), recent low (94) a few
    # days back so it's not a fresh low, gentle moves, RSI moderate -> hold.
    df = _make_price_df([100.0] * 200 + [98, 96, 94, 95, 96, 96, 95, 96])
    sell, reason, price = simulate.evaluate_exit(_pos(100.0), df, config)
    assert not sell


# --------------------------------------------------------------------------- #
# Cadence + lifecycle
# --------------------------------------------------------------------------- #
def test_milestones_every_three_days():
    ms = simulate._milestones(date(2026, 6, 6), date(2026, 6, 30), 3)
    assert date(2026, 6, 9) in ms
    assert date(2026, 6, 27) in ms
    assert date(2026, 6, 30) not in ms  # end excluded


@patch("src.simulate.os.environ.get", return_value="")
@patch("src.simulate.data")
@patch("src.regime.data")
@patch("src.simulate.universe")
def test_final_day_closes_and_marks_done(mock_universe, mock_regime_data, mock_data, _env, tmp_path):
    mock_universe.get_sp500_tickers.return_value = ["NFLX"]
    mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
    mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
    mock_data.fetch_fundamentals.return_value = _fundamentals()
    sp = str(tmp_path / "s.json")

    simulate.run(today=date(2026, 6, 6), state_path=sp)             # init
    state = simulate.run(today=date(2026, 6, 30), state_path=sp)    # month end

    assert state["status"] == "DONE"
    assert state["positions"] == []
    assert len(state["closed"]) >= 1


@patch("src.simulate.os.environ.get", return_value="")
@patch("src.simulate.data")
@patch("src.regime.data")
@patch("src.simulate.universe")
def test_done_state_is_noop(mock_universe, mock_regime_data, mock_data, _env, tmp_path):
    mock_universe.get_sp500_tickers.return_value = ["NFLX"]
    mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
    mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
    mock_data.fetch_fundamentals.return_value = _fundamentals()
    sp = str(tmp_path / "s.json")

    simulate.run(today=date(2026, 6, 6), state_path=sp)
    simulate.run(today=date(2026, 6, 30), state_path=sp)
    before = simulate.load_sim(sp)
    after = simulate.run(today=date(2026, 7, 1), state_path=sp)
    assert after["status"] == "DONE"
    assert after["closed"] == before["closed"]


# --------------------------------------------------------------------------- #
# Telegram composition smoke tests
# --------------------------------------------------------------------------- #
def test_compose_start_includes_reason():
    pos = {
        "ticker": "NFLX", "name": "Netflix Inc", "entry_price": 60.0,
        "shares": 16.6, "cost_basis": 1000.0,
        "entry_reason": {"drawdown_pct": -40.0, "rsi": 28.0,
                         "stabilization_signals": ["higher low"], "roe": 22.0},
    }
    msg = telegram_compose_start([pos])
    assert "NFLX" in msg and "40%" in msg and "RSI 28" in msg


def telegram_compose_start(positions):
    import src.telegram as t
    return t.compose_simulation_start(positions, "2026-06-06", "2026-06-30", 1000.0, 10, "RISK_ON")


def test_compose_summary_bottom_line():
    import src.telegram as t
    closed = [{"ticker": "NFLX", "entry_price": 60.0, "exit_price": 69.0,
               "pnl_pct": 15.0, "pnl": 150.0, "sell_reason": "target hit"}]
    msg = t.compose_summary(closed, [], "2026-06-06", "2026-06-30", 1000.0, 1150.0, 150.0)
    assert "NFLX" in msg and "TOTAL" in msg and "+15.0%" in msg
