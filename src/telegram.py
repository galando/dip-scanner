"""Telegram alert sender — compose and send dip opportunity alerts."""
import logging
import os

import requests

logger = logging.getLogger(__name__)


_SIGNAL_HE = {
    "RSI turning up from oversold": "RSI מתהפך מעלה",
    "higher low": "הפסיקה לרדת",
    "consecutive up closes": "עליות רצופות",
}

_REGIME_LABELS = {
    "RISK_ON": ("תקין", "Normal"),
    "RISK_OFF": ("זהיר", "Cautious"),
}


def _roe_label(v: float) -> str:
    if v >= 20:
        return "טוב / Good"
    if v >= 10:
        return "סביר / Fair"
    return "נמוך / Low"


def _margin_label(v: float) -> str:
    if v >= 15:
        return "טוב / Good"
    if v >= 5:
        return "סביר / Fair"
    return "נמוך / Low"


def _debt_label(v: float) -> str:
    if v < 50:
        return "נמוך / Low"
    if v <= 150:
        return "בינוני / Medium"
    return "גבוה / High"


def compose_alert(ticker: str, name: str, price: float, details: dict) -> str:
    """Compose a bilingual (Hebrew/English) Telegram alert in plain language."""
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

    mkt_cap_str = f"${mkt_cap / 1e9:.0f}B" if isinstance(mkt_cap, (int, float)) and mkt_cap > 0 else "N/A"

    regime_he, regime_en = _REGIME_LABELS.get(regime, (regime, regime))

    # RSI line — avoid jargon; describe what the selling pressure means in plain terms.
    # RSI 0-100: below 30 = stock was sold off too hard (potential bounce),
    # above 70 = bought up too hard. We always show what the number means.
    turning_up = "turning up" in str(rsi_trend).lower()
    if isinstance(rsi, (int, float)):
        rsi_int = round(rsi)
        scale = f"RSI {rsi_int}/100 — below 30 = sold off too hard, potential bounce"
        if rsi < 30 and turning_up:
            rsi_line = f"📊 נמכרה יתר על המידה ומתחילה להתאושש / sold off too hard, starting to recover ({scale})"
        elif rsi < 30:
            rsi_line = f"📊 נמכרה יתר על המידה, עדיין יורדת / sold off too hard, still falling ({scale})"
        elif rsi < 40:
            rsi_line = f"📊 קרובה לאזור מכירה קיצונית / nearing extreme selling zone ({scale})"
        elif rsi < 55 and not turning_up:
            rsi_line = f"📊 לחץ מכירה נמשך, עדיין לא בתחתית / selling pressure, not at bottom yet ({scale})"
        else:
            rsi_line = f"📊 לא ירדה מספיק עדיין / not beaten down enough yet ({scale})"
    else:
        rsi_line = f"📊 RSI: {rsi} {rsi_trend}".rstrip()

    # Stabilization signals — translate known strings, keep unknown ones as-is
    def _translate(s: str) -> str:
        he = _SIGNAL_HE.get(s)
        return f"{he} ({s})" if he else s

    if signals:
        stab_line = f"✅ סימני יציבות / Stability: {', '.join(_translate(s) for s in signals)}"
    else:
        stab_line = "⚠️ אין סימני יציבות ברורים / No clear stability signals yet"

    below_200_str = "כן / Yes ⚠️" if below_200dma else "לא / No ✅"

    # Quality section — round to 1 decimal to avoid float noise like 24.762999...
    roe_str = f"{roe:.1f}% — {_roe_label(roe)}" if isinstance(roe, (int, float)) else str(roe)
    margin_str = f"{op_margin:.1f}% — {_margin_label(op_margin)}" if isinstance(op_margin, (int, float)) else str(op_margin)
    debt_str = _debt_label(debt_eq) if isinstance(debt_eq, (int, float)) else str(debt_eq)

    # Warnings section
    trap_lines = [f"  ⚠️ {w}" for w in warnings] or ["  לא נמצאו דגלים אדומים / No red flags ✅"]
    trap_section = "\n".join(trap_lines)

    drawdown_abs = abs(drawdown)
    msg = (
        f"🔔 התראת ירידה / Dip Alert  [שוק: {regime_he} / Market: {regime_en}]\n"
        f"\n"
        f"{ticker} — {name}\n"
        f"מחיר נוכחי / Price: ${price:.2f}\n"
        f"📉 ירדה {drawdown_abs:.0f}% מהשיא שלה השנה (down {drawdown_abs:.0f}% from its yearly high)\n"
        f"💧 הירידה גדולה יותר מהרגיל לה פי {vol_adj} (drop is {vol_adj}x larger than its typical moves)\n"
        f"{rsi_line}\n"
        f"{stab_line}\n"
        f"\n"
        f"בריאות החברה / Company Health:\n"
        f"  תשואה על ההון / ROE: {roe_str}\n"
        f"  מרווח רווח / Profit margin: {margin_str}\n"
        f"  חוב / Debt: {debt_str}\n"
        f"  מתחת לממוצע 200 יום / Below 200d MA: {below_200_str}\n"
        f"  שווי שוק / Market cap: {mkt_cap_str}\n"
        f"\n"
        f"⚠️ שים לב / Watch out:\n"
        f"{trap_section}\n"
        f"\n"
        f"👉 זו נקודת התחלה לבדוק, לא ייעוץ השקעות.\n"
        f"   לפני שעושים כלום, כדאי להבין למה היא ירדה.\n"
        f"   (Check WHY it dropped before acting — not investment advice.)"
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
