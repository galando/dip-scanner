"""Does a tuning result survive being tested away from where it was fitted?

`src/tune.py` finds settings that do well on the months in the cache. That is
fitting. This module is the check: it re-tests a candidate on windows it was not
chosen on, and puts an interval around the improvement so a difference that is
really noise is visible as noise.

Three checks, weakest guarantee first:

  `non_overlapping`  Windows that share no sessions. The rolling windows used
                     for tuning overlap heavily, so eight of them carry nowhere
                     near eight windows of information; these carry one each.

  `walk_forward`     Tune on the earlier windows only, then score the winner on
                     the later ones. This is the closest thing available to an
                     out-of-sample test: the test windows had no vote in the
                     choice.

  `bootstrap`        Resample windows with replacement to get an interval around
                     the difference between two settings. The resampling unit is
                     the window, so with overlapping windows the interval is
                     optimistic — read it as a floor on the uncertainty, never a
                     p-value.

None of this manufactures information the cache does not hold. A month of data
tuned twelve ways is still a month of data; these checks make that visible
rather than fixing it.
"""
import random
import statistics
from datetime import date, timedelta

import config
import src.pricecache as pricecache
import src.tune as tune


def non_overlapping(start: date, end: date, days: int = 30,
                    cache_dir: str = pricecache.CACHE_DIR) -> list[tuple[date, date]]:
    """Consecutive `days`-long windows between start and end that share no sessions."""
    windows, cursor = [], start
    while cursor + timedelta(days=days) <= end:
        stop = cursor + timedelta(days=days)
        if pricecache.trading_days(cursor, stop, cache_dir):
            windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return windows


def rolling(start: date, end: date, days: int = 30, step_days: int = 7,
            cache_dir: str = pricecache.CACHE_DIR) -> list[tuple[date, date]]:
    """Overlapping windows — more of them, but far less information per window."""
    windows, cursor = [], start
    while cursor + timedelta(days=days) <= end:
        stop = cursor + timedelta(days=days)
        if pricecache.trading_days(cursor, stop, cache_dir):
            windows.append((cursor, stop))
        cursor += timedelta(days=step_days)
    return windows


def walk_forward(grid: dict[str, list], windows: list[tuple[date, date]],
                 baseline: dict, train_frac: float = 0.5,
                 cache_dir: str = pricecache.CACHE_DIR,
                 alerts_path: str = tune.replay.ALERTS_PATH,
                 purge: bool = True) -> dict:
    """Choose settings on the earlier windows, then score them on the later ones.

    The winner is picked by worst training window (not mean), because a setting
    that wins on average by collapsing in one month is the failure mode this is
    guarding against.

    Splitting a list of ROLLING windows in half does not separate the data: with
    30-day windows on a 7-day step, the first test window starts three weeks
    before the last training window ends, and the same trades sit on both sides
    of the split. `purge` drops every test window that begins on or before the
    training half ends, so "the test windows had no vote in the choice" is true
    rather than nearly true. It also means a short cache has no clean split at
    all, which is worth an exception rather than a comfortable number.
    """
    if len(windows) < 4:
        raise ValueError("walk-forward needs at least four windows to split")
    cut = max(2, int(len(windows) * train_frac))
    train, test = windows[:cut], windows[cut:]
    if purge:
        train_end = max(b for _, b in train)
        test = [w for w in test if w[0] > train_end]
        if not test:
            raise ValueError(
                f"no test window starts after the training half ends "
                f"({train_end}); every candidate test window shares sessions "
                f"with the windows the settings were chosen on, so there is no "
                f"out-of-sample half here. A clean split needs the cache to "
                f"cover about twice the span it does."
            )

    ranked = tune.sweep(grid, train, cache_dir, alerts_path)
    winner = ranked[0]["settings"]

    def score(settings, on):
        r = tune.evaluate(settings, on, cache_dir, alerts_path)
        return {"mean_pct": r["mean_pct"], "worst_pct": r["worst_pct"],
                "windows": r["windows"]}

    return {
        "train_windows": [(a.isoformat(), b.isoformat()) for a, b in train],
        "test_windows": [(a.isoformat(), b.isoformat()) for a, b in test],
        "chosen": winner,
        "chosen_train": score(winner, train),
        "chosen_test": score(winner, test),
        "baseline_train": score(baseline, train),
        "baseline_test": score(baseline, test),
        "candidates": len(ranked),
    }


