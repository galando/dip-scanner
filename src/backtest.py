"""Historical backtest of the dip signals — prove the edge before trusting it.

Replays the *price-based* gates (2a hard dip, 2b stabilization, price-only
trap checks) over years of history and measures what actually happened after
each signal: forward returns at multiple horizons, win rate, performance vs
SPY over the same windows, and how much additional pain came after entry
(max adverse excursion).

It runs each backtest twice — stabilization required vs skipped — so the core
design claim ("don't catch the falling knife, buy the first stabilization")
is tested directly instead of assumed.

Honest limitation: Gate 1 (quality) cannot be replayed because yfinance has
no point-in-time fundamentals. Results are therefore a lower bound on
selectivity — the live scanner is stricter than this backtest.

Usage:
    PYTHONPATH=. python -m src.backtest --period 5y --max-tickers 100
    PYTHONPATH=. python -m src.backtest --send   # also send report via Telegram
"""
import argparse
import logging
import os
import statistics

import pandas as pd

import config
import src.data as data
import src.gates as gates
import src.telegram as telegram
import src.universe as universe
from src.indicators import compute_sma

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _regime_at(spy_df: pd.DataFrame | None, ts) -> str:
    """Regime (SPY vs its 200dma) as of timestamp ts. RISK_ON when unknown."""
    if spy_df is None:
        return "RISK_ON"
    window = spy_df.loc[:ts]
    if len(window) < 200:
        return "RISK_ON"
    sma_200 = compute_sma(window, 200).iloc[-1]
    return "RISK_ON" if window["Close"].iloc[-1] > sma_200 else "RISK_OFF"


def find_signals(
    prices_df: pd.DataFrame,
    spy_df: pd.DataFrame | None,
    cfg,
    require_stabilization: bool = True,
) -> list[int]:
    """Walk one ticker's history; return index positions where the gates fire.

    Reuses the exact same gate functions as the live scanner, applied to the
    data available up to each point in time (no lookahead). With
    require_stabilization=False, candidates failing ONLY gate 2b are also
    accepted — the falling-knife baseline the stabilization upgrade is
    measured against.
    """
    n = len(prices_df)
    signals: list[int] = []
    last_signal = -(10 ** 9)

    for i in range(cfg.BACKTEST_MIN_HISTORY, n, cfg.BACKTEST_STEP_DAYS):
        if i - last_signal < cfg.DEDUP_DAYS:
            continue

        window = prices_df.iloc[: i + 1]
        regime = _regime_at(spy_df, prices_df.index[i])

        passed, details = gates.gate_2_dip_and_stabilization(window, regime, cfg)
        if not passed:
            reason = details.get("reason", "")
            stab_only_failure = reason.startswith("Stabilization gate")
            if require_stabilization or not stab_only_failure:
                continue

        # Price-based traps only: empty fundamentals dict, hard suppress.
        trap_ok, _ = gates.gate_3_trap({}, window, cfg, trap_behavior="suppress")
        if not trap_ok:
            continue

        signals.append(i)
        last_signal = i

    return signals


def forward_return(closes, i: int, horizon: int) -> float | None:
    """Return over `horizon` trading days from index i; None if beyond data."""
    if i + horizon >= len(closes):
        return None
    return float(closes[i + horizon] / closes[i] - 1.0)


def max_adverse_excursion(closes, i: int, window: int) -> float | None:
    """Worst drawdown from entry within `window` days (how much more pain)."""
    end = min(i + window, len(closes) - 1)
    if end <= i:
        return None
    return float(min(closes[i : end + 1]) / closes[i] - 1.0)


def evaluate_signals(
    prices_df: pd.DataFrame,
    spy_df: pd.DataFrame | None,
    signals: list[int],
    cfg,
) -> list[dict]:
    """Compute forward returns, SPY excess returns, and MAE for each signal."""
    rows: list[dict] = []
    closes = prices_df["Close"].values
    spy_closes = spy_df["Close"].values if spy_df is not None else None

    for i in signals:
        ts = prices_df.index[i]
        row: dict = {
            "date": str(pd.Timestamp(ts).date()),
            "mae": max_adverse_excursion(closes, i, cfg.BACKTEST_MAE_WINDOW),
        }
        spy_i = spy_df.index.searchsorted(ts) if spy_df is not None else None
        for h in cfg.BACKTEST_HORIZONS:
            ret = forward_return(closes, i, h)
            row[f"ret_{h}"] = ret
            excess = None
            if ret is not None and spy_closes is not None and spy_i is not None:
                spy_ret = forward_return(spy_closes, spy_i, h)
                if spy_ret is not None:
                    excess = ret - spy_ret
            row[f"excess_{h}"] = excess
        rows.append(row)

    return rows


