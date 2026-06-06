"""Four-gate pipeline: regime / quality / dip+stabilization / trap.

Each gate returns (passed: bool, details: dict).
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np

from src.indicators import (
    compute_rsi,
    compute_atr,
    compute_sma,
    compute_52w_high,
    compute_annualized_vol,
    compute_drawdown_from_52w_high,
    compute_vol_adjusted_drop,
    atr_distance_below_ma,
    detect_higher_low,
    detect_consecutive_up_closes,
)

logger = logging.getLogger(__name__)


def gate_0_regime(regime: str) -> tuple[bool, dict]:
    """Gate 0: Market regime. Always passes; returns regime for downstream use."""
    return True, {"regime": regime}


def gate_1_quality(fundamentals: dict, cfg) -> tuple[bool, dict]:
    """Gate 1: Quality — is this a company worth catching?

    Checks: ROE > MIN_ROE, operating margin > MIN_OP_MARGIN,
    debt/equity < MAX_DEBT_EQUITY, market cap > MIN_MKT_CAP.
    """
    reasons = []

    # Check for missing critical fields
    required_fields = {
        "returnOnEquity": "ROE",
        "operatingMargins": "operating margin",
        "marketCap": "market cap",
    }
    for field, label in required_fields.items():
        if field not in fundamentals or fundamentals[field] is None:
            reasons.append(f"Missing {label}")

    if reasons:
        return False, {"reason": f"Quality gate: {', '.join(reasons)}"}

    roe = fundamentals.get("returnOnEquity", 0) or 0
    op_margin = fundamentals.get("operatingMargins", 0) or 0
    debt_eq = fundamentals.get("debtToEquity") or float("inf")
    mkt_cap = fundamentals.get("marketCap", 0) or 0

    if roe * 100 < cfg.MIN_ROE:
        reasons.append(f"ROE {roe * 100:.1f}% < {cfg.MIN_ROE}%")

    if op_margin <= cfg.MIN_OP_MARGIN:
        reasons.append(f"Operating margin {op_margin * 100:.1f}% <= {cfg.MIN_OP_MARGIN * 100:.0f}%")

    if debt_eq > cfg.MAX_DEBT_EQUITY:
        reasons.append(f"Debt/equity {debt_eq:.0f} > {cfg.MAX_DEBT_EQUITY}")

    if mkt_cap < cfg.MIN_MKT_CAP:
        reasons.append(f"Market cap ${mkt_cap / 1e9:.1f}B < ${cfg.MIN_MKT_CAP / 1e9:.0f}B")

    if reasons:
        return False, {"reason": f"Quality gate: {'; '.join(reasons)}"}

    return True, {
        "roe": roe * 100,
        "op_margin": op_margin * 100,
        "debt_eq": debt_eq,
        "mkt_cap": mkt_cap,
    }


def gate_2_dip_and_stabilization(
    prices: pd.DataFrame, regime: str, cfg
) -> tuple[bool, dict]:
    """Gate 2: Hard dip (2a) + stabilization (2b).

    2a: drawdown >= MIN_DRAWDOWN, vol-adjusted drop, below 200dma
    2b: RSI turning up from oversold OR higher low OR consecutive up closes
    In RISK_OFF: require STABILIZATION_REQUIRED_RISK_OFF signals.
    """
    details = {}

    # --- Compute indicators ---
    drawdown = compute_drawdown_from_52w_high(prices)
    current_drawdown = drawdown.iloc[-1]
    details["drawdown_pct"] = round(current_drawdown, 1)

    rsi = compute_rsi(prices, period=14)
    current_rsi = rsi.iloc[-1]

    sma_200 = compute_sma(prices, 200)
    current_sma_200 = sma_200.iloc[-1] if len(sma_200.dropna()) > 0 else None
    current_close = prices["Close"].iloc[-1]

    vol_adj_drop = compute_vol_adjusted_drop(prices)
    current_vol_adj = vol_adj_drop.iloc[-1]

    atr_dist = atr_distance_below_ma(prices, ma_period=50)
    current_atr_dist = atr_dist.iloc[-1]

    details["rsi"] = round(current_rsi, 1)
    details["below_200dma"] = current_close < current_sma_200 if current_sma_200 else False
    details["vol_adjusted_drop"] = round(current_vol_adj, 2) if not np.isnan(current_vol_adj) else None
    details["atr_distance"] = round(current_atr_dist, 2) if not np.isnan(current_atr_dist) else None

    # --- 2a: Hard dip ---
    if current_drawdown > -cfg.MIN_DRAWDOWN:
        return False, {"reason": f"Dip gate: drawdown {current_drawdown:.1f}% > -{cfg.MIN_DRAWDOWN}%", **details}

    # Vol-adjusted drop: must exceed threshold OR be K ATRs below 50dma
    vol_ok = (not np.isnan(current_vol_adj) and current_vol_adj >= cfg.VOL_DROP_THRESHOLD)
    atr_ok = (not np.isnan(current_atr_dist) and current_atr_dist >= cfg.K_ATR)
    if not (vol_ok or atr_ok):
        return False, {"reason": f"Dip gate: insufficient vol-adjusted drop ({current_vol_adj:.2f}) or ATR distance ({current_atr_dist:.2f})", **details}

    if current_sma_200 is not None and current_close >= current_sma_200:
        return False, {"reason": "Dip gate: price not below 200-day MA", **details}

    # --- 2b: Stabilization ---
    signals = []

    # Signal 1: RSI was oversold within LOOKBACK and is turning up
    if len(rsi) >= cfg.LOOKBACK + 1:
        rsi_was_oversold = (rsi.iloc[-(cfg.LOOKBACK + 1):-1] < cfg.RSI_OVERSOLD).any()
        rsi_turning_up = current_rsi > rsi.iloc[-2] and current_rsi > rsi.iloc[-(cfg.LOOKBACK + 1)]
        if rsi_was_oversold and rsi_turning_up:
            signals.append("RSI turning up from oversold")

    # Signal 2: Higher low
    if detect_higher_low(prices):
        signals.append("higher low")

    # Signal 3: Consecutive up closes
    if detect_consecutive_up_closes(prices):
        signals.append("consecutive up closes")

    details["stabilization_signals"] = signals

    required = cfg.STABILIZATION_REQUIRED_RISK_OFF if regime == "RISK_OFF" else 1
    if len(signals) < required:
        return False, {"reason": f"Stabilization gate: {len(signals)}/{required} signals ({', '.join(signals) or 'none'})", **details}

    details["stabilization_count"] = len(signals)
    return True, details


def gate_3_trap(
    fundamentals: dict,
    prices: pd.DataFrame,
    cfg,
    trap_behavior: str = None,
) -> tuple[bool, dict]:
    """Gate 3: Trap vs. opportunity detection.

    Checks: fundamental soft flags, price-based traps, earnings blackout.
    trap_behavior: "warn" (pass with warning) or "suppress" (reject).
    """
    trap_behavior = trap_behavior or cfg.TRAP_BEHAVIOR
    warnings = []

    # --- Fundamental soft flags ---
    earnings_growth = fundamentals.get("earningsGrowth")
    revenue_growth = fundamentals.get("revenueGrowth")

    if earnings_growth is not None and earnings_growth < -0.1:
        warnings.append(f"ירידה ברווחים: {earnings_growth * 100:.0f}% — אות אזהרה")

    if revenue_growth is not None and revenue_growth < 0:
        warnings.append(f"ירידה בהכנסות: {revenue_growth * 100:.0f}% לעומת שנה שעברה — אות אזהרה")

    # --- Price-based trap detection (weighted higher) ---
    closes = prices["Close"].values
    volumes = prices["Volume"].values
    n = len(closes)

    # Fresh N-day low
    if n >= cfg.FRESH_LOW_DAYS:
        recent_low = closes[-1]
        period_low = closes[-cfg.FRESH_LOW_DAYS:].min()
        if recent_low <= period_low:
            warnings.append(f"שפל חדש של {cfg.FRESH_LOW_DAYS} ימים — המניה ממשיכה לרדת")

    # Steep downtrend: 50-day MA declining steeply
    if n >= 60:
        sma_50 = pd.Series(closes).rolling(50).mean()
        if not np.isnan(sma_50.iloc[-1]) and not np.isnan(sma_50.iloc[-11]):
            decline_pct = (sma_50.iloc[-1] - sma_50.iloc[-11]) / sma_50.iloc[-11] * 100
            if decline_pct < -cfg.STEEP_DOWNTREND_PCT:
                warnings.append(f"מגמת ירידה חדה: ממוצע 50 יום ירד {decline_pct:.1f}% ב-10 ימים")

    # Gap-down on high volume in last few days
    if n >= 5:
        avg_vol = volumes[:-3].mean() if n > 5 else volumes.mean()
        for i in range(-3, 0):
            if i - 1 >= -n:
                gap = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
                if gap < -cfg.GAP_DOWN_PCT and volumes[i] > avg_vol * cfg.GAP_VOLUME_MULT:
                    warnings.append(f"צניחה חדה של {gap:.1f}% עם נפח מסחר גבוה פי {volumes[i] / avg_vol:.1f}")

    # --- Earnings blackout ---
    earnings_date_str = fundamentals.get("nextEarningsDate")
    if earnings_date_str:
        try:
            earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_to_earnings = (earnings_date - datetime.now(timezone.utc)).days
            if 0 <= days_to_earnings <= cfg.EARNINGS_BLACKOUT_DAYS:
                warnings.append(f"דוח רבעוני עוד {days_to_earnings} ימים — סיכון לתנודתיות")
                if fundamentals.get("suppressOnEarnings") and trap_behavior == "suppress":
                    return False, {"warnings": warnings, "reason": "Earnings blackout suppressed"}
        except (ValueError, TypeError):
            pass

    # --- Decision ---
    price_traps = [w for w in warnings if any(k in w for k in ["צניחה", "שפל", "מגמת"])]

    if trap_behavior == "suppress" and len(price_traps) > 0:
        return False, {"warnings": warnings, "reason": "Price-based trap detected (suppressed)"}

    return True, {"warnings": warnings}
