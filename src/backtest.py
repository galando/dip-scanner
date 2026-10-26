"""Historical backtest of the dip signals — prove the edge before trusting it.

Replays the *price-based* gates (2a hard dip, 2b stabilization, price-only
trap checks) over years of history and measures what actually happened after
each signal: forward returns at multiple horizons, win rate, performance vs
SPY over the same windows, and how much additional pain came after entry
(max adverse excursion).

It runs each backtest twice — stabilization required vs skipped — so the core
design claim ("don't catch the falling knife, buy the first stabilization")
is tested directly instead of assumed.

Gate 1 (quality) and the 8-K trap need fundamentals as they stood on each
signal date, which yfinance cannot give. With SEC_USER_AGENT set they are
rebuilt from SEC filings (src/sec.py), counting only what was filed BEFORE the
signal session, and a third run without them measures what they add on the
same tickers. Without SEC access the backtest replays the price gates only and
the live scanner is stricter than its numbers.

Usage:
    PYTHONPATH=. python -m src.backtest --period 5y --max-tickers 100
    PYTHONPATH=. python -m src.backtest --offline    # prices from data/prices
    PYTHONPATH=. python -m src.backtest --no-sec     # price gates only, even with SEC set
    PYTHONPATH=. python -m src.backtest --send       # also send report via Telegram
"""
import argparse
import logging
import os
import statistics
from datetime import timedelta

import pandas as pd

import config
import src.data as data
import src.gates as gates
import src.pricecache as pricecache
import src.sec as sec
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


def _fundamentals_at(company: "sec.Company", prices_df: pd.DataFrame, i: int) -> dict:
    """Gate 1 and 8-K inputs as of session i, from what was filed BEFORE that day.

    A filing dated the signal session may have landed after the close the
    backtest buys at, so it waits for the next session. Strictly-before costs
    at most one day of information and never adds lookahead.
    """
    day = pd.Timestamp(prices_df.index[i]).date()
    fund = sec.fundamentals_asof(company, known_by=day - timedelta(days=1),
                                 price=float(prices_df["Close"].iloc[i]))
    fund["secFilings"] = [f for f in company.filings if f["date"] < day.isoformat()]
    fund["secSic"] = company.sic
    return fund