def aggregate(rows: list[dict], cfg) -> dict:
    """Aggregate per-signal rows into the headline statistics."""
    out: dict = {"n_signals": len(rows)}

    maes = [r["mae"] for r in rows if r.get("mae") is not None]
    if maes:
        out["median_mae"] = statistics.median(maes)
        out["worst_mae"] = min(maes)

    for h in cfg.BACKTEST_HORIZONS:
        rets = [r[f"ret_{h}"] for r in rows if r.get(f"ret_{h}") is not None]
        excess = [r[f"excess_{h}"] for r in rows if r.get(f"excess_{h}") is not None]
        if not rets:
            continue
        out[f"h{h}"] = {
            "n": len(rets),
            "median_ret": statistics.median(rets),
            "mean_ret": sum(rets) / len(rets),
            "win_rate": sum(1 for r in rets if r > 0) / len(rets),
            "beat_spy_rate": (sum(1 for e in excess if e > 0) / len(excess)) if excess else None,
            "median_excess": statistics.median(excess) if excess else None,
        }
    return out


def run_backtest(prices_map: dict, spy_df: pd.DataFrame | None, cfg=config) -> dict:
    """Backtest all tickers, once with stabilization required and once without."""
    results: dict = {}
    for mode, require_stab in (
        ("with_stabilization", True),
        ("without_stabilization", False),
    ):
        all_rows: list[dict] = []
        for ticker, prices_df in prices_map.items():
            if ticker == "SPY":
                continue
            try:
                sigs = find_signals(prices_df, spy_df, cfg, require_stabilization=require_stab)
                rows = evaluate_signals(prices_df, spy_df, sigs, cfg)
                for r in rows:
                    r["ticker"] = ticker
                all_rows.extend(rows)
            except Exception as e:  # one bad ticker never kills the backtest
                logger.warning("Backtest error for %s (%s): %s", ticker, mode, e)
        results[mode] = {"stats": aggregate(all_rows, cfg), "signals": all_rows}
        logger.info("%s: %d signals", mode, len(all_rows))
    return results


def format_report(results: dict, cfg=config, n_tickers: int = 0, period: str = "") -> str:
    """Human-readable report comparing stabilization ON vs OFF."""
    lines = [
        "📐 Dip Scanner Backtest Report",
        f"Universe: {n_tickers} tickers, period: {period or cfg.BACKTEST_PERIOD}",
        "(Price-based gates only — Gate 1 quality cannot be replayed historically,",
        " so the live scanner is stricter than these numbers.)",
        "",
    ]

    def _pct(x):
        return f"{x * 100:+.1f}%" if x is not None else "n/a"

    def _rate(x):
        return f"{x * 100:.0f}%" if x is not None else "n/a"

    for mode, label in (
        ("with_stabilization", "WITH stabilization (the live rule)"),
        ("without_stabilization", "WITHOUT stabilization (falling-knife baseline)"),
    ):
        stats = results.get(mode, {}).get("stats", {})
        lines.append(f"— {label} —")
        lines.append(f"Signals: {stats.get('n_signals', 0)}")
        if "median_mae" in stats:
            lines.append(
                f"Pain after entry (MAE, {cfg.BACKTEST_MAE_WINDOW}d): "
                f"median {_pct(stats['median_mae'])}, worst {_pct(stats['worst_mae'])}"
            )
        for h in cfg.BACKTEST_HORIZONS:
            s = stats.get(f"h{h}")
            if not s:
                continue
            lines.append(
                f"  {h:>3}d: median {_pct(s['median_ret'])}, "
                f"win rate {_rate(s['win_rate'])}, "
                f"beat SPY {_rate(s['beat_spy_rate'])}, "
                f"median excess {_pct(s['median_excess'])}"
            )
        lines.append("")

    with_s = results.get("with_stabilization", {}).get("stats", {})
    without_s = results.get("without_stabilization", {}).get("stats", {})
    h_key = f"h{cfg.BACKTEST_HORIZONS[1]}" if len(cfg.BACKTEST_HORIZONS) > 1 else None
    if h_key and h_key in with_s and h_key in without_s:
        diff = (with_s[h_key]["median_ret"] or 0) - (without_s[h_key]["median_ret"] or 0)
        verdict = "improves" if diff > 0 else "does NOT improve"
        lines.append(
            f"Verdict: stabilization {verdict} the median "
            f"{cfg.BACKTEST_HORIZONS[1]}d return by {_pct(diff)} per signal."
        )
    lines.append("")
    lines.append("If the edge does not beat SPY after costs, it is not an edge. "
                 "Not investment advice.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the dip scanner's price gates.")
    parser.add_argument("--period", default=config.BACKTEST_PERIOD,
                        help="yfinance history period (e.g. 2y, 5y, max)")
    parser.add_argument("--max-tickers", type=int, default=100,
                        help="Cap the universe for runtime (0 = all)")
    parser.add_argument("--send", action="store_true",
                        help="Also send the report via Telegram")
    args = parser.parse_args()

    tickers = universe.get_sp500_tickers()
    if args.max_tickers:
        tickers = tickers[: args.max_tickers]

    logger.info("Backtesting %d tickers over %s", len(tickers), args.period)
    prices_map = data.fetch_prices(tickers + ["SPY"], period=args.period)
    spy_df = prices_map.get("SPY")

    results = run_backtest(prices_map, spy_df, config)
    report = format_report(results, config, n_tickers=len(tickers), period=args.period)
    print(report)

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_ids = telegram.get_chat_ids()
        if token and chat_ids:
            for cid in chat_ids:
                telegram.send_alert(token, cid, report)
        else:
            logger.warning("Telegram credentials not set — report printed only")


if __name__ == "__main__":
    main()