def bootstrap(settings: dict, baseline: dict, windows: list[tuple[date, date]],
              draws: int = 2000, seed: int = 20260827,
              cache_dir: str = pricecache.CACHE_DIR,
              alerts_path: str = tune.replay.ALERTS_PATH) -> dict:
    """Interval around (settings - baseline), resampling whole windows.

    Both settings are replayed on the same windows first, so every draw compares
    them on identical months — the pairing removes the variation between months,
    which is much larger than the effect being measured.
    """
    a = tune.evaluate(settings, windows, cache_dir, alerts_path)
    b = tune.evaluate(baseline, windows, cache_dir, alerts_path)
    deltas = [x["return_pct"] - y["return_pct"]
              for x, y in zip(a["windows"], b["windows"])]

    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [deltas[rng.randrange(len(deltas))] for _ in deltas]
        means.append(sum(sample) / len(sample))
    means.sort()
    return {
        "per_window_delta": deltas,
        "observed_mean_delta": sum(deltas) / len(deltas),
        "ci_low": means[int(0.025 * draws)],
        "ci_high": means[int(0.975 * draws)],
        "share_positive": sum(1 for m in means if m > 0) / draws,
        "windows_improved": sum(1 for d in deltas if d > 0),
        "windows_unchanged": sum(1 for d in deltas if d == 0),
        "windows_worsened": sum(1 for d in deltas if d < 0),
        "n_windows": len(deltas),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
ADOPTED = {"SIM_MIN_HOLD_SESSIONS": config.SIM_MIN_HOLD_SESSIONS}
BEFORE = {"SIM_MIN_HOLD_SESSIONS": 0}


def direction_phrase(up: int, down: int, n: int) -> str:
    """How to describe a win/loss split, without overstating it.

    Split out so the wording is tested against the counts directly: the verdict
    used to be fixed text, and a bug fix that moved the numbers left the
    sentence beside them saying the same thing as before.
    """
    if up and not down:
        return f"Direction is consistent: the change helps in {up} of {n} overlapping windows and hurts in none."
    if up > down:
        return f"Direction leans positive: the change helps in {up} of {n} overlapping windows, hurts in {down}."
    if up == down:
        return f"Direction is a coin flip: the change helps in {up} of {n} overlapping windows and hurts in {down}."
    return f"Direction is negative: the change hurts in {down} of {n} overlapping windows and helps in {up}."


def _span(cache_dir: str = pricecache.CACHE_DIR) -> tuple[date, date]:
    dates = pricecache.load_dates(cache_dir)
    return date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])


