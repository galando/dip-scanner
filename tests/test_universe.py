"""Tests for universe.py — S&P 500 ticker list (fetch -> cache -> fallback)."""
import json

import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

import src.universe as universe
from src.universe import get_sp500_tickers, FALLBACK_TICKERS, MIN_EXPECTED_TICKERS


def _mock_response(symbols):
    resp = MagicMock()
    resp.text = "<html>mocked</html>"
    resp.raise_for_status.return_value = None
    return resp


def _full_symbol_list(n=503):
    return [f"TK{i}" for i in range(n)]


class TestGetSP500Tickers:

    def test_returns_list_of_strings(self):
        tickers = get_sp500_tickers()
        assert isinstance(tickers, list)
        assert len(tickers) > 0
        for t in tickers[:10]:
            assert isinstance(t, str)
            assert len(t) > 0

    @patch("src.universe._save_cache")
    @patch("src.universe.pd.read_html")
    @patch("src.universe.requests.get")
    def test_wikipedia_fetch_success(self, mock_get, mock_read_html, mock_save):
        """A full Wikipedia table is parsed, cached, and returned."""
        symbols = _full_symbol_list()
        mock_get.return_value = _mock_response(symbols)
        mock_read_html.return_value = [pd.DataFrame({"Symbol": symbols})]

        tickers = get_sp500_tickers()

        assert tickers == symbols
        mock_save.assert_called_once_with(symbols)
        # Wikipedia must be fetched with a browser User-Agent, not Python-urllib
        headers = mock_get.call_args.kwargs.get("headers", {})
        assert "Mozilla" in headers.get("User-Agent", "")

    @patch("src.universe._load_cache", return_value=None)
    @patch("src.universe.pd.read_html")
    @patch("src.universe.requests.get")
    def test_partial_parse_not_trusted(self, mock_get, mock_read_html, mock_cache):
        """A too-small table (partial parse) must never masquerade as the index."""
        symbols = ["AAPL", "MSFT", "GOOGL"]
        mock_get.return_value = _mock_response(symbols)
        mock_read_html.return_value = [pd.DataFrame({"Symbol": symbols})]

        tickers = get_sp500_tickers()

        assert tickers == FALLBACK_TICKERS

    @patch("src.universe.requests.get", side_effect=Exception("network error"))
    def test_cache_used_when_fetch_fails(self, mock_get, tmp_path):
        """The last good fetch is reused when Wikipedia is unreachable."""
        cached = _full_symbol_list(500)
        cache_file = tmp_path / "sp500_cache.json"
        cache_file.write_text(json.dumps({"updated": "2026-08-01", "tickers": cached}))

        with patch.object(universe, "CACHE_PATH", str(cache_file)):
            tickers = get_sp500_tickers()

        assert tickers == cached

    @patch("src.universe.requests.get", side_effect=Exception("network error"))
    def test_fallback_when_fetch_and_cache_fail(self, mock_get, tmp_path):
        """No network and no cache -> the full static list, never a crash."""
        with patch.object(universe, "CACHE_PATH", str(tmp_path / "missing.json")):
            tickers = get_sp500_tickers()

        assert tickers == FALLBACK_TICKERS

    @patch("src.universe.requests.get", side_effect=Exception("network error"))
    def test_undersized_cache_not_trusted(self, mock_get, tmp_path):
        """A stale/partial cache is rejected in favor of the full static list."""
        cache_file = tmp_path / "sp500_cache.json"
        cache_file.write_text(json.dumps({"tickers": ["AAPL", "MSFT"]}))

        with patch.object(universe, "CACHE_PATH", str(cache_file)):
            tickers = get_sp500_tickers()

        assert tickers == FALLBACK_TICKERS


class TestFallbackList:

    def test_fallback_list_is_full_index(self):
        """The static fallback must cover (almost) the whole index, not a sample.

        109-of-500 scanned in production traced back to a 110-name fallback;
        the last resort must never silently shrink the universe again.
        """
        assert len(FALLBACK_TICKERS) >= MIN_EXPECTED_TICKERS

    def test_no_duplicates(self):
        assert len(FALLBACK_TICKERS) == len(set(FALLBACK_TICKERS))

    def test_contains_well_known_names(self):
        for t in ("AAPL", "MSFT", "NVDA", "BRK-B", "JPM", "XOM", "JNJ", "NEE"):
            assert t in FALLBACK_TICKERS

    def test_yfinance_dash_format(self):
        """Class-share tickers use dashes (BRK-B), never dots (BRK.B)."""
        assert not any("." in t for t in FALLBACK_TICKERS)
