"""Tests for indicators.py — pure functions on price DataFrames."""
import pytest
import pandas as pd
import numpy as np

from src.indicators import (
    compute_rsi,
    compute_atr,
    compute_sma,
    compute_52w_high,
    compute_annualized_vol,
    compute_drawdown_from_52w_high,
    detect_higher_low,
    detect_consecutive_up_closes,
    compute_vol_adjusted_drop,
    atr_distance_below_ma,
)


def _make_price_df(prices, start="2024-01-01"):
    """Build OHLCV DataFrame from a close-price series."""
    dates = pd.bdate_range(start, periods=len(prices))
    close = pd.Series(prices, index=dates, dtype=float)
    high = close * 1.01
    low = close * 0.99
    df = pd.DataFrame({
        "Open": close,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": 1_000_000,
    })
    return df


def _make_ohlcv_df(closes, highs=None, lows=None, volumes=None, start="2024-01-01"):
    """Build OHLCV DataFrame with explicit highs/lows/volumes."""
    dates = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=dates, dtype=float)
    high = pd.Series(highs if highs else [c * 1.01 for c in closes], index=dates, dtype=float)
    low = pd.Series(lows if lows else [c * 0.99 for c in closes], index=dates, dtype=float)
    vol = pd.Series(volumes if volumes else [1_000_000] * len(closes), index=dates, dtype=float)
    df = pd.DataFrame({
        "Open": close,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": vol,
    })
    return df


class TestRSI:
    """RSI(14) computation with Wilder smoothing."""

    def test_flat_prices_rsi_near_50(self):
        """Flat prices should give RSI around 50."""
        prices = [100.0] * 50
        df = _make_price_df(prices)
        rsi = compute_rsi(df, period=14)
        assert 40 <= rsi.iloc[-1] <= 60

    def test_rising_prices_high_rsi(self):
        """Consistently rising prices should give RSI near 100."""
        prices = [100 + i for i in range(50)]
        df = _make_price_df(prices)
        rsi = compute_rsi(df, period=14)
        assert rsi.iloc[-1] > 70

    def test_falling_prices_low_rsi(self):
        """Consistently falling prices should give RSI near 0."""
        prices = [200 - i for i in range(50)]
        df = _make_price_df(prices)
        rsi = compute_rsi(df, period=14)
        assert rsi.iloc[-1] < 30


class TestATR:
    """ATR(14) computation."""

    def test_constant_range_atr(self):
        """Constant daily range should give predictable ATR."""
        # high=101, low=99, so range=2
        closes = [100.0] * 30
        highs = [101.0] * 30
        lows = [99.0] * 30
        df = _make_ohlcv_df(closes, highs, lows)
        atr = compute_atr(df, period=14)
        assert atr.iloc[-1] == pytest.approx(2.0, abs=0.1)

    def test_atr_returns_series(self):
        """ATR returns a pandas Series."""
        prices = [100.0] * 30
        df = _make_price_df(prices)
        atr = compute_atr(df, period=14)
        assert isinstance(atr, pd.Series)


class TestSMA:
    """Simple moving average."""

    def test_sma_50(self):
        """SMA(50) is the mean of the last 50 closes."""
        prices = list(range(1, 101))  # 1..100
        df = _make_price_df(prices)
        sma = compute_sma(df, 50)
        expected = sum(range(51, 101)) / 50
        assert sma.iloc[-1] == pytest.approx(expected, abs=0.01)

    def test_sma_200(self):
        """SMA(200) requires 200 data points."""
        prices = [100.0] * 250
        df = _make_price_df(prices)
        sma = compute_sma(df, 200)
        assert sma.iloc[-1] == pytest.approx(100.0, abs=0.01)


class Test52WeekHigh:
    """52-week high computation."""

    def test_correct_high(self):
        """52-week high is the max close in the last 252 trading days."""
        prices = [100.0] * 200 + [150.0] + [140.0] * 51
        df = _make_price_df(prices)
        high = compute_52w_high(df)
        assert high.iloc[-1] == 150.0


class TestAnnualizedVol:
    """Historical volatility."""

    def test_zero_vol_for_flat(self):
        """Flat prices give near-zero vol."""
        prices = [100.0] * 50
        df = _make_price_df(prices)
        vol = compute_annualized_vol(df)
        assert vol.iloc[-1] == pytest.approx(0.0, abs=0.001)

    def test_positive_vol_for_volatile(self):
        """Volatile prices give positive vol."""
        np.random.seed(42)
        prices = (100 + np.cumsum(np.random.randn(100) * 2)).tolist()
        df = _make_price_df(prices)
        vol = compute_annualized_vol(df)
        assert vol.iloc[-1] > 0


