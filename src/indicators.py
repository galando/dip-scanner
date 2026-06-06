"""Technical indicators — pure functions on OHLCV DataFrames."""
import pandas as pd
import numpy as np


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """RSI using Wilder smoothing (exponential moving average of gains/losses)."""
    close = df["Close"]
    delta = close.diff()

    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # Wilder smoothing: EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    # Handle division: avg_loss=0 and avg_gain=0 means flat -> RSI=50
    # avg_loss=0 and avg_gain>0 means only gains -> RSI=100
    # avg_loss>0 and avg_gain=0 means only losses -> RSI=0
    both_zero = (avg_loss == 0) & (avg_gain == 0)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.fillna(100.0)           # avg_loss=0 with avg_gain>0 -> RSI=100
    rsi = rsi.where(~both_zero, 50.0) # flat -> RSI=50
    return rsi


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder smoothing."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


def compute_sma(df: pd.DataFrame, period: int) -> pd.Series:
    """Simple moving average of Close."""
    return df["Close"].rolling(window=period, min_periods=period).mean()


def compute_52w_high(df: pd.DataFrame, window: int = 252) -> pd.Series:
    """Rolling 52-week (252 trading days) high."""
    return df["Close"].rolling(window=window, min_periods=1).max()


def compute_annualized_vol(df: pd.DataFrame, window: int = 252) -> pd.Series:
    """Annualized historical volatility (std of daily returns * sqrt(252))."""
    daily_returns = df["Close"].pct_change()
    return daily_returns.rolling(window=window, min_periods=20).std() * np.sqrt(252)


def compute_drawdown_from_52w_high(df: pd.DataFrame) -> pd.Series:
    """Drawdown from 52-week high as percentage.

    Returns negative values when below the high (e.g., -25.0 for 25% drawdown).
    """
    high = compute_52w_high(df)
    return (df["Close"] / high - 1.0) * 100.0


def detect_higher_low(df: pd.DataFrame, window: int = 5) -> bool:
    """Detect higher-low pattern using two consecutive windows.

    Most recent window's lowest low > prior window's lowest low,
    and today's close above the recent window's low.
    """
    if len(df) < window * 2:
        return False

    recent = df.iloc[-window:]
    prior = df.iloc[-(window * 2):-window]

    recent_low = recent["Low"].min()
    prior_low = prior["Low"].min()

    return bool(recent_low > prior_low and df["Close"].iloc[-1] > recent_low)


def detect_consecutive_up_closes(df: pd.DataFrame, n: int = 2, lookback: int = 10) -> bool:
    """Detect N consecutive up-closes off a recent low.

    Last close must be at or near the lowest close in the lookback window.
    """
    if len(df) < lookback + n:
        return False

    recent = df.iloc[-(lookback + n):]
    closes = recent["Close"].values

    # Check if we were near a low in the lookback portion
    lookback_closes = closes[:lookback]
    low_close = lookback_closes.min()

    # The recent closes (last n) should all be up
    tail = closes[-n:]
    if len(tail) < n:
        return False

    all_up = all(tail[i] > tail[i - 1] for i in range(1, len(tail)))

    # The low should be within the lookback window (not too far back)
    return bool(all_up and low_close <= closes[-n])


def compute_vol_adjusted_drop(df: pd.DataFrame) -> pd.Series:
    """Vol-adjusted drop: |drawdown| / annualized_vol.

    Higher ratio means the drop is more extreme relative to the stock's own vol.
    """
    dd = compute_drawdown_from_52w_high(df)
    vol = compute_annualized_vol(df)
    return dd.abs() / vol.replace(0, np.nan)


def atr_distance_below_ma(df: pd.DataFrame, ma_period: int = 50) -> pd.Series:
    """Distance below the MA measured in ATR units.

    Positive value means price is below MA by that many ATRs.
    """
    ma = compute_sma(df, ma_period)
    atr = compute_atr(df, period=14)
    distance = (ma - df["Close"]) / atr.replace(0, np.nan)
    return distance
