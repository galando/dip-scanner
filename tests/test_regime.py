"""Tests for regime.py — SPY vs 200-day MA regime detection."""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from src.regime import compute_regime, RISK_ON, RISK_OFF


def _make_spy_df(prices, start="2023-01-01"):
    """Build SPY-like price DataFrame."""
    dates = pd.bdate_range(start, periods=len(prices))
    close = pd.Series(prices, index=dates, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.005, "Low": close * 0.995,
        "Close": close, "Volume": 100_000_000,
    })


class TestComputeRegime:
    """Scenario 11: Market regime computed once per run."""

    @patch("src.regime.data")
    def test_spy_above_200dma_risk_on(self, mock_data):
        """SPY close above 200dma returns RISK_ON."""
        # 300 prices around 450 (above any reasonable 200dma)
        prices = [440 + i * 0.1 for i in range(250)]
        spy_df = _make_spy_df(prices)
        mock_data.fetch_prices.return_value = {"SPY": spy_df}

        regime = compute_regime()
        assert regime == RISK_ON

    @patch("src.regime.data")
    def test_spy_below_200dma_risk_off(self, mock_data):
        """SPY close below 200dma returns RISK_OFF."""
        # Start high, then drop steeply so last close is below 200dma
        prices = [500 - i * 0.3 for i in range(250)]
        spy_df = _make_spy_df(prices)
        mock_data.fetch_prices.return_value = {"SPY": spy_df}

        regime = compute_regime()
        assert regime == RISK_OFF

    @patch("src.regime.data")
    def test_regime_returns_string_constant(self, mock_data):
        """Regime is one of two string constants."""
        prices = [450.0] * 250
        spy_df = _make_spy_df(prices)
        mock_data.fetch_prices.return_value = {"SPY": spy_df}

        regime = compute_regime()
        assert regime in (RISK_ON, RISK_OFF)

    @patch("src.regime.data")
    def test_regime_handles_missing_spy(self, mock_data):
        """Missing SPY data defaults to RISK_OFF (safe default)."""
        mock_data.fetch_prices.return_value = {}

        regime = compute_regime()
        assert regime == RISK_OFF
