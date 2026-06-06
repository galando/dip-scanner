"""Telegram alert sender — compose and send dip opportunity alerts."""
import logging
import os

import requests

logger = logging.getLogger(__name__)


def compose_alert(ticker: str, name: str, price: float, details: dict) -> str:
    """Compose a Telegram alert message from gate results.

    Includes: regime, ticker, price, drawdown, vol-adjusted drop, RSI,
    stabilization method(s), 200dma status, quality metrics, trap flags, disclaimer.
    """
    regime = details.get("regime", "UNKNOWN")
    drawdown = details.get("drawdown_pct", 0)
    vol_adj = details.get("vol_adjusted_drop", "N/A")
    rsi = details.get("rsi", "N/A")
    rsi_trend = details.get("rsi_trend", "")
    signals = details.get("stabilization_signals", [])
    below_200dma = details.get("below_200dma", False)
    roe = details.get("roe", "N/A")
    op_margin = details.get("op_margin", "N/A")
    debt_eq = details.get("debt_eq", "N/A")
    mkt_cap = details.get("mkt_cap", 0)
    warnings = details.get("warnings", [])

    # Format market cap
    if isinstance(mkt_cap, (int, float)) and mkt_cap > 0:
        mkt_cap_str = f"${mkt_cap / 1e9:.0f}B"
    else:
        mkt_cap_str = "N/A"

    # Stabilization evidence
    stab_str = ", ".join(signals) if signals else "none"

    # Trap check section
    trap_lines = []
    for w in warnings:
        trap_lines.append(f"  WARNING: {w}")
    if not trap_lines:
        trap_lines.append("  No red flags detected")

    trap_section = "\n".join(trap_lines)

    msg = (
        f"Dip Opportunity Detected   [Regime: {regime}]\n"
        f"\n"
        f"{ticker} -- {name}\n"
        f"Price: ${price:.2f}\n"
        f"Drawdown from 52w high: {drawdown:.0f}%\n"
        f"Vol-adjusted drop: {vol_adj}x\n"
        f"RSI(14): {rsi} {rsi_trend}\n"
        f"Stabilization: {stab_str}\n"
        f"Below 200-day MA: {'yes' if below_200dma else 'no'}\n"
        f"\n"
        f"Quality: ROE {roe}% | Op margin {op_margin}% | Debt/Eq {debt_eq} | Mkt cap {mkt_cap_str}\n"
        f"\n"
        f"Trap check:\n{trap_section}\n"
        f"\n"
        f"Your call. Check WHY it fell before entering.\n"
        f"This is not investment advice."
    )
    return msg


def send_alert(token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API.

    Returns True on success, False on failure. Never raises.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code != 200:
            logger.error("Telegram API error %d: %s", response.status_code, response.text)
            return False
        logger.info("Alert sent successfully")
        return True
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False
