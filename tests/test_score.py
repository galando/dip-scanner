"""Tests for score.py — composite opportunity scoring and ranking."""
import pandas as pd
import pytest

import config
from src.score import score_candidate, rank_candidates, _scale


def _make_price_df(closes, volumes=None, start="2024-01-01"):
    dates = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=dates, dtype=float)
    vol = pd.Series(volumes if volumes else [1_000_000] * len(closes), index=dates, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": vol,
    })


def _bounce_prices():
    """Fell hard, bounced 3 days ago — a fresh-turn entry."""
    closes = [100.0] * 100 + [100.0 - i * 0.9 for i in range(40)] + [65.0, 66.0, 67.0]
    return _make_price_df(closes)


def _base_details(**overrides):
    d = {
        "drawdown_pct": -30.0,
        "vol_adjusted_drop": 2.0,
        "atr_distance": 2.5,
        "stabilization_signals": ["higher low"],
        "roe": 20.0,
        "op_margin": 15.0,
        "debt_eq": 80.0,
        "warnings": [],
    }
    d.update(overrides)
    return d


class TestScale:
    def test_clamps_and_maps(self):
        assert _scale(-1, 0, 10) == 0.0
        assert _scale(5, 0, 10) == 0.5
        assert _scale(99, 0, 10) == 1.0
        assert _scale(1, 5, 5) == 0.0  # degenerate range


class TestScoreCandidate:
    def test_score_in_range_with_breakdown(self):
        score, breakdown = score_candidate(_base_details(), _bounce_prices(), None, config)
        assert 0.0 <= score <= 100.0
        for key in ("depth", "timing", "quality", "relative_strength", "trap_penalty", "total"):
            assert key in breakdown

    def test_deeper_vol_adjusted_drop_scores_higher(self):
        prices = _bounce_prices()
        s_shallow, _ = score_candidate(_base_details(vol_adjusted_drop=1.5), prices, None, config)
        s_deep, _ = score_candidate(_base_details(vol_adjusted_drop=3.0), prices, None, config)
        assert s_deep > s_shallow

    def test_more_stabilization_signals_score_higher(self):
        prices = _bounce_prices()
        s_one, _ = score_candidate(
            _base_details(stabilization_signals=["higher low"]), prices, None, config)
        s_two, _ = score_candidate(
            _base_details(stabilization_signals=["higher low", "consecutive up closes"]),
            prices, None, config)
        assert s_two > s_one

    def test_price_trap_warning_penalized_harder_than_soft_flag(self):
        prices = _bounce_prices()
        s_clean, _ = score_candidate(_base_details(), prices, None, config)
        s_soft, _ = score_candidate(
            _base_details(warnings=["Revenue down 4% YoY"]), prices, None, config)
        s_price_trap, _ = score_candidate(
            _base_details(warnings=["שפל חדש של 20 ימים / Fresh 20-day low"]),
            prices, None, config)
        assert s_clean > s_soft > s_price_trap

    def test_better_quality_scores_higher(self):
        prices = _bounce_prices()
        s_ok, _ = score_candidate(
            _base_details(roe=13.0, op_margin=2.0, debt_eq=140.0), prices, None, config)
        s_great, _ = score_candidate(
            _base_details(roe=35.0, op_margin=30.0, debt_eq=20.0), prices, None, config)
        assert s_great > s_ok

    def test_relative_strength_rewards_outperformer(self):
        prices = _bounce_prices()
        n = len(prices)
        spy_falling = _make_price_df([500.0 - i * 0.5 for i in range(n)])
        spy_rising = _make_price_df([400.0 + i * 0.5 for i in range(n)])
        s_vs_falling, b1 = score_candidate(_base_details(), prices, spy_falling, config)
        s_vs_rising, b2 = score_candidate(_base_details(), prices, spy_rising, config)
        # Same stock looks relatively stronger against a falling market
        assert b1["relative_strength"] >= b2["relative_strength"]
        assert s_vs_falling >= s_vs_rising

    def test_fresh_bounce_beats_no_bounce(self):
        details = _base_details()
        fresh = _bounce_prices()  # low was 3 sessions ago
        knife = _make_price_df([100.0] * 100 + [100.0 - i * 0.9 for i in range(43)])  # today IS the low
        s_fresh, b_fresh = score_candidate(details, fresh, None, config)
        s_knife, b_knife = score_candidate(details, knife, None, config)
        assert b_fresh["days_since_low"] <= config.FRESH_BOUNCE_MAX_DAYS
        assert b_knife["days_since_low"] == 0
        assert s_fresh > s_knife

    def test_missing_fields_never_crash(self):
        score, _ = score_candidate({}, _make_price_df([100.0] * 5), None, config)
        assert 0.0 <= score <= 100.0


class TestRankCandidates:
    def _cand(self, ticker, score, sector=None):
        return {"ticker": ticker, "score": score, "sector": sector}

    def test_sorted_best_first_with_rank_fields(self):
        cands = [self._cand("A", 40), self._cand("B", 90), self._cand("C", 70)]
        selected, dropped = rank_candidates(cands, config)
        assert [c["ticker"] for c in selected] == ["B", "C", "A"]
        assert selected[0]["rank"] == 1
        assert selected[0]["ranked_of"] == 3
        assert dropped == []

    def test_daily_cap_enforced(self):
        cands = [self._cand(f"T{i}", 90 - i) for i in range(config.MAX_ALERTS_PER_DAY + 3)]
        selected, dropped = rank_candidates(cands, config)
        assert len(selected) == config.MAX_ALERTS_PER_DAY
        assert len(dropped) == 3
        # The best ones survived
        assert selected[0]["ticker"] == "T0"

    def test_sector_cap_enforced(self):
        cands = [
            self._cand("CHIP1", 95, "Technology"),
            self._cand("CHIP2", 90, "Technology"),
            self._cand("CHIP3", 85, "Technology"),
            self._cand("BANK1", 60, "Financials"),
        ]
        selected, dropped = rank_candidates(cands, config)
        tech = [c for c in selected if c["sector"] == "Technology"]
        assert len(tech) == config.MAX_PER_SECTOR
        # The lower-scored bank still gets through; the third chip is dropped
        assert any(c["ticker"] == "BANK1" for c in selected)
        assert any(c["ticker"] == "CHIP3" for c in dropped)

    def test_unknown_sector_not_capped(self):
        cands = [self._cand(f"U{i}", 80 - i, None) for i in range(4)]
        selected, _ = rank_candidates(cands, config)
        assert len(selected) == 4
