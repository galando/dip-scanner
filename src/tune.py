"""Tune the exit rules against months that already happened.

The scanner's entries and its exits are separate questions. `hold_curve` asks the
first one — how a signal behaves over the days after it fires, with no exit rule
at all — which shows where an exit *could* sit before any parameter is chosen.
`sweep` then asks the second: what the book would actually have returned under a
given set of thresholds, replayed day by day.

Both read the offline cache, so they are as good (and as limited) as the months
in it. Two months is a small sample: a grid search over it will find a winner by
construction. Treat anything here as a hypothesis to check on more history, not
as a settled answer — which is why `sweep` reports every window separately
instead of only the average, and why `pick` prefers settings that hold up on all
of them over settings that win on one.
"""
import itertools
import types
from datetime import date

import config
import src.pricecache as pricecache
import src.replay as replay

# The knobs that decide when a position is sold. Values well outside their
# natural range switch a rule off: RSI never exceeds 100, and a position cannot
# gain or lose 999%.
OFF = 999.0
EXIT_KNOBS = (
    "SIM_RSI_EXIT",
    "SIM_TAKE_PROFIT_PCT",
    "SIM_STOP_LOSS_PCT",
    "SIM_THESIS_BREAK_MIN_LOSS_PCT",
)


def with_overrides(**overrides) -> types.SimpleNamespace:
    """A stand-in for the config module with some thresholds replaced."""
    base = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    unknown = set(overrides) - set(base)
    if unknown:
        raise KeyError(f"not config settings: {sorted(unknown)}")
    return types.SimpleNamespace(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# What a signal does after it fires, before any exit rule is applied
# --------------------------------------------------------------------------- #
def hold_curve(windows: list[tuple[date, date]], horizons: range = range(1, 22),
               balanced: bool = True, cache_dir: str = pricecache.CACHE_DIR,
               alerts_path: str = replay.ALERTS_PATH) -> list[dict]:
    """Average forward return of every signal in the windows, by holding period.

    One row per horizon: hold each signal exactly N sessions from its entry
    close and see what it made. This is the exit-free baseline — if the curve
    keeps rising out to N days, an exit that fires before N is leaving money on
    the table.

    `balanced` (the default) keeps only signals that have data out to the longest
    horizon, so every row describes the same set of trades. Without it the late
    signals drop out of the long horizons and the curve partly reflects a
    changing sample rather than a changing holding period.
    """
    alerts = replay.load_alerts(alerts_path)
    entries: list[tuple[str, str]] = []           # (ticker, entry session)
    for start, end in windows:
        sessions = pricecache.trading_days(start, end, cache_dir)
        for day, tickers in replay._actionable(alerts, sessions).items():
            if day == sessions[-1].isoformat():
                continue                          # the book never buys on the last day
            for ticker in tickers:
                entries.append((ticker, day))

    if balanced:
        longest = max(horizons)
        entries = [
            (ticker, day) for ticker, day in entries
            if (frame := pricecache.load_frame(ticker, cache_dir)) is not None
            and 0 <= frame.index.get_indexer([day])[0] < len(frame) - longest
        ]

    rows = []
    for n in horizons:
        returns = []
        for ticker, day in entries:
            frame = pricecache.load_frame(ticker, cache_dir)
            if frame is None:
                continue
            # Frames are right-aligned to the shared calendar, so locate the
            # entry on the frame's own index rather than the calendar's.
            where = frame.index.get_indexer([day])[0]
            if where < 0 or where + n >= len(frame):
                continue
            entry = float(frame["Close"].iloc[where])
            returns.append((float(frame["Close"].iloc[where + n]) / entry - 1.0) * 100.0)
        if not returns:
            continue
        returns.sort()
        rows.append({
            "days": n,
            "signals": len(returns),
            "mean_pct": sum(returns) / len(returns),
            "median_pct": returns[len(returns) // 2],
            "win_rate_pct": sum(1 for r in returns if r > 0) / len(returns) * 100.0,
        })
    return rows


# --------------------------------------------------------------------------- #
# What the book returns under a given set of thresholds
# --------------------------------------------------------------------------- #
def evaluate(settings: dict, windows: list[tuple[date, date]],
             cache_dir: str = pricecache.CACHE_DIR,
             alerts_path: str = replay.ALERTS_PATH) -> dict:
    """Replay every window under one settings dict."""
    cfg = with_overrides(**settings)
    per_window = []
    for start, end in windows:
        state = replay.replay(start, end, cfg=cfg, cache_dir=cache_dir,
                              alerts_path=alerts_path)
        perf = replay.performance(state, cfg=cfg, cache_dir=cache_dir)
        per_window.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "return_pct": perf["return_on_capital_pct"],
            "benchmark_pct": perf["benchmark_pct"],
            "trades": perf["trades"],
            "winners": perf["winners"],
        })
    returns = [w["return_pct"] for w in per_window]
    excess = [w["return_pct"] - (w["benchmark_pct"] or 0.0) for w in per_window]
    return {
        "settings": settings,
        "windows": per_window,
        "mean_pct": sum(returns) / len(returns),
        "worst_pct": min(returns),
        "mean_excess_pct": sum(excess) / len(excess),
        "worst_excess_pct": min(excess),
        "trades": sum(w["trades"] for w in per_window),
    }


