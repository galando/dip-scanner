"""Replay the monthly simulation over a past window, day by day.

src/simulate.py can only ever run forward: one step per cron firing, against
whatever yfinance returns today. That makes the strategy impossible to inspect
after the fact — you have to wait a month to see a month.

This module runs the same book-keeping over history instead:

  • prices come from the offline cache (src/pricecache), truncated to the day
    being replayed, so no step can see the future;
  • exits are the production rule — simulate.evaluate_exit, unchanged;
  • buys come from the scanner's own recorded alert history (data/alerts.json,
    reconstructed from the committed dedup state), because a buy signal needs
    the full S&P 500 universe and point-in-time fundamentals that a price cache
    cannot reconstruct. Every entry is therefore a signal the live bot really
    did fire, on the day it fired, at that day's close.

The one judgement call the alert log does not record is ordering: when more
names are flagged than there are free slots, the live scanner ranks by
composite score. Here the tie-break is "most oversold first" (lowest RSI),
which is strategy-consistent and uses only cached data.

Usage:
    python -m src.replay 2026-07-28            # a full SIM_DURATION_DAYS month
    python -m src.replay 2026-07-28 2026-08-27
"""
import json
import logging
import os
import sys
from datetime import date

import config
import src.pricecache as pricecache
import src.simulate as simulate
import src.telegram as telegram
from src.indicators import compute_rsi

logger = logging.getLogger(__name__)

ALERTS_PATH = os.path.join("data", "alerts.json")


def load_alerts(path: str = ALERTS_PATH) -> dict[str, list[str]]:
    """{'2026-08-01': ['ACN', 'ADBE', ...]} — the bot's real alert log."""
    with open(path) as f:
        return json.load(f)


# A signal raised while the market is shut is acted on at the next close. Beyond
# this many days it is stale — the dip it described has had time to become
# something else — so it is dropped rather than dragged forward.
MAX_SIGNAL_AGE_DAYS = 4


def _actionable(alerts: dict[str, list[str]], sessions: list[date],
                max_age_days: int = MAX_SIGNAL_AGE_DAYS,
                window_start: date | None = None) -> dict[str, list[str]]:
    """Move each alert onto the first session at or after the day it fired.

    Almost every alert already lands on a trading day, but a handful do not —
    the 1 August 2026 batch was stamped on a Saturday. Those roll to the next
    session.

    An alert that fired *before* the window opened belongs to the previous
    window, not this one, even if it is only a day or two old: letting it in
    would make two adjacent windows share entries, which quietly destroys the
    independence that `src/validate.py` relies on. So a signal must have fired
    on or after the window opened, and must not be staler than `max_age_days`
    by the time a session comes round to act on it.

    The boundary is `window_start` — the day the window opens — not the first
    session in it. A window that opens on a weekend has its first session on the
    Monday, and using that instead would throw away exactly the alerts the
    roll-forward above exists to keep.
    """
    out: dict[str, list[str]] = {}
    first = window_start or sessions[0]
    for day, tickers in sorted(alerts.items()):
        fired = date.fromisoformat(day)
        if fired < first:
            continue
        landing = next((s for s in sessions if s >= fired), None)
        if landing is None or (landing - fired).days > max_age_days:
            continue
        bucket = out.setdefault(landing.isoformat(), [])
        bucket.extend(t for t in tickers if t not in bucket)
    return out


def _rank_signals(tickers: list[str], prices_map: dict) -> list[str]:
    """Most oversold first — the tie-break when free slots are scarce."""
    def rsi_of(t: str) -> float:
        df = prices_map.get(t)
        if df is None or len(df) < 15:
            return 100.0
        series = compute_rsi(df).dropna()
        return float(series.iloc[-1]) if len(series) else 100.0
    return sorted(tickers, key=rsi_of)


