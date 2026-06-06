"""Tests for telegram.py — alert sending via Telegram API."""
import pytest
from unittest.mock import patch, MagicMock

from src.telegram import compose_alert, send_alert


class TestComposeAlert:
    """Scenario 9: Alert format includes all required fields."""

    def _make_gate_details(self):
        """Sample gate details for an alert."""
        return {
            "regime": "RISK_ON",
            "drawdown_pct": -28.0,
            "vol_adjusted_drop": 2.1,
            "rsi": 27.0,
            "rsi_trend": "turning up (was 24 three days ago)",
            "stabilization_signals": ["higher low", "2 up closes"],
            "stabilization_count": 2,
            "below_200dma": True,
            "roe": 22.0,
            "op_margin": 18.0,
            "debt_eq": 80,
            "mkt_cap": 200_000_000_000,
            "warnings": ["Revenue growth YoY: -4% (soft flag)", "Earnings in 6 days"],
        }

    def test_includes_regime_label(self):
        """Alert includes regime label."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "RISK_ON" in msg

    def test_includes_ticker_and_name(self):
        """Alert includes ticker symbol and company name."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "NFLX" in msg
        assert "Netflix Inc" in msg

    def test_includes_price(self):
        """Alert includes current price."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "450" in msg

    def test_includes_drawdown(self):
        """Alert includes drawdown percentage."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "28%" in msg

    def test_includes_vol_adjusted_drop(self):
        """Alert includes vol-adjusted drop."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "2.1" in msg

    def test_includes_rsi(self):
        """Alert includes RSI value."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "RSI" in msg
        assert "27" in msg

    def test_includes_stabilization_evidence(self):
        """Alert includes stabilization method(s)."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "higher low" in msg.lower() or "stabilization" in msg.lower()

    def test_includes_quality_metrics(self):
        """Alert includes quality metrics."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "ROE" in msg
        assert "22" in msg

    def test_includes_trap_flags(self):
        """Alert includes trap check section."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "מלכודת" in msg or "⚠️" in msg

    def test_includes_disclaimer(self):
        """Alert includes disclaimer text."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "ייעוץ השקעות" in msg

    def test_includes_200dma_status(self):
        """Alert includes 200-day MA status."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "200" in msg


class TestSendAlert:
    """Sending alert via Telegram API."""

    @patch("src.telegram.requests")
    def test_sends_to_telegram_api(self, mock_requests):
        """Alert is sent via POST to Telegram API."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests.post.return_value = mock_response

        result = send_alert("test_token", "12345", "test message")
        assert result is True
        mock_requests.post.assert_called_once()
        call_args = mock_requests.post.call_args
        assert "api.telegram.org" in call_args[0][0]
        assert "test_token" in call_args[0][0]

    @patch("src.telegram.requests")
    def test_handles_api_failure_gracefully(self, mock_requests):
        """API failure does not crash, returns False."""
        mock_requests.post.side_effect = Exception("network error")
        result = send_alert("test_token", "12345", "test message")
        assert result is False

    @patch("src.telegram.requests")
    def test_handles_non_200_response(self, mock_requests):
        """Non-200 response returns False."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_requests.post.return_value = mock_response
        result = send_alert("test_token", "12345", "test message")
        assert result is False