def _report() -> None:
    first_alert = min(tune.replay.load_alerts())
    cache_start, end = _span()
    start = max(cache_start, date.fromisoformat(first_alert))

    over = rolling(start, end)
    apart = non_overlapping(start, end)

    # Three different things bound this, and quoting only the last one sent
    # readers off to deepen the price cache when the alert log was the
    # constraint. Name all three.
    depths = [len(df) for t in pricecache.available_tickers()
              if (df := pricecache.load_frame(t)) is not None]
    calendar = pricecache.load_dates()
    if depths:
        print(f"Price calendar   {calendar[0]} .. {calendar[-1]}  ({len(calendar)} sessions)")
        print(f"Shallowest ticker holds {min(depths)} bars, from "
              f"{calendar[-min(depths)]}")
    print(f"Alert log        {first_alert} .. {max(tune.replay.load_alerts())}")
    print(f"Evaluated on     {start} .. {end}  (the later of the two starts)\n")
    print(f"{len(over)} overlapping windows vs {len(apart)} that share no sessions.")
    print("The second number is the honest sample size.\n")

    print("=" * 68)
    print("1. The adopted change on windows that share no sessions")
    print("=" * 68)
    for label, settings in (("before", BEFORE), ("after ", ADOPTED)):
        r = tune.evaluate(settings, apart)
        per = "  ".join(f"{w['return_pct']:+6.2f}%" for w in r["windows"])
        print(f"  {label}: {per}    mean {r['mean_pct']:+6.2f}%  worst {r['worst_pct']:+6.2f}%")

    print()
    print("=" * 68)
    print("2. Paired bootstrap of the difference (resampling whole windows)")
    print("=" * 68)
    for label, wins in (("overlapping", over), ("non-overlapping", apart)):
        b = bootstrap(ADOPTED, BEFORE, wins)
        print(f"  {label:16} n={b['n_windows']:2d}  "
              f"mean {b['observed_mean_delta']:+5.2f}pp  "
              f"95% CI [{b['ci_low']:+5.2f}, {b['ci_high']:+5.2f}]  "
              f"P(>0)={b['share_positive']:.0%}  "
              f"{b['windows_improved']}up/{b['windows_unchanged']}flat/"
              f"{b['windows_worsened']}down")

    print()
    print("=" * 68)
    print("3. Walk-forward: settings chosen on the earlier half only")
    print("=" * 68)
    grid = {"SIM_MIN_HOLD_SESSIONS": [0, 3, 5, 10, 15],
            "SIM_RSI_EXIT": [60, 70, 101],
            "SIM_THESIS_BREAK_MIN_LOSS_PCT": [3, 5, 8]}
    try:
        wf = walk_forward(grid, over, BEFORE)
    except ValueError as exc:
        wf, adopted = None, None
        print(f"  Not available: {exc}")
        print(f"  Nothing is reported here rather than a split whose halves share")
        print(f"  sessions — that number would read as out-of-sample and would not be.")
    else:
        print(f"  trained on {len(wf['train_windows'])} windows, "
              f"tested on {len(wf['test_windows'])} ({wf['candidates']} candidates), "
              f"overlapping test windows purged")
        print(f"  chose: {wf['chosen']}")
        print(f"  {'':22}{'train mean':>12}{'test mean':>12}{'test worst':>12}")
        print(f"  {'baseline':22}{wf['baseline_train']['mean_pct']:+11.2f}%"
              f"{wf['baseline_test']['mean_pct']:+11.2f}%{wf['baseline_test']['worst_pct']:+11.2f}%")
        print(f"  {'walk-forward choice':22}{wf['chosen_train']['mean_pct']:+11.2f}%"
              f"{wf['chosen_test']['mean_pct']:+11.2f}%{wf['chosen_test']['worst_pct']:+11.2f}%")
        test = [tuple(map(date.fromisoformat, w)) for w in wf["test_windows"]]
        adopted = tune.evaluate(ADOPTED, test)
        print(f"  {'adopted setting':22}{'—':>11}{adopted['mean_pct']:+11.2f}%"
              f"{adopted['worst_pct']:+11.2f}%")

    print("\n" + "=" * 68)
    print("Verdict")
    print("=" * 68)
    b_over = bootstrap(ADOPTED, BEFORE, over)
    b_apart = bootstrap(ADOPTED, BEFORE, apart)

    # Every sentence below is derived from the numbers printed above it. An
    # earlier version stated its conclusions as fixed text, which stayed on the
    # page unchanged after a bug fix moved the very numbers beside it.
    print(f"  {direction_phrase(b_over['windows_improved'], b_over['windows_worsened'], b_over['n_windows'])}")

    spans_zero = b_apart["ci_low"] <= 0 <= b_apart["ci_high"]
    size = "not established" if spans_zero else "measurable on this sample"
    print(f"  Size is {size}: on the {b_apart['n_windows']} windows that share no")
    print(f"  sessions the 95% interval is [{b_apart['ci_low']:+.2f}, {b_apart['ci_high']:+.2f}] pp — it "
          f"{'includes' if spans_zero else 'excludes'} zero.")

    if wf is None:
        print("  No out-of-sample check: the cache is too short to split without")
        print("  the halves sharing sessions, so the only evidence here is the")
        print("  direction above and an interval that includes zero.")
        print(f"\n  Deepen the cache and re-run to turn this from a direction into a size:")
        print(f"      PYTHONPATH=. python -m src.cachebuild --period 5y")
        return

    beats_base = adopted["mean_pct"] > wf["baseline_test"]["mean_pct"]
    beats_wf = adopted["mean_pct"] > wf["chosen_test"]["mean_pct"]
    if beats_base and beats_wf:
        wf_line = ("The adopted setting was not chosen on the test windows and still beats\n"
                   "  both the baseline and the walk-forward winner there")
        wf_tail = "which is weak evidence for it\n  and against the busier settings."
    elif beats_base:
        wf_line = ("The adopted setting beats the baseline on the test windows but not the\n"
                   "  walk-forward winner")
        wf_tail = "so the choice is defensible but not the best the grid offers."
    else:
        wf_line = ("The adopted setting does NOT beat the baseline on the test windows it was\n"
                   "  not chosen on")
        wf_tail = "which is evidence against it."
    print(f"  {wf_line} ({adopted['mean_pct']:+.2f}% vs "
          f"{wf['baseline_test']['mean_pct']:+.2f}% and {wf['chosen_test']['mean_pct']:+.2f}%), {wf_tail}")

    print(f"\n  Deepen the cache and re-run to turn this from a direction into a size:")
    print(f"      PYTHONPATH=. python -m src.cachebuild --period 5y")


if __name__ == "__main__":
    _report()
