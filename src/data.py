"""Data fetching — yfinance wrapper with rate limiting and graceful error handling."""
import logging
from datetime import date, datetime, timezone

import yfinance as yf
import pandas as pd

import config
import src.sec as sec

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


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _next_earnings_date(yf_ticker, info: dict, today: date) -> str | None:
    """The first earnings date on or after `today`, as "YYYY-MM-DD", or None.

    yfinance's .info has no nextEarningsDate field, so the blackout check in
    Gate 3 read None for every stock and never fired. The upcoming report date
    lives in Ticker.calendar["Earnings Date"] (one date, or two when Yahoo only
    knows a range; the earlier one is used). The raw quote timestamps are a
    fallback for when the calendar request fails.
    """
    dates: list[date] = []
    try:
        calendar = yf_ticker.calendar
    except Exception as e:
        logger.warning("Failed to fetch earnings calendar: %s", e)
        calendar = None
    if isinstance(calendar, dict):
        for d in calendar.get("Earnings Date") or []:
            if isinstance(d, datetime):
                d = d.date()
            if isinstance(d, date):
                dates.append(d)
    if not dates:
        for key in ("earningsTimestampStart", "earningsTimestamp"):
            ts = info.get(key)
            if isinstance(ts, (int, float)) and ts > 0:
                dates.append(datetime.fromtimestamp(ts, tz=timezone.utc).date())
    upcoming = sorted(d for d in dates if d >= today)
    return upcoming[0].isoformat() if upcoming else None


def fetch_fundamentals(ticker: str, price: float | None = None) -> dict | None:
    """Fetch fundamental data for a single ticker via yf.Ticker.info.

    Returns dict of available fields, or None on failure.
    Key fields: returnOnEquity, operatingMargins, debtToEquity, marketCap,
                shortName, trailingPE, earningsGrowth, revenueGrowth,
                forwardPE, nextEarningsDate (from Ticker.calendar).

    When SEC access is configured (see src/sec.py), the four Gate 1 fields are
    replaced by values computed from the company's own filings, the same way
    the backtest computes them, and secFilings carries its recent 8-K items.
    A field SEC cannot provide keeps Yahoo's value; qualitySource says which
    source each field came from. `price` (the last close) prices the SEC share
    count into a market cap.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        info = yf_ticker.info
        if not info:
            logger.warning("No fundamental data for %s", ticker)
            return None
    except Exception as e:
        logger.warning("Failed to fetch fundamentals for %s: %s", ticker, e)
        return None

    info = dict(info)
    info["nextEarningsDate"] = _next_earnings_date(yf_ticker, info, _today())
    return _apply_sec(ticker, info, price)


def _apply_sec(ticker: str, info: dict, price: float | None) -> dict:
    """Overlay SEC fundamentals and 8-K filings onto Yahoo's info dict."""
    if not sec.enabled():
        return info
    try:
        company = sec.load_company(ticker, include_facts=config.QUALITY_SOURCE == "sec")
    except Exception as e:  # SEC trouble must never cost the scan a stock
        logger.warning("SEC data unavailable for %s: %s", ticker, e)
        return info
    if company is None:
        return info

    info["secFilings"] = company.filings
    if config.QUALITY_SOURCE != "sec" or company.facts is None:
        return info

    pit = sec.fundamentals_asof(company, known_by=_today(), price=price)
    sources = {}
    for field in sec.QUALITY_FIELDS:
        if pit.get(field) is not None:
            info[field] = pit[field]
            sources[field] = "sec"
        else:
            sources[field] = "yahoo"
    if pit.get("negativeEquity"):
        # The filings say equity is zero or negative. Yahoo's ROE for such a
        # company is not a better number, it is a meaningless one.
        info["negativeEquity"] = True
        sources["returnOnEquity"] = sources["debtToEquity"] = "sec"
    info["qualitySource"] = sources
    info["secPeriodEnd"] = pit.get("periodEnd")
    return info
