"""Monthly paper-trading simulation — a one-time, fake-money test of the strategy.

What it does, driven by one run per trading day (GitHub Actions cron):

  • Day 1   : buys every stock that passes the SAME strict four-gate dip pipeline
              the scanner alerts on (up to SIM_MAX_POSITIONS, $SIM_CASH_PER_STOCK each),
              and sends a Telegram message explaining WHY each was bought.
  • Mid-run : every day it re-checks open positions for an exit (take-profit /
              stop-loss / bounce-done / thesis-break) and sells, and fills any free
              slots with fresh dip signals — every buy and sell is announced with a reason.
              A plain status update goes out every SIM_UPDATE_INTERVAL_DAYS days.
  • Month end: closes the book at the last price and sends a full summary.

State lives in simulation.json and is committed back by the workflow, so the run
is stateless between days. Buys use the strict gates (no relaxation); sells use the
mean-reversion exit. This is a simulation only — never investment advice.
"""
import calendar
import json
import logging
import os
from datetime import date, datetime, timezone

import config
import src.universe as universe
import src.data as data
import src.regime as regime_mod
import src.gates as gates
import src.telegram as telegram
from src.indicators import compute_rsi, compute_drawdown_from_52w_high

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #
def load_sim(path: str = None) -> dict | None:
    """Load simulation state; None if it has never been initialized."""
    path = path or config.SIM_STATE_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        logger.warning("Invalid simulation state %s: %s", path, e)
        return None


def _json_default(o):
    """Coerce numpy scalars (from gate details) to native JSON types."""
    import numpy as np
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def save_sim(state: dict, path: str = None) -> None:
    path = path or config.SIM_STATE_PATH
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=_json_default)
    logger.info("Saved simulation state to %s", path)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _month_end(d: date) -> date:
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


# --------------------------------------------------------------------------- #
# Strategy: buy selection (strict gates) and exit rules
# --------------------------------------------------------------------------- #
def scan_candidates(regime: str, prices_map: dict, cfg, exclude: set) -> list[dict]:
    """Return stocks passing ALL gates, deepest drawdown first.

    Mirrors scanner.run_scan's pipeline (gate 2 -> fundamentals -> gate 1 -> gate 3)
    but skips the dedup store and returns structured buy candidates instead of alerting.
    """
    candidates: list[dict] = []
    for ticker, prices_df in prices_map.items():
        if ticker in exclude or ticker == "SPY":
            continue
        try:
            passed_2, details_2 = gates.gate_2_dip_and_stabilization(prices_df, regime, cfg)
            if not passed_2:
                continue
            fund = data.fetch_fundamentals(ticker)
            if fund is None:
                continue
            passed_1, details_1 = gates.gate_1_quality(fund, cfg)
            if not passed_1:
                continue
            passed_3, details_3 = gates.gate_3_trap(fund, prices_df, cfg)
            if not passed_3:
                continue

            details = {**details_2, **details_1, **details_3, "regime": regime}
            candidates.append({
                "ticker": ticker,
                "name": fund.get("shortName", ticker),
                "price": float(prices_df["Close"].iloc[-1]),
                "details": details,
            })
        except Exception as e:  # never let one bad ticker abort the scan
            logger.warning("Candidate scan error for %s: %s", ticker, e)
            continue

    candidates.sort(key=lambda c: c["details"].get("drawdown_pct", 0))
    return candidates


