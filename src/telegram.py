"""Telegram alert sender — compose and send dip opportunity alerts."""
import json
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


# ---------------------------------------------------------------------------
# Monthly paper-trading simulation messages (src/simulate.py)
# Same bilingual, plain-language voice as the dip alerts above.
# ---------------------------------------------------------------------------

def _why_bought_line(pos: dict) -> str:
    """One compact bilingual line explaining why a position was opened."""
    d = pos.get("entry_reason", {})
    dd = abs(d.get("drawdown_pct", 0) or 0)
    rsi = d.get("rsi", "N/A")
    signals = d.get("stabilization_signals", []) or []
    roe = d.get("roe", None)
    rsi_str = f"{round(rsi)}" if isinstance(rsi, (int, float)) else str(rsi)
    sig_he = ", ".join(_SIGNAL_HE.get(s, s) for s in signals) or "—"
    roe_str = f", ROE {roe:.0f}%" if isinstance(roe, (int, float)) else ""
    return (
        f"   ↳ ירדה {dd:.0f}% מהשיא, RSI {rsi_str}, סימני יציבות: {sig_he}{roe_str}\n"
        f"     (down {dd:.0f}% from high, RSI {rsi_str}, stabilizing: {', '.join(signals) or 'n/a'}{roe_str})"
    )


def compose_simulation_start(positions: list[dict], start_date: str, end_date: str,
                             cash_per_stock: float, max_positions: int, regime: str) -> str:
    """Opening notification: what was bought on day 1 and why."""
    regime_he, regime_en = _REGIME_LABELS.get(regime, (regime, regime))
    header = (
        f"🧪 סימולציית חודש — התחלה / Monthly Simulation — START\n"
        f"📅 {start_date} → {end_date}\n"
        f"💵 כסף מדומה / Paper money: ${cash_per_stock:.0f} לכל מניה / per stock, עד / up to {max_positions}\n"
        f"📈 מצב שוק / Market: {regime_he} / {regime_en}\n"
    )
    if not positions:
        return (
            header + "\n"
            "אף מניה לא עברה את כל הסינונים היום — מתחילים עם 0 פוזיציות.\n"
            "אקנה ברגע שתופיע ירידה איכותית. (No stock passed all gates today — "
            "starting with 0 positions; I'll buy as quality dips appear.)\n"
        )
    invested = sum(p["cost_basis"] for p in positions)
    lines = [header, f"\n🟢 נקנו / Bought ({len(positions)}) — סה\"כ ${invested:.0f}:"]
    for p in positions:
        lines.append(
            f"\n{p['ticker']} — {p['name']}  @ ${p['entry_price']:.2f}  "
            f"({p['shares']:.3f} מניות / shares)"
        )
        lines.append(_why_bought_line(p))
    lines.append(
        "\n\n📲 עדכון כל 3 ימים + סיכום בסוף החודש. "
        "(Updates every 3 days + a summary at month end.)\n"
        "ℹ️ סימולציה בלבד, לא ייעוץ השקעות. (Simulation only — not investment advice.)"
    )
    return "\n".join(lines)


def _pnl_emoji(pct: float) -> str:
    return "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")


def compose_trade_notice(buys: list[dict], sells: list[dict], date: str) -> str:
    """Notification sent whenever the bot buys or sells mid-month, with reasons."""
    lines = [f"🔁 פעולת מסחר בסימולציה / Simulation trade — {date}"]
    if sells:
        lines.append("\n🔻 נמכרו / SOLD:")
        for s in sells:
            emoji = _pnl_emoji(s["pnl_pct"])
            lines.append(
                f"{emoji} {s['ticker']} @ ${s['exit_price']:.2f} → "
                f"{s['pnl_pct']:+.1f}% (${s['pnl']:+.0f})"
            )
            lines.append(f"   ↳ סיבה / why: {s['sell_reason']}")
    if buys:
        lines.append("\n🟢 נקנו / BOUGHT:")
        for b in buys:
            lines.append(
                f"🟢 {b['ticker']} — {b['name']} @ ${b['entry_price']:.2f} "
                f"({b['shares']:.3f} מניות / shares)"
            )
            lines.append(_why_bought_line(b))
    lines.append("\nℹ️ סימולציה בלבד. (Simulation only — not investment advice.)")
    return "\n".join(lines)