def find_signals(
    prices_df: pd.DataFrame,
    spy_df: pd.DataFrame | None,
    cfg,
    require_stabilization: bool = True,
    company: "sec.Company | None" = None,
    funnel: dict | None = None,
) -> list[int]:
    """Walk one ticker's history; return index positions where the gates fire.

    Reuses the exact same gate functions as the live scanner, applied to the
    data available up to each point in time (no lookahead). With
    require_stabilization=False, candidates failing ONLY gate 2b are also
    accepted — the falling-knife baseline the stabilization upgrade is
    measured against.

    With `company` (SEC data for this ticker), Gate 1 and the 8-K trap run too,
    on point-in-time fundamentals. `funnel`, when given, counts the windows
    each of them rejected: "quality" (thresholds), "no_sec_data" (a field the
    filings could not support) and "filing" (a hard 8-K item).
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

        fund: dict = {}
        if company is not None:
            fund = _fundamentals_at(company, prices_df, i)
            passed_1, _ = gates.gate_1_quality(fund, cfg)
            if not passed_1:
                if funnel is not None:
                    missing = (not fund.get("negativeEquity")
                               and any(fund.get(f) is None for f in sec.QUALITY_FIELDS))
                    funnel["no_sec_data" if missing else "quality"] += 1
                continue

        # Price-based traps (hard suppress), plus 8-K events when SEC data is in.
        trap_ok, trap_details = gates.gate_3_trap(fund, window, cfg, trap_behavior="suppress")
        if not trap_ok:
            if funnel is not None and str(trap_details.get("reason", "")).startswith("SEC 8-K"):
                funnel["filing"] += 1
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


def run_backtest(prices_map: dict, spy_df: pd.DataFrame | None, cfg=config,
                 companies: dict | None = None) -> dict:
    """Backtest all tickers, once with stabilization required and once without.

    With `companies` ({ticker: sec.Company}), both runs also apply Gate 1 and
    the 8-K trap point in time, and a third run ("price_only") repeats the live
    rule without them. All three then cover only the tickers SEC had data for,
    so every row compares the same companies.
    """
    results: dict = {}
    tickers = [t for t in prices_map if t != "SPY"]
    modes = [("with_stabilization", True, True), ("without_stabilization", False, True)]
    if companies is not None:
        results["sec"] = {"tickers": sum(1 for t in tickers if t in companies),
                          "of": len(tickers)}
        tickers = [t for t in tickers if t in companies]
        modes.append(("price_only", True, False))
    for mode, require_stab, use_sec in modes:
        all_rows: list[dict] = []
        funnel = {"quality": 0, "no_sec_data": 0, "filing": 0}
        for ticker in tickers:
            prices_df = prices_map[ticker]
            company = companies.get(ticker) if companies is not None and use_sec else None
            try:
                sigs = find_signals(prices_df, spy_df, cfg, require_stabilization=require_stab,
                                    company=company, funnel=funnel)
                rows = evaluate_signals(prices_df, spy_df, sigs, cfg)
                for r in rows:
                    r["ticker"] = ticker
                all_rows.extend(rows)
            except Exception as e:  # one bad ticker never kills the backtest
                logger.warning("Backtest error for %s (%s): %s", ticker, mode, e)
        results[mode] = {"stats": aggregate(all_rows, cfg), "signals": all_rows}
        if companies is not None and use_sec:
            results[mode]["funnel"] = funnel
        logger.info("%s: %d signals", mode, len(all_rows))
    return results


def load_companies(tickers: list[str], prices_map: dict) -> dict:
    """SEC data for every ticker that has it, reaching back as far as the prices do."""
    starts = [pd.Timestamp(df.index[0]).date() for df in prices_map.values() if len(df)]
    since = min(starts) if starts else None
    companies = {}
    for n, ticker in enumerate(tickers, start=1):
        company = sec.load_company(ticker, include_facts=True, since=since)
        if company is not None and company.facts:
            companies[ticker] = company
        if n % 25 == 0:
            logger.info("SEC data: %d/%d tickers fetched, %d usable", n, len(tickers), len(companies))
        if not sec.enabled():          # SEC refused us mid-run; stop asking
            break
    logger.info("SEC data for %d of %d tickers", len(companies), len(tickers))
    return companies


def format_report(results: dict, cfg=config, n_tickers: int = 0, period: str = "") -> str:
    """Human-readable report comparing stabilization ON vs OFF."""
    sec_cov = results.get("sec")
    lines = [
        "📐 Dip Scanner Backtest Report",
        f"Universe: {n_tickers} tickers, period: {period or cfg.BACKTEST_PERIOD}",
    ]
    if sec_cov:
        lines += [
            "Gate 1 (quality) and the 8-K trap replayed point in time from SEC filings,",
            f"on the {sec_cov['tickers']} of {sec_cov['of']} tickers SEC had data for "
            f"(the others are left out of every row).",
            "",
        ]
    else:
        lines += [
            "(Price-based gates only — without SEC data Gate 1 quality cannot be replayed",
            " historically, so the live scanner is stricter than these numbers.)",
            "",
        ]

    def _pct(x):
        return f"{x * 100:+.1f}%" if x is not None else "n/a"

    def _rate(x):
        return f"{x * 100:.0f}%" if x is not None else "n/a"

    sections = [
        ("with_stabilization", "WITH stabilization (the live rule)"),
        ("without_stabilization", "WITHOUT stabilization (falling-knife baseline)"),
    ]
    if "price_only" in results:
        sections.append(("price_only", "WITHOUT quality and 8-K checks (price gates only, same tickers)"))
    for mode, label in sections:
        stats = results.get(mode, {}).get("stats", {})
        lines.append(f"— {label} —")
        lines.append(f"Signals: {stats.get('n_signals', 0)}")
        funnel = results.get(mode, {}).get("funnel")
        if funnel:
            lines.append(
                f"Dips stopped by quality: {funnel['quality']}, by missing SEC figures: "
                f"{funnel['no_sec_data']}, by an 8-K: {funnel['filing']}")
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
    price_only = results.get("price_only", {}).get("stats", {})
    if h_key and h_key in with_s and h_key in price_only:
        diff = (with_s[h_key]["median_ret"] or 0) - (price_only[h_key]["median_ret"] or 0)
        verdict = "improves" if diff > 0 else "does NOT improve"
        lines.append(
            f"Verdict: the quality and 8-K checks {verdict} the median "
            f"{cfg.BACKTEST_HORIZONS[1]}d return by {_pct(diff)} per signal "
            f"({with_s['n_signals']} signals vs {price_only['n_signals']})."
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
    parser.add_argument("--offline", action="store_true",
                        help="Read data/prices instead of the live price feed")
    parser.add_argument("--no-sec", action="store_true",
                        help="Price gates only, even when SEC_USER_AGENT is set")
    args = parser.parse_args()

    if args.offline:
        tickers = [t for t in pricecache.available_tickers() if t != "SPY"]
        if args.max_tickers:
            tickers = tickers[: args.max_tickers]
        loaded = {t: df for t in tickers + ["SPY"]
                  if (df := pricecache.load_frame(t)) is not None}
        # A ticker with fewer than BACKTEST_MIN_HISTORY sessions can never emit a
        # signal. Counting the deepest one would let a single long series report
        # a full-universe backtest whose signals came from a handful of tickers.
        deep = {t: df for t, df in loaded.items()
                if len(df) > config.BACKTEST_MIN_HISTORY}
        shallow = sorted(set(loaded) - set(deep) - {"SPY"})

        # SPY is the benchmark and the regime input, never a signal candidate, so
        # the depth filter must not remove it: dropping it turns every date
        # RISK_ON (the looser gate-2 thresholds) and every excess return None,
        # which reads as a completed backtest rather than as one missing its
        # control. It needs 200 sessions to place SPY against its own 200dma —
        # more than BACKTEST_MIN_HISTORY asks of a candidate — so say plainly
        # which of the two it fails.
        spy_cached = loaded.get("SPY")
        if spy_cached is not None:
            deep["SPY"] = spy_cached
        if spy_cached is None:
            logger.warning("no cached SPY: regime is RISK_ON throughout and the "
                           "report carries no benchmark. Cache SPY and re-run.")
        elif len(spy_cached) < 200:
            logger.warning("cached SPY holds %d sessions, fewer than the 200 needed "
                           "for its own 200dma: regime is RISK_ON throughout. The "
                           "benchmark is still computed.", len(spy_cached))
        if not deep or (len(deep) == 1 and "SPY" in deep):
            deepest = max((len(df) for df in loaded.values()), default=0)
            raise SystemExit(
                f"no cached ticker has more than BACKTEST_MIN_HISTORY "
                f"({config.BACKTEST_MIN_HISTORY}) sessions — the deepest holds "
                f"{deepest}, so not one signal can be emitted. Deepen the cache:\n"
                f"    PYTHONPATH=. python -m src.cachebuild --period 5y"
            )
        if shallow:
            logger.warning(
                "%d of %d cached tickers are too shallow to emit signals and are "
                "excluded: %s", len(shallow), len(tickers),
                ", ".join(shallow[:8]) + ("..." if len(shallow) > 8 else ""))
        prices_map = deep
        tickers = [t for t in deep if t != "SPY"]
        # SPY is in `deep` unconditionally now, so exclude it from the depth
        # the report quotes — that number describes the candidates scanned.
        depth = min(len(df) for t, df in deep.items() if t != "SPY")
        args.period = f"cache ({depth}+ sessions, {len(tickers)} tickers)"
        logger.info("Backtesting %d cached tickers over %s", len(tickers), args.period)
    else:
        tickers = universe.get_sp500_tickers()
        if args.max_tickers:
            tickers = tickers[: args.max_tickers]
        logger.info("Backtesting %d tickers over %s", len(tickers), args.period)
        prices_map = data.fetch_prices(tickers + ["SPY"], period=args.period)
    spy_df = prices_map.get("SPY")

    companies = None
    if sec.enabled() and not args.no_sec:
        companies = load_companies(tickers, prices_map) or None
        if companies is None:
            logger.warning("SEC returned no usable data; replaying the price gates only")

    results = run_backtest(prices_map, spy_df, config, companies=companies)
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