def sweep(grid: dict[str, list], windows: list[tuple[date, date]],
          cache_dir: str = pricecache.CACHE_DIR,
          alerts_path: str = replay.ALERTS_PATH) -> list[dict]:
    """Evaluate the full cartesian product of `grid`, worst window first."""
    keys = list(grid)
    results = [
        evaluate(dict(zip(keys, combo)), windows, cache_dir, alerts_path)
        for combo in itertools.product(*(grid[k] for k in keys))
    ]
    results.sort(key=lambda r: (-r["worst_excess_pct"], -r["mean_excess_pct"]))
    return results


def pick(results: list[dict], baseline: dict) -> dict:
    """The best setting that also beats the baseline in *every* window.

    Ranking on the average alone rewards a setting that wins big in one month and
    loses in the other, which is exactly the overfit a two-month sample invites.
    Requiring every window to improve is a weak guard, but it is a guard.
    """
    base = next(r for r in results if r["settings"] == baseline)
    base_by_window = {w["start"]: w["return_pct"] for w in base["windows"]}
    robust = [
        r for r in results
        if all(w["return_pct"] >= base_by_window[w["start"]] for w in r["windows"])
    ]
    return {"baseline": base, "robust": robust}


# The sell reasons are bilingual free text built for a human reading Telegram.
# Match on the English marker each rule embeds rather than slicing the string.
_RULE_MARKERS = (
    ("stop-loss", "stop-loss"),
    ("target hit", "take-profit"),
    ("bounce done", "bounce done (RSI)"),
    ("Dip not over", "thesis break"),
    ("month ended", "month ended (not a rule)"),
)


def _rule_of(sell_reason: str) -> str:
    for marker, label in _RULE_MARKERS:
        if marker in sell_reason:
            return label
    return "other"


