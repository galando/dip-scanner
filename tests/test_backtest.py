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
    # Gate 1, used when SEC data is replayed
    MIN_ROE = config.MIN_ROE
    MIN_OP_MARGIN = config.MIN_OP_MARGIN
    MAX_DEBT_EQUITY = config.MAX_DEBT_EQUITY
    MIN_MKT_CAP = config.MIN_MKT_CAP
    SEC_8K_HARD_ITEMS = config.SEC_8K_HARD_ITEMS
    SEC_8K_SOFT_ITEMS = config.SEC_8K_SOFT_ITEMS
    SEC_8K_HARD_LOOKBACK_DAYS = config.SEC_8K_HARD_LOOKBACK_DAYS
    SEC_8K_SOFT_LOOKBACK_DAYS = config.SEC_8K_SOFT_LOOKBACK_DAYS
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

    def test_shallow_spy_is_kept_as_benchmark_and_regime_input(self, monkeypatch, caplog):
        """Dropping SPY turns every date RISK_ON and loses the benchmark silently."""
        import logging
        import src.backtest as backtest
        self._cache(monkeypatch, {"DEEP1": 400, "DEEP2": 400, "SPY": 120})
        monkeypatch.setattr("sys.argv", ["backtest", "--offline"])
        with caplog.at_level(logging.WARNING):
            backtest.main()
        assert "cached SPY holds 120 sessions" in caplog.text

    def test_missing_spy_is_reported(self, monkeypatch, caplog):
        import logging
        import src.backtest as backtest
        self._cache(monkeypatch, {"DEEP1": 400, "DEEP2": 400})
        monkeypatch.setattr("sys.argv", ["backtest", "--offline"])
        with caplog.at_level(logging.WARNING):
            backtest.main()
        assert "no cached SPY" in caplog.text


# --------------------------------------------------------------------------- #
# Gate 1 and the 8-K trap, replayed point in time from SEC data
# --------------------------------------------------------------------------- #
import src.sec as sec  # noqa: E402


def _sec_year(y, q, filed_lag_days=0):
    """Three 10-Q quarters and a 10-K year of value q per quarter."""
    def lag(d):
        return (pd.Timestamp(d) + pd.Timedelta(days=filed_lag_days)).date().isoformat()
    return [
        {"start": f"{y}-01-01", "end": f"{y}-03-31", "val": q, "filed": lag(f"{y}-05-01"), "form": "10-Q", "accn": f"{y}q1"},
        {"start": f"{y}-04-01", "end": f"{y}-06-30", "val": q, "filed": lag(f"{y}-08-01"), "form": "10-Q", "accn": f"{y}q2"},
        {"start": f"{y}-07-01", "end": f"{y}-09-30", "val": q, "filed": lag(f"{y}-11-01"), "form": "10-Q", "accn": f"{y}q3"},
        {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 4 * q, "filed": lag(f"{y + 1}-02-15"), "form": "10-K", "accn": f"{y}k"},
    ]


def _sec_company(net_income=30.0, filed_lag_days=0, filings=()):
    """A company whose figures as of spring 2023 pass Gate 1 (ROE 24%, margin 20%)."""
    def inst(end, val, filed):
        return [{"end": end, "val": val, "filed": filed, "form": "10-K", "accn": "k"}]
    facts = {
        "Revenues": _sec_year(2021, 100.0) + _sec_year(2022, 100.0, filed_lag_days),
        "OperatingIncomeLoss": _sec_year(2021, 20.0) + _sec_year(2022, 20.0, filed_lag_days),
        "NetIncomeLoss": _sec_year(2021, net_income) + _sec_year(2022, net_income, filed_lag_days),
        "StockholdersEquity": inst("2022-12-31", 500.0, "2023-02-15"),
        "LongTermDebt": inst("2022-12-31", 100.0, "2023-02-15"),
        "EntityCommonStockSharesOutstanding": inst("2023-02-01", 1e9, "2023-02-15"),
    }
    return sec.Company("TST", 1, list(filings), facts)


class TestPointInTimeQuality:
    def test_healthy_company_keeps_every_price_signal(self):
        df = _dip_recovery_prices()
        funnel = {"quality": 0, "no_sec_data": 0, "filing": 0}
        assert find_signals(df, None, _Cfg, company=_sec_company(), funnel=funnel) == \
            find_signals(df, None, _Cfg)
        assert funnel == {"quality": 0, "no_sec_data": 0, "filing": 0}

    def test_low_roe_company_is_filtered_out(self):
        funnel = {"quality": 0, "no_sec_data": 0, "filing": 0}
        sigs = find_signals(_dip_recovery_prices(), None, _Cfg,
                            company=_sec_company(net_income=5.0), funnel=funnel)
        assert sigs == []
        assert funnel["quality"] > 0 and funnel["no_sec_data"] == 0

    def test_figures_filed_after_the_signal_are_not_used(self):
        """The same numbers, filed a year late: on the signal dates nothing
        recent enough was known, so the windows count as missing data."""
        funnel = {"quality": 0, "no_sec_data": 0, "filing": 0}
        sigs = find_signals(_dip_recovery_prices(), None, _Cfg,
                            company=_sec_company(filed_lag_days=400), funnel=funnel)
        assert sigs == []
        assert funnel["no_sec_data"] > 0

    def test_8k_counts_only_once_filed_before_the_signal_session(self):
        df = _dip_recovery_prices()
        first = find_signals(df, None, _Cfg)[0]
        day = df.index[first].date()

        before = _sec_company(filings=[{"date": (day - pd.Timedelta(days=10)).isoformat(),
                                        "items": ["4.02"]}])
        funnel = {"quality": 0, "no_sec_data": 0, "filing": 0}
        assert first not in find_signals(df, None, _Cfg, company=before, funnel=funnel)
        assert funnel["filing"] > 0

        same_day = _sec_company(filings=[{"date": day.isoformat(), "items": ["4.02"]}])
        assert first in find_signals(df, None, _Cfg, company=same_day)

    def test_run_backtest_compares_the_same_tickers_with_and_without(self):
        prices = {"AAA": _dip_recovery_prices(), "BBB": _dip_recovery_prices()}
        results = run_backtest(prices, None, _Cfg, companies={"AAA": _sec_company(net_income=5.0)})
        assert results["sec"] == {"tickers": 1, "of": 2}
        assert results["with_stabilization"]["stats"]["n_signals"] == 0
        assert results["price_only"]["stats"]["n_signals"] == 2      # AAA only, not BBB
        assert results["with_stabilization"]["funnel"]["quality"] > 0
        assert "funnel" not in results["price_only"]

        report = format_report(results, _Cfg, n_tickers=2, period="test")
        assert "on the 1 of 2 tickers SEC had data for" in report
        assert "WITHOUT quality and 8-K checks" in report

    def test_without_companies_the_report_is_unchanged(self):
        results = run_backtest({"AAA": _dip_recovery_prices()}, None, _Cfg)
        assert "price_only" not in results and "sec" not in results
        assert "Gate 1 quality cannot be replayed" in format_report(results, _Cfg, 1, "t")