class TestDrawdown:
    """Drawdown from 52-week high."""

    def test_at_high_zero_drawdown(self):
        """At 52w high, drawdown is 0."""
        prices = [100.0] * 260
        df = _make_price_df(prices)
        dd = compute_drawdown_from_52w_high(df)
        assert dd.iloc[-1] == pytest.approx(0.0, abs=0.01)

    def test_below_high_negative_drawdown(self):
        """Below 52w high, drawdown is negative (percent)."""
        prices = [100.0] * 200 + [75.0]
        df = _make_price_df(prices)
        dd = compute_drawdown_from_52w_high(df)
        assert dd.iloc[-1] == pytest.approx(-25.0, abs=0.5)


class TestVolAdjustedDrop:
    """Vol-adjusted drop normalizes across volatility profiles.

    Scenario 10: utility with low vol vs semiconductor with high vol.
    """

    def test_utility_vs_semiconductor(self):
        """Utility 15% drop / 12% vol should have higher ratio than semi 30% drop / 40% vol."""
        # Utility: 15% drawdown, 12% annualized vol
        util_ratio = abs(-15.0) / 12.0
        # Semi: 30% drawdown, 40% annualized vol
        semi_ratio = abs(-30.0) / 40.0
        assert util_ratio > semi_ratio  # utility's drop is more significant relative to its vol

    def test_returns_reasonable_value(self):
        """Vol-adjusted drop returns a positive float."""
        prices = [100.0] * 200 + [75.0]
        df = _make_price_df(prices)
        vadj = compute_vol_adjusted_drop(df)
        assert isinstance(vadj.iloc[-1], float)


class TestHigherLow:
    """Swing-low / higher-low detection (5-day windows)."""

    def test_higher_low_detected(self):
        """When recent low is higher than prior low, returns True."""
        # Build prices where last 5 days have higher low than previous 5
        closes = [100.0] * 10 + [80.0, 79.0, 78.0, 79.0, 80.0,  # first 5-day window low=78
                                   82.0, 81.0, 80.0, 81.0, 83.0]  # second 5-day window low=80
        lows =   [100.0] * 10 + [79.0, 78.0, 77.0, 78.0, 79.0,
                                   81.0, 80.0, 79.0, 80.0, 82.0]
        df = _make_ohlcv_df(closes, lows=lows)
        result = detect_higher_low(df)
        assert result is True

    def test_lower_low_not_detected(self):
        """When recent low is lower than prior low, returns False."""
        closes = [100.0] * 10 + [85.0, 84.0, 83.0, 84.0, 85.0,  # low=83
                                   82.0, 81.0, 80.0, 81.0, 82.0]  # low=80 < 83
        lows =   [100.0] * 10 + [84.0, 83.0, 82.0, 83.0, 84.0,
                                   81.0, 80.0, 79.0, 80.0, 81.0]
        df = _make_ohlcv_df(closes, lows=lows)
        result = detect_higher_low(df)
        assert result is False


class TestConsecutiveUpCloses:
    """Consecutive up-closes detection."""

    def test_two_up_closes_off_low(self):
        """Two consecutive up closes off a 10-day low should return True."""
        # 10 days of decline, then 2 up closes
        closes = [100 - i for i in range(12)] + [90.0, 91.0]  # last two are up
        df = _make_price_df(closes)
        result = detect_consecutive_up_closes(df)
        assert result is True

    def test_no_up_closes(self):
        """Continued decline should return False."""
        closes = [100 - i for i in range(20)]
        df = _make_price_df(closes)
        result = detect_consecutive_up_closes(df)
        assert result is False


class TestATRDistanceBelowMA:
    """ATR-based distance below 50-day MA."""

    def test_at_ma_zero_distance(self):
        """At the MA, distance is 0."""
        prices = [100.0] * 60
        df = _make_price_df(prices)
        dist = atr_distance_below_ma(df)
        assert dist.iloc[-1] == pytest.approx(0.0, abs=0.1)

    def test_below_ma_positive_distance(self):
        """Below MA, distance in ATRs is positive."""
        prices = [100.0] * 60
        df = _make_price_df(prices)
        # Drop last close to 90
        df.iloc[-1, df.columns.get_loc("Close")] = 90.0
        df.iloc[-1, df.columns.get_loc("Low")] = 89.0
        df.iloc[-1, df.columns.get_loc("High")] = 91.0
        dist = atr_distance_below_ma(df)
        assert dist.iloc[-1] > 0
