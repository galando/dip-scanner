"""Market regime detection — SPY vs 200-day MA."""
import logging

import src.data as data
from src.indicators import compute_sma

logger = logging.getLogger(__name__)

RISK_ON = "RISK_ON"
RISK_OFF = "RISK_OFF"


def compute_regime() -> str:
    """Compute market regime from SPY vs its 200-day MA.

    Returns RISK_ON if SPY close > 200dma, RISK_OFF otherwise.
    Computed once per run and reused for every stock.
    """
    prices = data.fetch_prices(["SPY"])

    if "SPY" not in prices:
        logger.warning("SPY price data unavailable, defaulting to RISK_OFF")
        return RISK_OFF

    spy_df = prices["SPY"]
    if len(spy_df) < 200:
        logger.warning("SPY has fewer than 200 days of data, defaulting to RISK_OFF")
        return RISK_OFF

    spy_close = spy_df["Close"].iloc[-1]
    sma_200 = compute_sma(spy_df, 200).iloc[-1]

    regime = RISK_ON if spy_close > sma_200 else RISK_OFF
    logger.info("Regime: %s (SPY close=%.2f, 200dma=%.2f)", regime, spy_close, sma_200)
    return regime
