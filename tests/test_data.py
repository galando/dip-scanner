"""Tests for data.py — yfinance wrapper with graceful error handling."""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from src.data import fetch_prices, fetch_fundamentals


class TestFetchPrices:
    """Scenario 7: Scanner handles yfinance failure on individual ticker."""

    @patch("src.data.yf")
    def test_batch_download_returns_dict_of_dfs(self, mock_yf):
        """Successful batch download returns {ticker: DataFrame}."""
        dates = pd.bdate_range("2024-01-01", periods=5)
        # Build per-ticker DataFrames then concat with MultiIndex
        aapl = pd.DataFrame({
            "Close": [150, 151, 152, 153, 154],
            "High": [151, 152, 153, 154, 155],
            "Low": [149, 150, 151, 152, 153],
            "Open": [149, 150, 151, 152, 153],
            "Volume": [1e6] * 5,
        }, index=dates)
        msft = pd.DataFrame({
            "Close": [400, 401, 402, 403, 404],
            "High": [401, 402, 403, 404, 405],
            "Low": [399, 400, 401, 402, 403],
            "Open": [399, 400, 401, 402, 403],
            "Volume": [2e6] * 5,
        }, index=dates)
        # Simulate yfinance group_by="ticker": columns are (ticker, field)
        mock_df = pd.concat([aapl, msft], axis=1, keys=["AAPL", "MSFT"])
        mock_yf.download.return_value = mock_df

        result = fetch_prices(["AAPL", "MSFT"])
        assert "AAPL" in result
        assert "MSFT" in result
        assert isinstance(result["AAPL"], pd.DataFrame)
        assert "Close" in result["AAPL"].columns

    @patch("src.data.yf")
    def test_batch_download_empty_result(self, mock_yf):
        """Empty download result returns empty dict."""
        mock_yf.download.return_value = pd.DataFrame()
        result = fetch_prices(["BADTICKER"])
        assert isinstance(result, dict)
        assert len(result) == 0

    @patch("src.data.yf")
    def test_trailing_empty_bar_is_dropped(self, mock_yf):
        """A run after 00:00 UTC gets an all-NaN row for the untraded new day.

        Left in, it made iloc[-1] NaN for every ticker and rejected all 503.
        """
        dates = pd.bdate_range("2024-01-01", periods=4)
        frames = {
            t: pd.DataFrame({
                "Close": [10.0, 11.0, 12.0, np.nan],
                "High": [10.5, 11.5, 12.5, np.nan],
                "Low": [9.5, 10.5, 11.5, np.nan],
                "Open": [10.0, 11.0, 12.0, np.nan],
                "Volume": [1e6, 1e6, 1e6, np.nan],
            }, index=dates)
            for t in ("AAPL", "MSFT")
        }
        mock_yf.download.return_value = pd.concat(frames.values(), axis=1, keys=frames.keys())

        for tickers in (["AAPL", "MSFT"], ["AAPL"]):
            result = fetch_prices(tickers)
            for t in tickers:
                assert len(result[t]) == 3
                assert result[t]["Close"].iloc[-1] == 12.0

    @patch("src.data.yf")
    def test_batch_download_exception_returns_empty(self, mock_yf):
        """Exception during download is caught, returns empty dict."""
        mock_yf.download.side_effect = Exception("network error")
        result = fetch_prices(["AAPL"])
        assert isinstance(result, dict)
        assert len(result) == 0


class TestFetchFundamentals:
    """Scenario 7: Scanner handles yfinance failure on individual ticker."""

    @patch("src.data.yf")
    def test_successful_fetch_returns_dict(self, mock_yf):
        """Successful .info fetch returns fundamental data dict."""
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "returnOnEquity": 0.22,
            "operatingMargins": 0.18,
            "debtToEquity": 80,
            "marketCap": 200_000_000_000,
            "shortName": "Netflix Inc",
            "trailingPE": 25,
            "earningsGrowth": 0.05,
            "revenueGrowth": 0.03,
        }
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_fundamentals("NFLX")
        assert result is not None
        assert result["returnOnEquity"] == 0.22
        assert result["shortName"] == "Netflix Inc"

    @patch("src.data.yf")
    def test_missing_info_returns_none(self, mock_yf):
        """Ticker with no .info data returns None."""
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_fundamentals("BADTICKER")
        assert result is None

    @patch("src.data.yf")
    def test_exception_returns_none(self, mock_yf):
        """Exception during .info fetch returns None (graceful degradation)."""
        mock_ticker = MagicMock()
        type(mock_ticker).info = property(lambda self: (_ for _ in ()).throw(Exception("fail")))
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_fundamentals("CRASH")
        assert result is None

    @patch("src.data.yf")
    def test_partial_data_still_returns(self, mock_yf):
        """Partial fundamental data (missing some fields) still returns."""
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "returnOnEquity": 0.15,
            "marketCap": 50_000_000_000,
            # missing: operatingMargins, debtToEquity, etc.
        }
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_fundamentals("SOME")
        assert result is not None
        assert result["returnOnEquity"] == 0.15
        assert "operatingMargins" not in result


class TestNextEarningsDate:
    """yfinance's .info has no nextEarningsDate, so Gate 3's blackout never
    fired. The date has to come from Ticker.calendar."""

    @patch("src.data._today")
    @patch("src.data.yf")
    def test_date_comes_from_the_calendar(self, mock_yf, mock_today):
        from datetime import date
        mock_today.return_value = date(2026, 10, 23)
        mock_ticker = MagicMock()
        mock_ticker.info = {"shortName": "Apple"}
        mock_ticker.calendar = {"Earnings Date": [date(2026, 10, 29), date(2026, 11, 2)]}
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_fundamentals("AAPL")
        assert result["nextEarningsDate"] == "2026-10-29"   # the earlier end of a range

    @patch("src.data.yf")
    def test_past_dates_and_a_failed_calendar_give_none(self, mock_yf):
        from datetime import date
        mock_ticker = MagicMock()
        mock_ticker.info = {"shortName": "X"}
        mock_ticker.calendar = {"Earnings Date": [date(2001, 1, 1)]}
        mock_yf.Ticker.return_value = mock_ticker
        assert fetch_fundamentals("X")["nextEarningsDate"] is None

        type(mock_ticker).calendar = property(lambda self: (_ for _ in ()).throw(Exception("429")))
        assert fetch_fundamentals("X")["nextEarningsDate"] is None

    def test_quote_timestamp_is_the_fallback(self):
        from datetime import date, datetime, timezone
        from src.data import _next_earnings_date
        mock_ticker = MagicMock()
        mock_ticker.calendar = {}
        ts = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc).timestamp()
        got = _next_earnings_date(mock_ticker, {"earningsTimestampStart": ts}, date(2026, 10, 25))
        assert got == "2026-11-04"