def _portfolio_block(rows: list[dict], total_cost: float, total_value: float) -> list[str]:
    """Shared open-positions table used by updates and the final summary."""
    lines = []
    for r in rows:
        emoji = _pnl_emoji(r["pnl_pct"])
        lines.append(
            f"{emoji} {r['ticker']}: ${r['entry_price']:.2f} → ${r['current_price']:.2f}  "
            f"{r['pnl_pct']:+.1f}% (${r['pnl']:+.0f})"
        )
    total_pct = (total_value / total_cost - 1) * 100 if total_cost else 0.0
    lines.append(
        f"\n📊 פתוחות / Open: ${total_value:.0f} מתוך / of ${total_cost:.0f} "
        f"→ {_pnl_emoji(total_pct)} {total_pct:+.1f}%"
    )
    return lines


def compose_update(open_rows: list[dict], total_cost: float, total_value: float,
                   realized_pnl: float, closed_count: int, date: str,
                   day_n: int, total_days: int) -> str:
    """Periodic (every-3-days) status update."""
    lines = [
        f"📲 עדכון סימולציה / Simulation update — {date}",
        f"⏳ יום / Day {day_n} מתוך / of {total_days}",
    ]
    if open_rows:
        lines.append("")
        lines += _portfolio_block(open_rows, total_cost, total_value)
    else:
        lines.append("\nאין פוזיציות פתוחות כרגע. (No open positions right now.)")
    if closed_count:
        lines.append(
            f"💰 רווח/הפסד ממומש / Realized P&L: {_pnl_emoji(realized_pnl)} "
            f"${realized_pnl:+.0f} ({closed_count} עסקאות סגורות / closed trades)"
        )
    lines.append("\nℹ️ סימולציה בלבד. (Simulation only — not investment advice.)")
    return "\n".join(lines)


def compose_summary(closed: list[dict], open_rows: list[dict], start_date: str,
                    end_date: str, total_invested: float, total_final: float,
                    realized_pnl: float) -> str:
    """End-of-month summary: every trade, the reasons, and the bottom line."""
    total_pnl = total_final - total_invested
    total_pct = (total_final / total_invested - 1) * 100 if total_invested else 0.0
    lines = [
        "🏁 סיכום סימולציית החודש / Monthly Simulation — SUMMARY",
        f"📅 {start_date} → {end_date}\n",
    ]
    if closed:
        lines.append("📕 עסקאות שנסגרו / Closed trades:")
        for c in closed:
            emoji = _pnl_emoji(c["pnl_pct"])
            lines.append(
                f"{emoji} {c['ticker']}: ${c['entry_price']:.2f} → ${c['exit_price']:.2f}  "
                f"{c['pnl_pct']:+.1f}% (${c['pnl']:+.0f})"
            )
            lines.append(f"   ↳ {c['sell_reason']}")
        lines.append("")
    if open_rows:
        lines.append("📗 פוזיציות שנותרו פתוחות בסוף (נסגרו לפי מחיר אחרון) / "
                     "Still open at month end (marked at last price):")
        for r in open_rows:
            emoji = _pnl_emoji(r["pnl_pct"])
            lines.append(
                f"{emoji} {r['ticker']}: ${r['entry_price']:.2f} → ${r['current_price']:.2f}  "
                f"{r['pnl_pct']:+.1f}% (${r['pnl']:+.0f})"
            )
        lines.append("")
    winners = [t for t in closed if t["pnl"] > 0]
    n_trades = len(closed)
    win_line = (
        f"✅ עסקאות מנצחות / Winning trades: {len(winners)}/{n_trades}"
        if n_trades else "אין עסקאות סגורות. (No closed trades.)"
    )
    lines.append("📈 שורה תחתונה / Bottom line:")
    lines.append(win_line)
    lines.append(
        f"💵 הושקע / Invested: ${total_invested:.0f}  →  שווי סופי / Final: ${total_final:.0f}"
    )
    lines.append(f"{_pnl_emoji(total_pct)} סה\"כ / TOTAL: {total_pct:+.1f}% (${total_pnl:+.0f})")
    lines.append(
        "\nℹ️ זו הייתה סימולציה חד-פעמית עם כסף מדומה. אפשר להריץ שוב חודש הבא.\n"
        "(One-time paper-money simulation. Run it again next month if you like — "
        "not investment advice.)"
    )
    return "\n".join(lines)


_USERS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.json")


def get_chat_ids() -> list[str]:
    """Return all registered chat IDs from data/users.json plus TELEGRAM_CHAT_ID env var."""
    ids: set[str] = set()

    env_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_id:
        ids.add(env_id)

    try:
        with open(_USERS_FILE) as f:
            data = json.load(f)
        for cid in data.get("chat_ids", []):
            cid = str(cid).strip()
            if cid:
                ids.add(cid)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return sorted(ids)


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