def evaluate_exit(pos: dict, prices_df, cfg) -> tuple[bool, str, float]:
    """Decide whether to sell an open position. Returns (sell?, reason, current_price)."""
    current_price = float(prices_df["Close"].iloc[-1])
    pnl_pct = (current_price / pos["entry_price"] - 1.0) * 100.0

    # 1) Stop-loss — the dip kept falling; the thesis failed.
    if pnl_pct <= -cfg.SIM_STOP_LOSS_PCT:
        return True, (f"סטופ-לוס: ירדה {pnl_pct:.1f}% מהקנייה / stop-loss, "
                      f"down {pnl_pct:.1f}% from entry"), current_price

    # 2) Take-profit — recovered to target; mean reversion captured.
    if pnl_pct >= cfg.SIM_TAKE_PROFIT_PCT:
        return True, (f"יעד רווח: עלתה {pnl_pct:+.1f}% מהקנייה / target hit, "
                      f"up {pnl_pct:+.1f}% from entry"), current_price

    # 3) Bounce complete — RSI back to normal while in profit.
    rsi = compute_rsi(prices_df)
    current_rsi = float(rsi.iloc[-1]) if len(rsi.dropna()) else 50.0
    if current_rsi >= cfg.SIM_RSI_EXIT and pnl_pct > 0:
        return True, (f"ההתאוששות הושלמה: RSI חזר ל-{current_rsi:.0f} ({pnl_pct:+.1f}%) / "
                      f"bounce done, RSI back to {current_rsi:.0f} ({pnl_pct:+.1f}%)"), current_price

    # 4) Thesis breaking — a fresh price-based trap appeared (new lows / downtrend / gap-down).
    passed, _ = gates.gate_3_trap({}, prices_df, cfg, trap_behavior="suppress")
    if not passed:
        return True, ("התזה נשברה: שפל חדש / מגמת ירידה / thesis breaking: "
                      "fresh lows or steep downtrend"), current_price

    return False, "", current_price


# --------------------------------------------------------------------------- #
# P&L helpers
# --------------------------------------------------------------------------- #
def _open_row(pos: dict, current_price: float) -> dict:
    pnl = pos["shares"] * current_price - pos["cost_basis"]
    pnl_pct = (current_price / pos["entry_price"] - 1.0) * 100.0
    return {
        "ticker": pos["ticker"],
        "entry_price": pos["entry_price"],
        "current_price": current_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    }


