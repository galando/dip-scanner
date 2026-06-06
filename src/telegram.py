"""Telegram alert sender — compose and send dip opportunity alerts."""
import logging
import os

import requests

logger = logging.getLogger(__name__)


def compose_alert(ticker: str, name: str, price: float, details: dict) -> str:
    """Compose a Telegram alert message from gate results (Hebrew, plain language)."""
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

    if isinstance(mkt_cap, (int, float)) and mkt_cap > 0:
        mkt_cap_str = f"${mkt_cap / 1e9:.0f}B"
    else:
        mkt_cap_str = "N/A"

    stab_str = ", ".join(signals) if signals else "אין"

    regime_labels = {
        "RISK_ON": "שוק אופטימי — משקיעים נוטים לסיכון",
        "RISK_OFF": "שוק זהיר — משקיעים נוטים להגנה",
    }
    regime_he = regime_labels.get(regime, regime)

    if isinstance(rsi, (int, float)):
        if rsi < 30:
            rsi_desc = "אזור קנייה אפשרי"
        elif rsi < 40:
            rsi_desc = "קרוב לאזור קנייה"
        else:
            rsi_desc = "עדיין לא בתחתית"
    else:
        rsi_desc = ""

    below_200dma_str = "כן ⚠️" if below_200dma else "לא ✅"

    trap_lines = []
    for w in warnings:
        trap_lines.append(f"  ⚠️ {w}")
    if not trap_lines:
        trap_lines.append("  לא נמצאו דגלים אדומים ✅")

    trap_section = "\n".join(trap_lines)

    msg = (
        f"🔔 זוהתה הזדמנות קנייה פוטנציאלית!\n"
        f"[{regime} — {regime_he}]\n"
        f"\n"
        f"📌 {ticker} — {name}\n"
        f"💰 מחיר: ${price:.2f}\n"
        f"📉 ירד {abs(drawdown):.0f}% מהשיא של השנה האחרונה\n"
        f"⚡ עוצמת הירידה: חריגה פי {vol_adj} מהרגיל\n"
        f"\n"
        f"📊 אינדיקטורים טכניים:\n"
        f"  RSI: {rsi} {rsi_trend} — {rsi_desc}\n"
        f"  סימני בלימה: {stab_str}\n"
        f"  מתחת לממוצע 200 יום: {below_200dma_str}\n"
        f"\n"
        f"🏢 נתוני החברה:\n"
        f"  תשואה על ההון (ROE): {roe}%\n"
        f"  רווחיות תפעולית: {op_margin}%\n"
        f"  יחס חוב-להון: {debt_eq}\n"
        f"  שווי שוק: {mkt_cap_str}\n"
        f"\n"
        f"🚩 בדיקת מלכודת:\n{trap_section}\n"
        f"\n"
        f"💡 לפני שנכנסים — בדוק מה גרם לירידה.\n"
        f"זה לא ייעוץ השקעות."
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
