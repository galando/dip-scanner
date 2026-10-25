"""datacheck: what the gates would read, and when to fail."""
from datetime import date
from unittest.mock import MagicMock, patch

import src.datacheck as datacheck
import src.sec as sec


def _ticker(earnings):
    t = MagicMock()
    t.info = {"returnOnEquity": 0.3, "operatingMargins": 0.2, "debtToEquity": 50.0,
              "marketCap": 2e12, "currentPrice": 100.0}
    t.calendar = {"Earnings Date": earnings}
    return t


@patch("src.datacheck.yf")
def test_yahoo_only_run_passes_when_an_earnings_date_is_found(mock_yf, capsys):
    mock_yf.Ticker.return_value = _ticker([date(2099, 1, 30)])
    assert datacheck.main(["AAPL"]) == 0
    out = capsys.readouterr().out
    assert "SEC: off" in out and "next earnings (Yahoo calendar): 2099-01-30" in out


@patch("src.datacheck.yf")
def test_no_earnings_date_anywhere_fails(mock_yf, capsys):
    mock_yf.Ticker.return_value = _ticker([])
    assert datacheck.main(["AAPL", "MSFT"]) == 1
    assert "PROBLEM: no ticker returned an earnings date" in capsys.readouterr().out


@patch("src.datacheck.yf")
def test_sec_side_by_side(mock_yf, monkeypatch, capsys):
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    mock_yf.Ticker.return_value = _ticker([date(2099, 1, 30)])
    company = sec.Company("AAPL", 320193, [{"date": "2099-01-01", "items": ["5.02"]}], {})
    monkeypatch.setattr(sec, "load_company", lambda t, include_facts=True: company)
    monkeypatch.setattr(sec, "fundamentals_asof", lambda c, known_by, price: {
        "returnOnEquity": 0.25, "operatingMargins": None, "debtToEquity": 40.0,
        "marketCap": 1.5e12, "negativeEquity": False, "periodEnd": "2026-06-27"})
    assert datacheck.main(["AAPL"]) == 0
    out = capsys.readouterr().out
    assert "returnOnEquity           30.0%       25.0%" in out
    assert "operatingMargins         20.0%         n/a" in out