def replay(start: date, end: date = None, cfg=config, cache_dir: str = pricecache.CACHE_DIR,
           alerts_path: str = ALERTS_PATH) -> dict:
    """Run the whole window and return the finished simulation state.

    The state has the same shape src/simulate.py persists, so the Telegram
    composers and any downstream reporting work on it unchanged.
    """
    end = end or simulate._sim_end(start, cfg)
    sessions = pricecache.trading_days(start, end, cache_dir)
    if not sessions:
        raise ValueError(f"No cached sessions in {start}..{end}")
    alerts = _actionable(load_alerts(alerts_path), sessions, window_start=start)

    state = {
        "status": "RUNNING",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "cash_per_stock": cfg.SIM_CASH_PER_STOCK,
        "max_positions": cfg.SIM_MAX_POSITIONS,
        "positions": [],
        "closed": [],
        "updates_sent": [],
    }
    messages: list[str] = []
    missing: set[str] = set()

    for today in sessions:
        is_final = today >= sessions[-1]
        held = [p["ticker"] for p in state["positions"]]
        todays_signals = alerts.get(today.isoformat(), [])
        wanted = list(dict.fromkeys(held + todays_signals))
        prices_map = pricecache.fetch_prices_asof(wanted, today, cache_dir)
        missing |= {t for t in wanted if t not in prices_map}

        # --- 1) Exits on open positions (production rule, unchanged) ---
        sells, survivors = [], []
        for pos in state["positions"]:
            df = prices_map.get(pos["ticker"])
            if df is None or df.empty:
                survivors.append(pos)          # no data today — hold
                continue
            sell, reason, price = simulate.evaluate_exit(pos, df, cfg)
            if sell:
                sells.append({
                    "ticker": pos["ticker"], "name": pos["name"],
                    "entry_price": pos["entry_price"], "entry_date": pos["entry_date"],
                    "exit_price": price, "exit_date": today.isoformat(),
                    "shares": pos["shares"], "cost_basis": pos["cost_basis"],
                    "pnl": pos["shares"] * price - pos["cost_basis"],
                    "pnl_pct": (price / pos["entry_price"] - 1.0) * 100.0,
                    "sell_reason": reason,
                })
            else:
                survivors.append(pos)
        state["positions"] = survivors
        state["closed"].extend(sells)

        # --- 2) Fill free slots from the day's alerts ---
        buys = []
        free = cfg.SIM_MAX_POSITIONS - len(state["positions"])
        if not is_final and free > 0:
            exclude = {p["ticker"] for p in state["positions"]} | {s["ticker"] for s in sells}
            fresh = [t for t in todays_signals if t not in exclude and t in prices_map]
            for ticker in _rank_signals(fresh, prices_map)[:free]:
                price = float(prices_map[ticker]["Close"].iloc[-1])
                pos = {
                    "ticker": ticker, "name": ticker,
                    "entry_price": price, "entry_date": today.isoformat(),
                    "shares": cfg.SIM_CASH_PER_STOCK / price,
                    "cost_basis": cfg.SIM_CASH_PER_STOCK,
                    "entry_reason": {},
                }
                state["positions"].append(pos)
                buys.append(pos)

        # --- 3) Mark the book to today's close ---
        open_rows = []
        for pos in state["positions"]:
            df = prices_map.get(pos["ticker"])
            price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else pos["entry_price"]
            open_rows.append(simulate._open_row(pos, price))
        total_cost = sum(p["cost_basis"] for p in state["positions"])
        total_value = sum(p["cost_basis"] + r["pnl"] for p, r in zip(state["positions"], open_rows))
        realized_pnl = sum(c["pnl"] for c in state["closed"])
        closed_invested = sum(c.get("cost_basis", cfg.SIM_CASH_PER_STOCK) for c in state["closed"])

        # --- 4) The messages the bot would have sent ---
        if (buys or sells) and not is_final:
            messages.append(telegram.compose_trade_notice(
                buys, sells, today.isoformat(),
                open_rows=open_rows, total_cost=total_cost, total_value=total_value,
                realized_pnl=realized_pnl, total_invested_all=total_cost + closed_invested,
                book_size=simulate.book_size(state),
                positions_opened=len(state["closed"]) + len(state["positions"]),
            ))

        if is_final:
            for pos, row in zip(state["positions"], open_rows):
                state["closed"].append({
                    "ticker": pos["ticker"], "name": pos["name"],
                    "entry_price": pos["entry_price"], "entry_date": pos["entry_date"],
                    "exit_price": row["current_price"], "exit_date": today.isoformat(),
                    "shares": pos["shares"], "cost_basis": pos["cost_basis"],
                    "pnl": row["pnl"], "pnl_pct": row["pnl_pct"],
                    "sell_reason": "החודש הסתיים — סגירת הספרים / month ended — book closed",
                })
            invested = sum(c.get("cost_basis", cfg.SIM_CASH_PER_STOCK) for c in state["closed"])
            final = invested + sum(c["pnl"] for c in state["closed"])
            messages.append(telegram.compose_summary(
                state["closed"], open_rows, state["start_date"], state["end_date"],
                invested, final, realized_pnl, book_size=simulate.book_size(state),
            ))
            state["positions"] = []
            state["status"] = "DONE"
            break

        due = [m for m in simulate._milestones(start, end, cfg.SIM_UPDATE_INTERVAL_DAYS)
               if m <= today and m.isoformat() not in state["updates_sent"]]
        if due:
            messages.append(telegram.compose_update(
                open_rows, total_cost, total_value, realized_pnl,
                len(state["closed"]), today.isoformat(),
                (today - start).days, (end - start).days,
                total_invested_all=total_cost + closed_invested,
                book_size=simulate.book_size(state),
            ))
            state["updates_sent"].extend(m.isoformat() for m in due)

    state["messages"] = messages
    state["missing_prices"] = sorted(missing)
    return state


