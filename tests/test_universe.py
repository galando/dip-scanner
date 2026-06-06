"""Tests for universe.py — S&P 500 ticker list."""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd

from src.universe import get_sp500_tickers, FALLBACK_TICKERS


class TestGetSP500Tickers:
    """Scenario: Market regime computed once per run (universe must work)."""

    def test_returns_list_of_strings(self):
        """Result is a list of non-empty ticker strings."""
        tickers = get_sp500_tickers()
        assert isinstance(tickers, list)
        assert len(tickers) > 0
        for t in tickers[:10]:
            assert isinstance(t, str)
            assert len(t) > 0

    @patch("src.universe.pd.read_html")
    def test_wikipedia_fetch_success(self, mock_read_html):
        """Wikipedia table is parsed correctly."""
        mock_df = pd.DataFrame({"Symbol": ["AAPL", "MSFT", "GOOGL"]})
        mock_read_html.return_value = [mock_df]
        tickers = get_sp500_tickers()
        assert tickers == ["AAPL", "MSFT", "GOOGL"]

    @patch("src.universe.pd.read_html", side_effect=Exception("network error"))
    def test_fallback_on_network_error(self, mock_read_html):
        """Falls back to static list when Wikipedia fails."""
        tickers = get_sp500_tickers()
        assert isinstance(tickers, list)
        assert len(tickers) > 0
        assert "AAPL" in tickers

    def test_fallback_list_is_reasonable(self):
        """Static fallback list contains well-known S&P 500 names."""
        assert "AAPL" in FALLBACK_TICKERS
        assert "MSFT" in FALLBACK_TICKERS
        assert len(FALLBACK_TICKERS) >= 50
