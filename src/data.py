"""Data fetching — yfinance wrapper with rate limiting and graceful error handling."""
import logging

import yfinance as yf
import pandas as pd

import config

logger = logging.getLogger(__name__)


def _drop_empty_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows with no close price.

    When the scan runs after 00:00 UTC (GitHub routinely starts the 20:30 cron
    three to five hours late), yfinance appends a row for the new calendar day
    before that session has traded, with every field NaN. Every gate reads
    iloc[-1], so that one empty row made the latest close, drawdown, ATR and
    vol figures NaN for all 503 stocks: Gate 2 rejected every one of them and
    SPY's 200-day comparison fell back to RISK_OFF. Multi-ticker downloads
    also pad a ticker with NaN rows on dates only other tickers traded.
    """
    return df.dropna(subset=["Close"])


def fetch_prices(tickers: list[str], period: str = None) -> dict[str, pd.DataFrame]:
    """Batch-download OHLCV price history via yf.download.

    Returns {ticker: DataFrame} with columns: Open, High, Low, Close, Volume.
    Tickers with no data are omitted from the result.
    """
    period = period or config.PRICE_HISTORY_PERIOD
    result: dict[str, pd.DataFrame] = {}

    if not tickers:
        return result

    try:
        raw = yf.download(tickers, period=period, group_by="ticker", threads=True, progress=False)
    except Exception as e:
        logger.error("Batch price download failed: %s", e)
        return result

    if raw.empty:
        logger.warning("Batch price download returned empty data")
        return result

    # Single ticker: yfinance 1.x still returns MultiIndex (ticker at level 0)
    if len(tickers) == 1:
        ticker = tickers[0]
        if not raw.empty and not raw.isna().all().all():
            df = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = _drop_empty_bars(df)
            if not df.empty:
                result[ticker] = df
        return result

    # Multi-ticker: MultiIndex columns (ticker, field)
    for ticker in tickers:
        try:
            if ticker not in raw.columns.get_level_values(0):
                continue
            df = _drop_empty_bars(raw[ticker].copy())
            if df.empty:
                continue
            result[ticker] = df
        except Exception as e:
            logger.warning("Failed to extract price data for %s: %s", ticker, e)

    logger.info("Fetched prices for %d/%d tickers", len(result), len(tickers))
    return result


def fetch_fundamentals(ticker: str) -> dict | None:
    """Fetch fundamental data for a single ticker via yf.Ticker.info.

    Returns dict of available fields, or None on failure.
    Key fields: returnOnEquity, operatingMargins, debtToEquity, marketCap,
                shortName, trailingPE, earningsGrowth, revenueGrowth,
                forwardPE, nextEarningsDate.
    """
    try:
        info = yf.Ticker(ticker).info
        if not info:
            logger.warning("No fundamental data for %s", ticker)
            return None
        return info
    except Exception as e:
        logger.warning("Failed to fetch fundamentals for %s: %s", ticker, e)
        return None
