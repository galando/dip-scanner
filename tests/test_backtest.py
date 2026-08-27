"""Tests for backtest.py — historical signal replay and forward-return stats."""
import numpy as np
import pandas as pd
import pytest

import config
from src.backtest import (
    find_signals,
    forward_return,
    max_adverse_excursion,
    evaluate_signals,
    aggregate,
    run_backtest,
    format_report,
    _regime_at,
)


class _Cfg:
    """Backtest config shrunk so synthetic series stay small and fast."""
    # Gate thresholds (mirror config.py)
    MAX_SINGLE_DAY_MOVE_PCT = config.MAX_SINGLE_DAY_MOVE_PCT
    MIN_DRAWDOWN = config.MIN_DRAWDOWN
    RSI_OVERSOLD = config.RSI_OVERSOLD
    LOOKBACK = config.LOOKBACK
    K_ATR = config.K_ATR
    VOL_DROP_THRESHOLD = config.VOL_DROP_THRESHOLD
    STABILIZATION_REQUIRED_RISK_OFF = config.STABILIZATION_REQUIRED_RISK_OFF
    TRAP_BEHAVIOR = config.TRAP_BEHAVIOR
    EARNINGS_BLACKOUT_DAYS = config.EARNINGS_BLACKOUT_DAYS
    FRESH_LOW_DAYS = config.FRESH_LOW_DAYS
    STEEP_DOWNTREND_PCT = config.STEEP_DOWNTREND_PCT
    GAP_DOWN_PCT = config.GAP_DOWN_PCT
    GAP_VOLUME_MULT = config.GAP_VOLUME_MULT
    DEDUP_DAYS = config.DEDUP_DAYS
    # Backtest knobs (small for tests)
    BACKTEST_MIN_HISTORY = 252
    BACKTEST_STEP_DAYS = 1
    BACKTEST_HORIZONS = (5, 21)
    BACKTEST_MAE_WINDOW = 21


def _make_price_df(closes, start="2022-01-03"):
    dates = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=dates, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1_000_000,
    })


def _dip_recovery_prices(total=400):
    """Long flat base, a 40% slide, a stabilization, then a recovery."""
    closes = []
    closes += [100.0] * 260                                   # base
    closes += [100.0 - i * (40.0 / 40) for i in range(1, 41)]  # slide to 60
    closes += [60.0, 60.5, 61.0, 61.5, 62.0]                   # stabilization
    while len(closes) < total:
        closes.append(closes[-1] * 1.004)                      # recovery
    return _make_price_df(closes)


class TestForwardReturn:
    def test_basic_return(self):
        closes = np.array([100.0, 110.0, 121.0])
        assert forward_return(closes, 0, 1) == pytest.approx(0.10)

    def test_beyond_data_returns_none(self):
        closes = np.array([100.0, 110.0])
        assert forward_return(closes, 0, 5) is None


class TestMaxAdverseExcursion:
    def test_pain_after_entry(self):
        closes = np.array([100.0, 95.0, 90.0, 105.0])
        assert max_adverse_excursion(closes, 0, 3) == pytest.approx(-0.10)

    def test_no_forward_data_returns_none(self):
        closes = np.array([100.0])
        assert max_adverse_excursion(closes, 0, 5) is None


class TestRegimeAt:
    def test_none_spy_defaults_risk_on(self):
        assert _regime_at(None, pd.Timestamp("2024-01-01")) == "RISK_ON"

    def test_rising_spy_is_risk_on(self):
        spy = _make_price_df([400.0 + i * 0.5 for i in range(300)])
        assert _regime_at(spy, spy.index[-1]) == "RISK_ON"

    def test_falling_spy_is_risk_off(self):
        spy = _make_price_df([500.0 - i * 0.5 for i in range(300)])
        assert _regime_at(spy, spy.index[-1]) == "RISK_OFF"