def performance(state: dict, cfg=config, benchmark: str = "SPY",
                cache_dir: str = pricecache.CACHE_DIR) -> dict:
    """Headline numbers for a finished replay.

    Two different "return" figures matter and they are easy to confuse:

      • return_on_capital — P&L over the money actually at risk
        (SIM_CASH_PER_STOCK x SIM_MAX_POSITIONS). This is the one to compare
        against a buy-and-hold benchmark.
      • return_on_turnover — P&L over the sum of every position's cost basis.
        A strategy that recycles the same $10,000 through 26 trades reports
        $26,000 "invested" here, so this number is always the flattering-looking
        small one. It is what the live Telegram summary prints.
    """
    closed = state["closed"]
    pnl = sum(c["pnl"] for c in closed)
    turnover = sum(c.get("cost_basis", cfg.SIM_CASH_PER_STOCK) for c in closed)
    capital = cfg.SIM_CASH_PER_STOCK * cfg.SIM_MAX_POSITIONS
    out = {
        "trades": len(closed),
        "winners": sum(1 for c in closed if c["pnl"] > 0),
        "pnl": pnl,
        "capital": capital,
        "turnover": turnover,
        "return_on_capital_pct": pnl / capital * 100 if capital else 0.0,
        "return_on_turnover_pct": pnl / turnover * 100 if turnover else 0.0,
        "benchmark": None,
        "benchmark_pct": None,
    }
    bench = pricecache.load_frame(benchmark, cache_dir)
    if bench is not None:
        window = bench[(bench.index >= state["start_date"]) & (bench.index <= state["end_date"])]
        if len(window) >= 2:
            out["benchmark"] = benchmark
            out["benchmark_pct"] = float(
                window["Close"].iloc[-1] / window["Close"].iloc[0] - 1.0
            ) * 100.0
    return out


if __name__ == "__main__":
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    result = replay(start, end)
    for msg in result["messages"]:
        print(msg)
        print("\n" + "-" * 60 + "\n")
    perf = performance(result)
    print(
        f"{perf['trades']} trades, {perf['winners']} winners | "
        f"P&L ${perf['pnl']:+.0f} = {perf['return_on_capital_pct']:+.2f}% on "
        f"${perf['capital']:.0f} of capital "
        f"({perf['return_on_turnover_pct']:+.2f}% of ${perf['turnover']:.0f} turnover)"
    )
    if perf["benchmark_pct"] is not None:
        print(f"{perf['benchmark']} over the same window: {perf['benchmark_pct']:+.2f}%")
    if result["missing_prices"]:
        print("NOTE — no cached prices for:", ", ".join(result["missing_prices"]))
