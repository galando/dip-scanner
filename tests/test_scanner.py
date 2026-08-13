"""Tests for scanner.py — main orchestrator.

Integration tests with mocked yfinance and Telegram API.
Maps to Scenarios 1, 6, 7 from intent.md.
"""
import pytest
from unittest.mock import patch, MagicMock, call
import pandas as pd
import numpy as np

from src.scanner import run_scan


def _make_price_df(closes, start="2024-01-01"):
    """Build OHLCV DataFrame from close prices."""
    dates = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=dates, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1_000_000,
    })


def _make_passing_prices():
    """Price data that passes gate 2 (deep dip + stabilization)."""
    base = 100.0
    low = 60.0  # 40% drawdown
    prices = []
    for i in range(150): prices.append(base)
    for i in range(30): prices.append(base - (base - low) * (i + 1) / 30)
    for i in range(5): prices.append(low)
    for i in range(45): prices.append(low + i * 0.15)
    return _make_price_df(prices, start="2023-06-01")


def _make_spy_risk_on():
    """SPY data for RISK_ON regime."""
    prices = [440 + i * 0.1 for i in range(250)]
    return _make_price_df(prices)


def _make_fundamentals_passing():
    return {
        "returnOnEquity": 0.22,
        "operatingMargins": 0.18,
        "debtToEquity": 80,
        "marketCap": 200_000_000_000,
        "shortName": "Netflix Inc",
        "earningsGrowth": 0.05,
        "revenueGrowth": 0.03,
    }


def _patches():
    """Common patches for scanner tests. Patch at the import site.

    regime.py imports data as `src.data`, so patch src.regime.data too.
    """
    return [
        patch("src.scanner.state"),
        patch("src.scanner.telegram"),
        patch("src.scanner.data"),
        patch("src.regime.data"),
        patch("src.scanner.universe"),
    ]


class TestRunScan:
    """Scenario 1: Stock passes all four gates and triggers alert."""

    @patch("src.scanner.os.environ.get", return_value="fake_value")
    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_passing_stock_sends_alert(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state, mock_env):
        """Stock passing all gates triggers a Telegram alert."""
        mock_universe.get_sp500_tickers.return_value = ["NFLX"]

        # Regime data (SPY) via regime.data
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}

        # Scanner price data (NFLX)
        mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
        mock_data.fetch_fundamentals.return_value = _make_fundamentals_passing()

        mock_state.load_state.return_value = {}
        mock_state.is_recently_alerted.return_value = False
        mock_telegram.send_alert.return_value = True
        # MagicMock iterates as empty; the send loop needs a real chat id list
        mock_telegram.get_chat_ids.return_value = ["12345"]

        run_scan()

        mock_telegram.send_alert.assert_called_once()
        # Verify compose_alert was called with NFLX
        compose_call = mock_telegram.compose_alert.call_args
        assert compose_call[0][0] == "NFLX"

    @patch("src.scanner.os.environ.get", return_value="fake_value")
    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_daily_cap_limits_alerts_to_best(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state, mock_env):
        """With more passers than MAX_ALERTS_PER_DAY, only the top-N are sent."""
        import config as cfg
        n_tickers = cfg.MAX_ALERTS_PER_DAY + 3
        tickers = [f"TK{i}" for i in range(n_tickers)]
        mock_universe.get_sp500_tickers.return_value = tickers
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
        mock_data.fetch_prices.return_value = {t: _make_passing_prices() for t in tickers}
        mock_data.fetch_fundamentals.return_value = _make_fundamentals_passing()

        mock_state.load_state.return_value = {}
        mock_state.is_recently_alerted.return_value = False
        mock_telegram.send_alert.return_value = True
        mock_telegram.get_chat_ids.return_value = ["12345"]

        run_scan()

        assert mock_telegram.compose_alert.call_count == cfg.MAX_ALERTS_PER_DAY
        assert mock_telegram.send_alert.call_count == cfg.MAX_ALERTS_PER_DAY
        # Every sent alert carries its score and rank
        for c in mock_telegram.compose_alert.call_args_list:
            details = c[0][3]
            assert "score" in details and details["rank"] is not None

    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_dedup_suppresses_repeat(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state):
        """Scenario 6: Dedup suppresses repeat alerts."""
        mock_universe.get_sp500_tickers.return_value = ["NFLX"]
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
        mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
        mock_data.fetch_fundamentals.return_value = _make_fundamentals_passing()

        mock_state.load_state.return_value = {"NFLX": "2024-06-01"}
        mock_state.is_recently_alerted.return_value = True

        run_scan()

        mock_telegram.send_alert.assert_not_called()

    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_yfinance_failure_graceful(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state):
        """Scenario 7: yfinance failure on individual ticker is handled gracefully."""
        mock_universe.get_sp500_tickers.return_value = ["BADTICKER", "NFLX"]
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
        mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
        mock_data.fetch_fundamentals.return_value = _make_fundamentals_passing()

        mock_state.load_state.return_value = {}
        mock_state.is_recently_alerted.return_value = False

        # Should complete without raising
        run_scan()

    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_no_tickers_completes_cleanly(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state):
        """Empty universe completes without error."""
        mock_universe.get_sp500_tickers.return_value = []
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_spy_risk_on()}
        mock_data.fetch_prices.return_value = {}

        run_scan()

        mock_telegram.send_alert.assert_not_called()

    @patch("src.scanner.config")
    @patch("src.scanner.state")
    @patch("src.scanner.telegram")
    @patch("src.scanner.data")
    @patch("src.regime.data")
    @patch("src.scanner.universe")
    def test_suppress_in_risk_off_mode(self, mock_universe, mock_regime_data, mock_data, mock_telegram, mock_state, mock_cfg):
        """SUPPRESS_IN_RISK_OFF=True skips all alerts in RISK_OFF."""
        mock_universe.get_sp500_tickers.return_value = ["NFLX"]

        # SPY below 200dma -> RISK_OFF
        spy_prices = [500 - i * 0.3 for i in range(250)]
        mock_regime_data.fetch_prices.return_value = {"SPY": _make_price_df(spy_prices)}
        mock_data.fetch_prices.return_value = {"NFLX": _make_passing_prices()}
        mock_data.fetch_fundamentals.return_value = _make_fundamentals_passing()

        mock_state.load_state.return_value = {}
        mock_state.is_recently_alerted.return_value = False

        mock_cfg.SUPPRESS_IN_RISK_OFF = True
        mock_cfg.DEDUP_DAYS = 10

        run_scan()

        mock_telegram.send_alert.assert_not_called()