class TestFindSignals:
    def test_dip_recovery_produces_signal(self):
        df = _dip_recovery_prices()
        signals = find_signals(df, None, _Cfg, require_stabilization=True)
        assert len(signals) >= 1
        # Signals fire in the stabilization/early-recovery zone, not the base
        assert all(i >= 300 for i in signals)

    def test_no_lookahead(self):
        """A signal at index i must also fire when the future is truncated."""
        df = _dip_recovery_prices()
        signals = find_signals(df, None, _Cfg, require_stabilization=True)
        first = signals[0]
        truncated = df.iloc[: first + 1]
        again = find_signals(truncated, None, _Cfg, require_stabilization=True)
        assert first in again

    def test_stabilization_off_finds_at_least_as_many(self):
        df = _dip_recovery_prices()
        with_stab = find_signals(df, None, _Cfg, require_stabilization=True)
        without = find_signals(df, None, _Cfg, require_stabilization=False)
        assert len(without) >= len(with_stab)

    def test_flat_series_produces_no_signals(self):
        df = _make_price_df([100.0] * 400)
        assert find_signals(df, None, _Cfg) == []


class TestEvaluateAndAggregate:
    def test_rows_have_returns_and_stats_aggregate(self):
        df = _dip_recovery_prices(total=450)
        signals = find_signals(df, None, _Cfg)
        rows = evaluate_signals(df, None, signals, _Cfg)
        assert rows and all("mae" in r and "ret_5" in r for r in rows)
        stats = aggregate(rows, _Cfg)
        assert stats["n_signals"] == len(rows)
        assert "h5" in stats
        # Recovery series: forward returns should be positive
        assert stats["h5"]["median_ret"] > 0
        assert 0.0 <= stats["h5"]["win_rate"] <= 1.0

    def test_excess_vs_spy_computed(self):
        df = _dip_recovery_prices(total=450)
        spy = _make_price_df([400.0] * 450)  # flat market
        signals = find_signals(df, spy, _Cfg)
        rows = evaluate_signals(df, spy, signals, _Cfg)
        vals = [r["excess_5"] for r in rows if r["excess_5"] is not None]
        assert vals  # excess returns exist against a flat SPY
        for r in rows:
            if r["excess_5"] is not None:
                assert r["excess_5"] == pytest.approx(r["ret_5"])


class TestRunBacktestAndReport:
    def test_end_to_end_report(self):
        prices_map = {
            "GOOD": _dip_recovery_prices(total=450),
            "FLAT": _make_price_df([100.0] * 450),
            "SPY": _make_price_df([400.0] * 450),
        }
        results = run_backtest(prices_map, prices_map["SPY"], _Cfg)
        assert results["with_stabilization"]["stats"]["n_signals"] >= 1
        report = format_report(results, _Cfg, n_tickers=2, period="test")
        assert "WITH stabilization" in report
        assert "WITHOUT stabilization" in report
        assert "Not investment advice" in report

    def test_bad_ticker_never_kills_run(self):
        broken = _make_price_df([100.0] * 300)
        broken.loc[broken.index[250], "Close"] = np.nan
        prices_map = {"BROKEN": broken, "SPY": _make_price_df([400.0] * 300)}
        results = run_backtest(prices_map, prices_map["SPY"], _Cfg)
        assert "with_stabilization" in results


class TestOfflineGuard:
    """--offline must not report a universe-wide backtest built from one ticker."""

    def _cache(self, monkeypatch, depths: dict):
        import pandas as pd
        import src.pricecache as pricecache

        def load_frame(ticker, cache_dir=None):
            n = depths.get(ticker)
            if n is None:
                return None
            idx = pd.bdate_range("2020-01-01", periods=n)
            closes = [100.0] * n
            return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                                 "Close": closes, "Volume": [1] * n}, index=idx)

        monkeypatch.setattr(pricecache, "load_frame", load_frame)
        monkeypatch.setattr(pricecache, "available_tickers",
                            lambda cache_dir=None: sorted(depths))

    def test_refuses_when_nothing_is_deep_enough(self, monkeypatch):
        import src.backtest as backtest
        self._cache(monkeypatch, {"AAA": 100, "BBB": 120, "SPY": 300})
        monkeypatch.setattr("sys.argv", ["backtest", "--offline"])
        with pytest.raises(SystemExit, match="not one signal can be emitted"):
            backtest.main()

    def test_shallow_tickers_are_excluded_not_silently_counted(self, monkeypatch, capsys):
        import src.backtest as backtest
        self._cache(monkeypatch, {"DEEP1": 400, "DEEP2": 400,
                                  "SHALLOW": 50, "SPY": 400})
        monkeypatch.setattr("sys.argv", ["backtest", "--offline"])
        backtest.main()
        report = capsys.readouterr().out
        # The report names the universe it actually scanned, not everything cached.
        assert "2 tickers" in report
