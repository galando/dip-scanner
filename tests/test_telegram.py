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
        """Alert includes regime label (Hebrew and English)."""
        msg = compose_alert("NFLX", "Netflix Inc.", 450.0, self._make_gate_details())
        assert "תקין" in msg or "Normal" in msg

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


def test_why_bought_line_names_its_source_when_gate_details_are_absent():
    from src.telegram import _why_bought_line as t_line
    """A replayed entry has no gate output; say so instead of printing 0% / N/A."""
    line = t_line({"ticker": "ACN", "entry_reason": {}})
    assert "0%" not in line and "N/A" not in line
    assert "recorded alert" in line


def test_why_bought_line_still_reports_real_gate_details():
    from src.telegram import _why_bought_line as t_line
    line = t_line({
        "ticker": "ACN",
        "entry_reason": {"drawdown_pct": -31.4, "rsi": 28.6,
                         "stabilization_signals": ["higher low"], "roe": 42.0},
    })
    assert "down 31% from high" in line
    assert "RSI 29" in line
    assert "higher low" in line
    assert "ROE 42%" in line


# --------------------------------------------------------------------------- #
# Return on capital, not on turnover
# --------------------------------------------------------------------------- #
def _closed(n, pnl_each, cost=1000.0):
    """n identical closed round-trips, so only the trade COUNT varies."""
    return [{
        "ticker": f"T{i}", "name": f"T{i}",
        "entry_price": 100.0, "exit_price": 100.0 + pnl_each / 10.0,
        "entry_date": "2026-06-01", "exit_date": "2026-06-10",
        "shares": 10.0, "cost_basis": cost,
        "pnl": pnl_each, "pnl_pct": pnl_each / cost * 100.0,
        "sell_reason": "test",
    } for i in range(n)]


def test_summary_headline_is_pnl_over_capital_not_turnover():
    from src.telegram import compose_summary
    closed = _closed(24, -730.0 / 24)          # the June run: -$730 on a $10k book
    turnover = sum(c["cost_basis"] for c in closed)
    msg = compose_summary(closed, [], "2026-06-08", "2026-06-30",
                          turnover, turnover - 730.0, -730.0, book_size=10000.0)
    assert "-7.3% על ההון / on capital ($10000)" in msg
    assert "$10000" in msg and "שווי סופי / Final: $9270" in msg
    # turnover survives, but only as turnover
    assert "turnover: $24000" in msg
    assert "of turnover, not a return" in msg


def test_headline_does_not_move_when_only_the_trade_count_changes():
    """The old denominator shrank the number every time the bot traded again."""
    from src.telegram import compose_summary

    def headline(n):
        closed = _closed(n, -600.0 / n)
        turnover = sum(c["cost_basis"] for c in closed)
        msg = compose_summary(closed, [], "2026-06-01", "2026-06-30",
                              turnover, turnover - 600.0, -600.0, book_size=10000.0)
        return [ln for ln in msg.splitlines() if "on capital" in ln][0]

    # Same -$600 on the same $10,000 book, reached in 6 trades or in 30.
    assert headline(6) == headline(30)
    assert "-6.0%" in headline(6)


def test_update_reports_the_book_size_and_capital_return():
    from src.telegram import compose_update
    rows = [{"ticker": "AAA", "entry_price": 100.0, "current_price": 90.0,
             "pnl": -100.0, "pnl_pct": -10.0}]
    msg = compose_update(rows, 1000.0, 900.0, realized_pnl=-500.0, closed_count=8,
                         date="2026-06-20", day_n=12, total_days=30,
                         total_invested_all=9000.0, book_size=10000.0)
    assert "גודל התיק / Book size: $10000" in msg
    assert "-6.0% על ההון / on capital ($10000)" in msg   # (-100 + -500) / 10000
    assert "turnover: $9000" in msg


def test_trade_notice_reports_capital_return():
    from src.telegram import compose_trade_notice
    rows = [{"ticker": "AAA", "entry_price": 100.0, "current_price": 110.0,
             "pnl": 100.0, "pnl_pct": 10.0}]
    msg = compose_trade_notice([], [], "2026-06-20", open_rows=rows,
                               total_cost=1000.0, total_value=1100.0,
                               realized_pnl=400.0, total_invested_all=5000.0,
                               book_size=10000.0, positions_opened=5)
    assert "+5.0% על ההון / on capital ($10000)" in msg   # (100 + 400) / 10000
    assert "in 5 positions" in msg


def test_book_size_comes_from_the_run_not_from_live_config():
    """A mid-run config change must not rescale a percentage already reported."""
    from src.simulate import book_size
    assert book_size({"cash_per_stock": 1000.0, "max_positions": 10}) == 10000.0
    assert book_size({}) == 0.0


def test_summary_falls_back_to_the_old_line_without_a_book_size():
    from src.telegram import compose_summary
    closed = _closed(2, 50.0)
    msg = compose_summary(closed, [], "2026-06-01", "2026-06-30", 2000.0, 2100.0, 100.0)
    assert "הושקע / Invested: $2000" in msg
    assert "on capital" not in msg


def test_month_end_positions_are_listed_once_not_twice():
    """The caller moves them into `closed` for the totals and also passes them here."""
    from src.telegram import compose_summary, BOOK_CLOSED
    rule_closed = {"ticker": "AAA", "name": "AAA", "entry_price": 100.0,
                   "exit_price": 112.0, "entry_date": "2026-06-01",
                   "exit_date": "2026-06-20", "shares": 10.0, "cost_basis": 1000.0,
                   "pnl": 120.0, "pnl_pct": 12.0, "sell_reason": "target hit"}
    at_end = {"ticker": "BBB", "name": "BBB", "entry_price": 50.0,
              "exit_price": 52.0, "entry_date": "2026-06-10",
              "exit_date": "2026-06-30", "shares": 20.0, "cost_basis": 1000.0,
              "pnl": 40.0, "pnl_pct": 4.0, "sell_reason": BOOK_CLOSED}
    open_rows = [{"ticker": "BBB", "entry_price": 50.0, "current_price": 52.0,
                  "pnl": 40.0, "pnl_pct": 4.0}]

    msg = compose_summary([rule_closed, at_end], open_rows, "2026-06-01",
                          "2026-06-30", 2000.0, 2160.0, 120.0, book_size=10000.0)

    assert msg.count("BBB:") == 1
    assert msg.count("AAA:") == 1
    # ...and it is listed under "still open at month end", not as a rule exit.
    still_open = msg.index("Still open at month end")
    assert msg.index("BBB:") > still_open
    assert msg.index("AAA:") < still_open
    # Both positions still count as resolved trades.
    assert "Winning trades: 2/2" in msg


def test_open_rows_not_mirrored_into_closed_are_still_shown():
    """The summary must not depend on every caller mirroring its open rows."""
    from src.telegram import compose_summary, BOOK_CLOSED
    at_end = {"ticker": "BBB", "name": "BBB", "entry_price": 50.0,
              "exit_price": 52.0, "entry_date": "2026-06-10",
              "exit_date": "2026-06-30", "shares": 20.0, "cost_basis": 1000.0,
              "pnl": 40.0, "pnl_pct": 4.0, "sell_reason": BOOK_CLOSED}
    stray = {"ticker": "CCC", "entry_price": 10.0, "current_price": 11.0,
             "pnl": 100.0, "pnl_pct": 10.0}

    msg = compose_summary([at_end], [stray], "2026-06-01", "2026-06-30",
                          1000.0, 1040.0, 0.0, book_size=10000.0)
    assert msg.count("BBB:") == 1
    assert msg.count("CCC:") == 1
