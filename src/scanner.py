"""Scanner orchestrator — wires all modules and runs the daily scan.

Flow: regime -> universe -> batch prices -> price-based gates (2a, 2b) ->
      dedup -> fetch fundamentals for survivors -> quality gate -> trap gate ->
      score & rank (best first, sector-capped, top-N) -> telegram
"""
import logging
import os
import sys

import config
import src.universe as universe
import src.data as data
import src.regime as regime_mod
import src.gates as gates
import src.state as state
import src.telegram as telegram
from src.indicators import compute_rsi
from src.score import score_candidate, rank_candidates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def run_scan() -> list[str]:
    """Run the full dip scanner pipeline. Returns list of alerted tickers."""
    logger.info("=== Dip Scanner Run Starting ===")

    # Gate 0: Market regime (computed once)
    regime = regime_mod.compute_regime()
    logger.info("Market regime: %s", regime)

    if regime_mod.RISK_OFF == regime and config.SUPPRESS_IN_RISK_OFF:
        logger.info("SUPPRESS_IN_RISK_OFF is True — skipping scan in RISK_OFF")
        return []

    # Get universe
    tickers = universe.get_sp500_tickers()
    logger.info("Scanning %d tickers", len(tickers))

    # Fetch batch prices
    prices_map = data.fetch_prices(tickers)
    logger.info("Got prices for %d/%d tickers", len(prices_map), len(tickers))

    if not prices_map:
        logger.warning("No price data fetched, aborting scan")
        return []

    # SPY history for relative-strength scoring (best-effort; scoring
    # degrades gracefully without it)
    spy_df = data.fetch_prices(["SPY"]).get("SPY")

    # Load dedup state
    dedup_state = state.load_state()

    candidates: list[dict] = []
    deduped: list[str] = []

    # --- Phase 1: collect every stock that passes all gates ---
    for ticker, prices_df in prices_map.items():
        try:
            # Price-based gates first (cheap)
            passed_2, details_2 = gates.gate_2_dip_and_stabilization(
                prices_df, regime, config
            )
            if not passed_2:
                logger.debug("SKIP %s: %s", ticker, details_2.get("reason", "gate 2"))
                continue

            # Dedup check before the expensive fundamentals fetch
            if state.is_recently_alerted(ticker, dedup_state, config.DEDUP_DAYS):
                logger.info("SKIP %s: recently alerted (dedup)", ticker)
                deduped.append(ticker)
                continue

            # Fetch fundamentals only for survivors (expensive)
            fund = data.fetch_fundamentals(ticker)
            if fund is None:
                logger.debug("SKIP %s: no fundamental data", ticker)
                continue

            # Quality gate
            passed_1, details_1 = gates.gate_1_quality(fund, config)
            if not passed_1:
                logger.debug("SKIP %s: %s", ticker, details_1.get("reason", "quality gate"))
                continue

            # Trap gate
            passed_3, details_3 = gates.gate_3_trap(fund, prices_df, config)
            if not passed_3:
                logger.debug("SKIP %s: trap detected — %s", ticker, details_3.get("reason", ""))
                continue

            all_details = {**details_2, **details_1, **details_3, "regime": regime}

            # Add RSI trend description
            rsi_series = compute_rsi(prices_df)
            if len(rsi_series) >= config.LOOKBACK + 1:
                rsi_now = rsi_series.iloc[-1]
                rsi_then = rsi_series.iloc[-(config.LOOKBACK + 1)]
                all_details["rsi_trend"] = f"turning up (was {rsi_then:.0f} {config.LOOKBACK} days ago)" if rsi_now > rsi_then else "declining"

            score, breakdown = score_candidate(all_details, prices_df, spy_df, config)
            all_details["score"] = score
            all_details["score_breakdown"] = breakdown

            candidates.append({
                "ticker": ticker,
                "name": fund.get("shortName", ticker),
                "price": float(prices_df["Close"].iloc[-1]),
                "sector": fund.get("sector"),
                "score": score,
                "details": all_details,
            })
            logger.info("CANDIDATE: %s score=%.1f sector=%s", ticker, score, fund.get("sector"))

        except Exception as e:
            logger.error("Error processing %s: %s", ticker, e, exc_info=True)
            continue

    # --- Phase 2: rank best-first, cap per sector and per day, send ---
    selected, dropped = rank_candidates(candidates, config)
    for cand in dropped:
        # Not marked in the dedup store, so a strong-but-crowded-out candidate
        # can still alert tomorrow if it keeps qualifying.
        logger.info(
            "RANKED OUT: %s score=%.1f (top %d cap / sector cap)",
            cand["ticker"], cand["score"], config.MAX_ALERTS_PER_DAY,
        )

    alerted: list[str] = []
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = telegram.get_chat_ids()

    for cand in selected:
        details = cand["details"]
        details["rank"] = cand.get("rank")
        details["ranked_of"] = cand.get("ranked_of")
        msg = telegram.compose_alert(cand["ticker"], cand["name"], cand["price"], details)

        if token and chat_ids:
            for cid in chat_ids:
                telegram.send_alert(token, cid, msg)
        else:
            logger.warning("Telegram credentials not set or no users registered, printing alert instead")
            print(msg)

        dedup_state = state.mark_alerted(cand["ticker"], dedup_state)
        alerted.append(cand["ticker"])
        logger.info("ALERT: %s (%s) score=%.1f rank=%s", cand["ticker"], cand["name"],
                    cand["score"], cand.get("rank"))

    # Save dedup state
    state.save_state(dedup_state)

    # Send daily summary when no new alerts fired so the user always hears something
    if not alerted:
        if token and chat_ids:
            summary = telegram.compose_daily_summary(
                regime, len(prices_map), len(alerted), deduped
            )
            for cid in chat_ids:
                telegram.send_alert(token, cid, summary)
            logger.info("Daily summary sent to %d recipients", len(chat_ids))

    logger.info("=== Scan Complete: %d alerts sent ===", len(alerted))
    return alerted


if __name__ == "__main__":
    run_scan()
