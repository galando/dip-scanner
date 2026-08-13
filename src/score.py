"""Composite opportunity score — rank candidates that passed all four gates.

The gates are binary: pass or fail. The score is ordinal: among today's
passers, which are the *best* risk/reward entries? On a broad sell-off day
dozens of stocks can qualify; alerting all of them buries the good ones.
Score every passer 0-100, send only the top few.

Score components:

    Dip depth          0-25   how extreme the drop is vs the stock's own vol
    Timing             0-30   stabilization strength, bounce freshness,
                              volume confirmation
    Quality            0-25   ROE / operating margin / debt
    Relative strength  0-10   holding up better than SPY over the last month
    Trap penalty       0 to -30  each red flag subtracts

The score never overrides the gates — a failing stock is never scored.
"""
import logging

import pandas as pd

from src.indicators import (
    detect_volume_confirmation,
    compute_relative_strength,
    days_since_low,
)

logger = logging.getLogger(__name__)


def _scale(x: float, lo: float, hi: float) -> float:
    """Map x from [lo, hi] to [0, 1], clamped."""
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_candidate(
    details: dict,
    prices: pd.DataFrame,
    spy_prices: pd.DataFrame | None,
    cfg,
) -> tuple[float, dict]:
    """Score a candidate that already passed all gates. Returns (score, breakdown).

    `details` is the merged gate details dict (drawdown_pct, vol_adjusted_drop,
    stabilization_signals, roe, op_margin, debt_eq, warnings, ...).
    """
    breakdown: dict = {}

    # --- Dip depth (0-25): vol-adjusted drop is the primary measure; a stock
    # 2.5x below its own typical volatility is a rarer, more stretched entry
    # than one scraping past the 1.5x threshold. ATR distance is the fallback
    # when annualized vol is unavailable.
    vol_adj = details.get("vol_adjusted_drop")
    atr_dist = details.get("atr_distance")
    if vol_adj is not None:
        depth = _scale(vol_adj, cfg.VOL_DROP_THRESHOLD, cfg.VOL_DROP_THRESHOLD + 1.5) * 25.0
    elif atr_dist is not None:
        depth = _scale(atr_dist, cfg.K_ATR, cfg.K_ATR + 2.0) * 25.0
    else:
        depth = 0.0
    breakdown["depth"] = round(depth, 1)

    # --- Timing (0-30): the "best timing" component.
    timing = 0.0
    signals = details.get("stabilization_signals", []) or []
    timing += min(len(signals), 2) * 10.0  # each stabilization signal, capped at 2

    volume_confirmed = detect_volume_confirmation(
        prices,
        n=cfg.VOLUME_CONFIRM_DAYS,
        avg_window=cfg.VOLUME_CONFIRM_AVG_WINDOW,
        mult=cfg.VOLUME_CONFIRM_MULT,
    )
    if volume_confirmed:
        timing += 5.0
    breakdown["volume_confirmed"] = volume_confirmed

    dsl = days_since_low(prices)
    breakdown["days_since_low"] = dsl
    if dsl is not None and dsl > 0:  # 0 = today is the low: no bounce to buy yet
        if dsl <= cfg.FRESH_BOUNCE_MAX_DAYS:
            timing += 5.0  # fresh turn — the ideal entry window
        elif dsl <= cfg.FRESH_BOUNCE_MAX_DAYS * 2:
            timing += 2.0  # bounce is aging but still early
    breakdown["timing"] = round(min(timing, 30.0), 1)

    # --- Quality (0-25): better companies recover more reliably.
    quality = 0.0
    roe = details.get("roe")
    if isinstance(roe, (int, float)):
        quality += _scale(roe, cfg.MIN_ROE, 30.0) * 12.0
    op_margin = details.get("op_margin")
    if isinstance(op_margin, (int, float)):
        quality += _scale(op_margin, 0.0, 25.0) * 8.0
    debt_eq = details.get("debt_eq")
    if isinstance(debt_eq, (int, float)):
        quality += (1.0 - _scale(debt_eq, 0.0, cfg.MAX_DEBT_EQUITY)) * 5.0
    breakdown["quality"] = round(quality, 1)

    # --- Relative strength vs SPY (0-10): a dip stock is usually lagging the
    # market; the ones lagging least (or already outperforming) tend to have
    # found their buyers first.
    rs = compute_relative_strength(prices, spy_prices, lookback=cfg.RS_LOOKBACK)
    breakdown["relative_strength_pct"] = round(rs, 1) if rs is not None else None
    rs_score = _scale(rs, -10.0, 5.0) * 10.0 if rs is not None else 0.0
    breakdown["relative_strength"] = round(rs_score, 1)

    # --- Trap penalty: price-based traps are the reliable ones, hit harder.
    penalty = 0.0
    warnings = details.get("warnings", []) or []
    price_trap_markers = ("צניחה", "שפל", "מגמת")  # gap-down / fresh-low / downtrend
    for w in warnings:
        if any(marker in w for marker in price_trap_markers):
            penalty += 10.0
        else:
            penalty += 5.0
    penalty = min(penalty, 30.0)
    breakdown["trap_penalty"] = round(-penalty, 1)

    score = breakdown["depth"] + breakdown["timing"] + quality + rs_score - penalty
    score = max(0.0, min(100.0, score))
    breakdown["total"] = round(score, 1)
    return round(score, 1), breakdown


def rank_candidates(candidates: list[dict], cfg) -> tuple[list[dict], list[dict]]:
    """Sort scored candidates best-first, apply the sector cap and the daily cap.

    Each candidate dict must carry "score" and may carry "sector".
    Returns (selected, dropped). Selected entries get "rank" (1-based) and
    "ranked_of" (total number of scored candidates today) added in place.
    Candidates with no known sector are not counted against any sector cap.
    """
    ordered = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)

    selected: list[dict] = []
    dropped: list[dict] = []
    sector_counts: dict[str, int] = {}

    for cand in ordered:
        if len(selected) >= cfg.MAX_ALERTS_PER_DAY:
            dropped.append(cand)
            continue
        sector = cand.get("sector")
        if sector and sector_counts.get(sector, 0) >= cfg.MAX_PER_SECTOR:
            logger.info(
                "RANK: dropping %s (score %.1f) — sector cap reached for %s",
                cand.get("ticker"), cand.get("score", 0.0), sector,
            )
            dropped.append(cand)
            continue
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        selected.append(cand)

    for i, cand in enumerate(selected, start=1):
        cand["rank"] = i
        cand["ranked_of"] = len(ordered)

    return selected, dropped