def _milestones(start: date, end: date, interval: int) -> list[date]:
    """Scheduled update dates: start+interval, start+2*interval, ... before end."""
    out, k = [], 1
    while True:
        m = start.fromordinal(start.toordinal() + k * interval)
        if m >= end:
            break
        out.append(m)
        k += 1
    return out


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def _send(message: str) -> None:
    """Send via Telegram if credentials are set, else log to stdout."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        telegram.send_alert(token, chat_id, message)
    else:
        logger.warning("Telegram credentials not set, printing message instead")
        print(message)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def initialize(today: date, regime: str, prices_map: dict, cfg, state_path: str) -> dict:
    """Day 1: open positions from strict signals and announce them."""
    start, end = today, _month_end(today)
    candidates = scan_candidates(regime, prices_map, cfg, exclude=set())[:cfg.SIM_MAX_POSITIONS]

    positions = []
    for c in candidates:
        shares = cfg.SIM_CASH_PER_STOCK / c["price"]
        positions.append({
            "ticker": c["ticker"],
            "name": c["name"],
            "entry_price": c["price"],
            "entry_date": start.isoformat(),
            "shares": shares,
            "cost_basis": cfg.SIM_CASH_PER_STOCK,
            "entry_reason": c["details"],
        })

    state = {
        "status": "RUNNING",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "cash_per_stock": cfg.SIM_CASH_PER_STOCK,
        "max_positions": cfg.SIM_MAX_POSITIONS,
        "positions": positions,
        "closed": [],
        "updates_sent": [],
    }
    save_sim(state, state_path)
    _send(telegram.compose_simulation_start(
        positions, start.isoformat(), end.isoformat(),
        cfg.SIM_CASH_PER_STOCK, cfg.SIM_MAX_POSITIONS, regime,
    ))
    logger.info("Simulation initialized with %d positions", len(positions))
    return state


def run(today: date = None, cfg=config, state_path: str = None) -> dict:
    """Single daily step of the simulation. Returns the updated state."""
    today = today or _today()
    state_path = state_path or cfg.SIM_STATE_PATH
    logger.info("=== Simulation step %s ===", today.isoformat())

    regime = regime_mod.compute_regime()
    tickers = universe.get_sp500_tickers()

    state = load_sim(state_path)
    held = [p["ticker"] for p in state["positions"]] if state else []
    all_tickers = list(dict.fromkeys(tickers + held))
    prices_map = data.fetch_prices(all_tickers)

    # First ever run: initialize and stop.
    if state is None:
        return initialize(today, regime, prices_map, cfg, state_path)

    if state.get("status") == "DONE":
        logger.info("Simulation already complete — nothing to do.")
        return state

    start = date.fromisoformat(state["start_date"])
    end = date.fromisoformat(state["end_date"])
    is_final = today >= end

    # --- 1) Evaluate exits on open positions ---
    sells, survivors = [], []
    for pos in state["positions"]:
        prices_df = prices_map.get(pos["ticker"])
        if prices_df is None or prices_df.empty:
            survivors.append(pos)  # no data today — hold
            continue
        sell, reason, current_price = evaluate_exit(pos, prices_df, cfg)
        if sell:
            pnl = pos["shares"] * current_price - pos["cost_basis"]
            sells.append({
                "ticker": pos["ticker"], "name": pos["name"],
                "entry_price": pos["entry_price"], "entry_date": pos["entry_date"],
                "exit_price": current_price, "exit_date": today.isoformat(),
                "shares": pos["shares"], "pnl": pnl,
                "pnl_pct": (current_price / pos["entry_price"] - 1.0) * 100.0,
                "sell_reason": reason,
            })
        else:
            survivors.append(pos)
    state["positions"] = survivors
    state["closed"].extend(sells)

    # --- 2) Fill free slots with fresh strict signals (not on the final day) ---
    buys = []
    free = cfg.SIM_MAX_POSITIONS - len(state["positions"])
    if not is_final and free > 0:
        exclude = {p["ticker"] for p in state["positions"]} | {s["ticker"] for s in sells}
        for c in scan_candidates(regime, prices_map, cfg, exclude)[:free]:
            shares = cfg.SIM_CASH_PER_STOCK / c["price"]
            pos = {
                "ticker": c["ticker"], "name": c["name"],
                "entry_price": c["price"], "entry_date": today.isoformat(),
                "shares": shares, "cost_basis": cfg.SIM_CASH_PER_STOCK,
                "entry_reason": c["details"],
            }
            state["positions"].append(pos)
            buys.append(pos)

    # --- 3) Current open-position P&L ---
    open_rows = []
    for pos in state["positions"]:
        prices_df = prices_map.get(pos["ticker"])
        price = float(prices_df["Close"].iloc[-1]) if prices_df is not None and not prices_df.empty else pos["entry_price"]
        open_rows.append(_open_row(pos, price))
    total_cost = sum(p["cost_basis"] for p in state["positions"])
    total_value = sum(p["cost_basis"] + r["pnl"] for p, r in zip(state["positions"], open_rows))
    realized_pnl = sum(c["pnl"] for c in state["closed"])

    # --- 4) Notifications ---
    if (buys or sells) and not is_final:
        _send(telegram.compose_trade_notice(buys, sells, today.isoformat()))

    if is_final:
        # Close the book at the last price.
        for pos, row in zip(state["positions"], open_rows):
            state["closed"].append({
                "ticker": pos["ticker"], "name": pos["name"],
                "entry_price": pos["entry_price"], "entry_date": pos["entry_date"],
                "exit_price": row["current_price"], "exit_date": today.isoformat(),
                "shares": pos["shares"], "pnl": row["pnl"], "pnl_pct": row["pnl_pct"],
                "sell_reason": "החודש הסתיים — סגירת הספרים / month ended — book closed",
            })
        total_invested = sum(c["cost_basis"] if "cost_basis" in c else cfg.SIM_CASH_PER_STOCK
                             for c in state["closed"])
        total_final = total_invested + sum(c["pnl"] for c in state["closed"])
        _send(telegram.compose_summary(
            state["closed"], open_rows, state["start_date"], state["end_date"],
            total_invested, total_final, realized_pnl,
        ))
        state["positions"] = []
        state["status"] = "DONE"
        save_sim(state, state_path)
        logger.info("Simulation complete.")
        return state

    # Periodic status update (handles weekend/holiday slippage via milestone tracking).
    due = [m for m in _milestones(start, end, cfg.SIM_UPDATE_INTERVAL_DAYS)
           if m <= today and m.isoformat() not in state["updates_sent"]]
    if due:
        day_n = (today - start).days
        total_days = (end - start).days
        _send(telegram.compose_update(
            open_rows, total_cost, total_value, realized_pnl,
            len(state["closed"]), today.isoformat(), day_n, total_days,
        ))
        state["updates_sent"].extend(m.isoformat() for m in due)

    save_sim(state, state_path)
    return state


if __name__ == "__main__":
    run()