def exit_cost(windows: list[tuple[date, date]], cfg=config,
              cache_dir: str = pricecache.CACHE_DIR,
              alerts_path: str = replay.ALERTS_PATH) -> list[dict]:
    """Per exit rule: what the sold positions did afterwards, if left alone.

    For every trade the book closed, re-price the same position at the end of its
    window and compare. Grouped by the rule that triggered the sale, this shows
    which rule is cutting winners and which is cutting losers.

    It is a diagnostic, not the answer, and it can point the wrong way. Holding a
    position also occupies a slot a later signal would have used, which this
    cannot see. On the two cached months it reports the thesis-break rule as
    costing ~2% per trade, yet `sweep` shows loosening that rule makes June
    materially worse: the positions it cut were losers, and the freed slots went
    to better names. Use this to see *where* to look, then confirm with `sweep`.
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    for start, end in windows:
        state = replay.replay(start, end, cfg=cfg, cache_dir=cache_dir,
                              alerts_path=alerts_path)
        for trade in state["closed"]:
            frame = pricecache.load_frame(trade["ticker"], cache_dir)
            if frame is None:
                continue
            held = frame[frame.index <= str(end)]
            if held.empty:
                continue
            realised = trade["pnl_pct"]
            if_held = (float(held["Close"].iloc[-1]) / trade["entry_price"] - 1.0) * 100.0
            buckets.setdefault(_rule_of(trade["sell_reason"]), []).append((realised, if_held))

    rows = []
    for label, pairs in buckets.items():
        realised = [p[0] for p in pairs]
        if_held = [p[1] for p in pairs]
        rows.append({
            "rule": label,
            "trades": len(pairs),
            "realised_pct": sum(realised) / len(realised),
            "if_held_pct": sum(if_held) / len(if_held),
            "left_behind_pct": (sum(if_held) - sum(realised)) / len(pairs),
        })
    rows.sort(key=lambda r: -r["left_behind_pct"] * r["trades"])
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
# The months currently in the cache. Both are full SIM_DURATION_DAYS windows:
# the first is the June 2026 run extended to a proper month, the second the
# month ending on the last cached session.
WINDOWS = [
    (date(2026, 6, 8), date(2026, 7, 8)),
    (date(2026, 7, 28), date(2026, 8, 27)),
]

GRID = {
    "SIM_RSI_EXIT": [60, 65, 70, 75, 80, 101],
    "SIM_TAKE_PROFIT_PCT": [12, 15, 20, 25, 30, OFF],
    "SIM_STOP_LOSS_PCT": [8, 10, 12, 15, 20],
    "SIM_THESIS_BREAK_MIN_LOSS_PCT": [3, 5, 8, OFF],
}


def _fmt(settings: dict) -> str:
    short = {"SIM_RSI_EXIT": "RSI", "SIM_TAKE_PROFIT_PCT": "TP",
             "SIM_STOP_LOSS_PCT": "SL", "SIM_THESIS_BREAK_MIN_LOSS_PCT": "TB"}
    return "  ".join(
        f"{short.get(k, k)}={'off' if v >= OFF or (k == 'SIM_RSI_EXIT' and v > 100) else v:g}"
        if not isinstance(v, str) else f"{short.get(k, k)}={v}"
        for k, v in settings.items()
    )


def _print_curve() -> None:
    print("Forward return of every cached signal, no exit rule applied:\n")
    print(" days  signals    mean%   median%    win%")
    for row in hold_curve(WINDOWS):
        print(f"  {row['days']:3d}  {row['signals']:7d}   {row['mean_pct']:+6.2f}   "
              f"{row['median_pct']:+7.2f}   {row['win_rate_pct']:5.1f}")


def _print_exit_cost() -> None:
    print("Per exit rule: average realised move vs. the move if the position had"
          "\nbeen left alone until the end of its month.\n")
    print("trades   realised   if held   left behind   rule")
    for row in exit_cost(WINDOWS):
        print(f"{row['trades']:6d}   {row['realised_pct']:+7.2f}%  {row['if_held_pct']:+7.2f}%   "
              f"{row['left_behind_pct']:+9.2f}%   {row['rule']}")


def _print_sweep(top: int = 15) -> None:
    baseline = {k: getattr(config, k) for k in EXIT_KNOBS}
    results = sweep(GRID, WINDOWS)
    chosen = pick(results, baseline)
    base = chosen["baseline"]

    def line(entry: dict) -> str:
        per = "  ".join(f"{w['return_pct']:+6.2f}%" for w in entry["windows"])
        return (f"{per}   worst-vs-SPY {entry['worst_excess_pct']:+6.2f}%   "
                f"{entry['trades']:3d} trades   {_fmt(entry['settings'])}")

    print(f"{len(results)} combinations over {len(WINDOWS)} windows.")
    print("Columns: return per window, then worst window's excess over SPY.\n")
    print("current:  " + line(base))
    print(f"\nBest {top} that beat current in EVERY window "
          f"({len(chosen['robust'])} of {len(results)} qualify):\n")
    for entry in chosen["robust"][:top]:
        print("          " + line(entry))


if __name__ == "__main__":
    import sys
    command = sys.argv[1] if len(sys.argv) > 1 else "curve"
    if command == "curve":
        _print_curve()
    elif command == "cost":
        _print_exit_cost()
    elif command == "sweep":
        _print_sweep()
    else:
        raise SystemExit("usage: python -m src.tune [curve|cost|sweep]")
