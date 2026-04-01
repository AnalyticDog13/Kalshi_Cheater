"""
Tests for src/edge_calculator.py.

Covers:
  - _taker_fee_per_contract: formula correctness, ceiling, edge cases
  - EdgeParams.from_settings: config loading
  - compute_edge: all NO_TRADE guards, three-scenario fee breakdown, Kelly sizing,
    direction (BUY_YES vs BUY_NO), SANITY_CHECK threshold, recommendation strings
"""

from __future__ import annotations

import math
import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from src.edge_calculator import (
    EdgeAssessment,
    EdgeParams,
    _taker_fee_per_contract,
    compute_edge,
)
from src.data_feeds.weather_ensemble import EnsembleResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ensemble(probability: float, confidence: str = "high", members: int = 139) -> EnsembleResult:
    """Build a minimal EnsembleResult for testing."""
    spread_map = {"high": 4.0, "medium": 8.0, "low": 15.0}
    spread = spread_map.get(confidence, 4.0)
    exceeding = int(probability * members)
    return EnsembleResult(
        market_ticker="TEST-TICKER",
        target_date=date(2026, 4, 1),
        threshold_f=75.0,
        station_lat=40.7789,
        station_lon=-73.9692,
        probability=probability,
        total_members=members,
        members_exceeding=exceeding,
        ensemble_mean=74.0,
        ensemble_spread=spread,
        ensemble_std=2.0,
        confidence_interval_90=(70.0, 80.0),
        models_used=["gfs_seamless"],
        member_keys_found=members,
        freshness_minutes=0.0,
        bias_correction_applied=0.0,
        fetched_at=datetime.now(timezone.utc),
    )


DEFAULT_PARAMS = EdgeParams(
    min_edge_high_confidence=0.04,
    min_edge_medium_confidence=0.06,
    min_edge_low_confidence=0.10,
    assumed_slippage=0.01,
    fee_multiple_required=3.0,
    kelly_fraction=0.25,
    extreme_price_lower_bound=0.02,
    extreme_price_upper_bound=0.98,
)

FEE_COEFF = 0.07


# ===========================================================================
# _taker_fee_per_contract
# ===========================================================================

class TestTakerFeePerContract:

    def test_midpoint_price(self):
        # At P=0.50: ceil(0.07 * 1 * 0.5 * 0.5 * 100) / 100 = ceil(1.75) / 100 = 0.02
        fee = _taker_fee_per_contract(0.07, 0.50)
        assert fee == 0.02

    def test_ceiling_applied(self):
        # At P=0.40: 0.07 * 0.40 * 0.60 * 100 = 1.68 → ceil = 2 → 0.02
        fee = _taker_fee_per_contract(0.07, 0.40)
        assert fee == 0.02

    def test_low_price_smaller_fee(self):
        # At P=0.10: 0.07 * 0.10 * 0.90 * 100 = 0.63 → ceil = 1 → 0.01
        fee = _taker_fee_per_contract(0.07, 0.10)
        assert fee == 0.01

    def test_very_low_price_minimum_fee(self):
        # At P=0.03: 0.07 * 0.03 * 0.97 * 100 = 0.2037 → ceil = 1 → 0.01
        fee = _taker_fee_per_contract(0.07, 0.03)
        assert fee == 0.01

    def test_extreme_low_price_returns_zero(self):
        # At P=0: fee should be 0 (no trade at boundary)
        assert _taker_fee_per_contract(0.07, 0.0) == 0.0

    def test_extreme_high_price_returns_zero(self):
        assert _taker_fee_per_contract(0.07, 1.0) == 0.0

    def test_symmetric_around_midpoint(self):
        # Fee is symmetric: P(0.30) == P(0.70) because 0.3*0.7 == 0.7*0.3
        fee_30 = _taker_fee_per_contract(0.07, 0.30)
        fee_70 = _taker_fee_per_contract(0.07, 0.70)
        assert fee_30 == fee_70

    def test_different_coefficient(self):
        # Coefficient of 0.05 instead of 0.07
        fee = _taker_fee_per_contract(0.05, 0.50)
        # 0.05 * 0.5 * 0.5 * 100 = 1.25 → ceil = 2 → 0.02
        assert fee == 0.02

    def test_coefficient_zero_means_no_fee(self):
        assert _taker_fee_per_contract(0.0, 0.50) == 0.0


# ===========================================================================
# EdgeParams
# ===========================================================================

class TestEdgeParams:

    def test_from_settings_loads_values(self):
        settings = {
            "edge": {
                "min_edge_high_confidence": 0.03,
                "min_edge_medium_confidence": 0.05,
                "min_edge_low_confidence": 0.09,
                "assumed_slippage": 0.005,
                "fee_multiple_required": 2.0,
                "kelly_fraction": 0.20,
                "extreme_price_lower_bound": 0.03,
                "extreme_price_upper_bound": 0.97,
            }
        }
        params = EdgeParams.from_settings(settings)
        assert params.min_edge_high_confidence == 0.03
        assert params.kelly_fraction == 0.20
        assert params.fee_multiple_required == 2.0

    def test_from_settings_uses_defaults_when_missing(self):
        params = EdgeParams.from_settings({})
        assert params.min_edge_high_confidence == 0.04
        assert params.kelly_fraction == 0.25

    def test_from_settings_partial_override(self):
        settings = {"edge": {"kelly_fraction": 0.15}}
        params = EdgeParams.from_settings(settings)
        assert params.kelly_fraction == 0.15
        assert params.assumed_slippage == 0.01  # default preserved


# ===========================================================================
# compute_edge — NO_TRADE guards
# ===========================================================================

class TestComputeEdgeNoTrade:

    def test_no_edge_at_all(self):
        # Ensemble prob == market price → no edge
        result = compute_edge(
            "TEST", 0.65, make_ensemble(0.65), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"
        assert result.recommended_contracts == 0
        assert result.skip_reason is not None

    def test_edge_killed_by_taker_fee(self):
        # Edge of 0.01, fee at P=0.50 is 0.02 — edge doesn't survive taker fee
        result = compute_edge(
            "TEST", 0.50, make_ensemble(0.51), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"
        assert "taker fee" in result.skip_reason.lower() or "survive" in result.skip_reason.lower()

    def test_edge_below_minimum_after_slippage(self):
        # Edge = 0.04 (exactly at high-confidence threshold), slippage=0.01
        # → effective edge = 0.03 < 0.04 → NO_TRADE
        result = compute_edge(
            "TEST", 0.60, make_ensemble(0.64, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"

    def test_extreme_price_buy_yes_too_low(self):
        # Entry price = 0.01 (below lower bound of 0.02)
        result = compute_edge(
            "TEST", 0.01, make_ensemble(0.05, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"
        assert "Kelly range" in result.skip_reason

    def test_extreme_price_buy_no_too_low(self):
        # BUY_NO at market_price=0.99 → entry_price = 0.01 (too low)
        result = compute_edge(
            "TEST", 0.99, make_ensemble(0.90, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"

    def test_extreme_price_buy_yes_too_high(self):
        # Entry price = 0.99 (above upper bound of 0.98)
        result = compute_edge(
            "TEST", 0.99, make_ensemble(1.00, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"

    def test_low_confidence_requires_higher_edge(self):
        # Edge = 0.07 — passes medium threshold (0.06) but not low threshold (0.10)
        ensemble = make_ensemble(0.57, confidence="low")
        result = compute_edge(
            "TEST", 0.50, ensemble, FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"

    def test_medium_confidence_below_threshold(self):
        # Edge = 0.05, medium threshold = 0.06 → NO_TRADE after slippage (0.05-0.01=0.04 < 0.06)
        ensemble = make_ensemble(0.55, confidence="medium")
        result = compute_edge(
            "TEST", 0.50, ensemble, FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "NO_TRADE"


# ===========================================================================
# compute_edge — TRADE cases
# ===========================================================================

class TestComputeEdgeTrade:

    def test_basic_buy_yes_trade(self):
        # Ensemble says 70%, market says 60% → BUY_YES, edge=10pp (below sanity threshold)
        result = compute_edge(
            "KXHIGHNY-25APR1-B75", 0.60, make_ensemble(0.70, confidence="high"),
            FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "TRADE"
        assert result.direction == "BUY_YES"
        assert result.entry_price == pytest.approx(0.60)
        assert result.raw_edge == pytest.approx(0.10)
        assert result.recommended_contracts > 0
        assert result.recommended_dollars > 0

    def test_basic_buy_no_trade(self):
        # Ensemble says 40%, market says 50% → BUY_NO, edge=10pp (pay 0.50 per NO contract)
        result = compute_edge(
            "TEST", 0.50, make_ensemble(0.40, confidence="high"),
            FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.recommendation == "TRADE"
        assert result.direction == "BUY_NO"
        assert result.entry_price == pytest.approx(0.50)  # 1.0 - 0.50

    def test_direction_buy_yes_when_ensemble_higher(self):
        result = compute_edge("T", 0.40, make_ensemble(0.70), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.direction == "BUY_YES"
        assert result.entry_price == pytest.approx(0.40)

    def test_direction_buy_no_when_ensemble_lower(self):
        result = compute_edge("T", 0.70, make_ensemble(0.40), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.direction == "BUY_NO"
        assert result.entry_price == pytest.approx(0.30)  # 1.0 - 0.70

    def test_raw_edge_computed_correctly(self):
        result = compute_edge("T", 0.55, make_ensemble(0.65), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.raw_edge == pytest.approx(0.10)

    def test_taker_fee_populated(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        # At entry_price=0.60: 0.07 * 0.60 * 0.40 * 100 = 1.68 → ceil=2 → 0.02
        assert result.taker_fee == pytest.approx(0.02)

    def test_maker_fee_always_zero(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.maker_fee == 0.0

    def test_edge_after_taker_is_raw_minus_fee(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_taker == pytest.approx(result.raw_edge - result.taker_fee)

    def test_edge_after_maker_equals_raw_edge(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_maker == pytest.approx(result.raw_edge)

    def test_edge_after_round_trip_is_raw_minus_two_fees(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_round_trip == pytest.approx(
            result.raw_edge - result.taker_fee - result.early_exit_taker_fee
        )

    def test_position_capped_at_max_position_dollars(self):
        # Huge portfolio, but max_position_dollars=50
        result = compute_edge("T", 0.50, make_ensemble(0.80), FEE_COEFF, 100_000.0, 50.0, DEFAULT_PARAMS)
        assert result.recommended_dollars <= 50.0 + 0.01  # tiny rounding tolerance

    def test_position_scales_with_portfolio(self):
        # Larger portfolio → more contracts (Kelly scales with portfolio)
        small = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 200.0, 50.0, DEFAULT_PARAMS)
        large = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 800.0, 50.0, DEFAULT_PARAMS)
        # Both capped at max_position_dollars=50, but large should at least equal small
        assert large.recommended_dollars >= small.recommended_dollars

    def test_contracts_at_least_one(self):
        # Even tiny portfolios produce at least 1 contract
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 10.0, 50.0, DEFAULT_PARAMS)
        assert result.recommended_contracts >= 1

    def test_market_ticker_propagated(self):
        result = compute_edge(
            "KXHIGHNY-MYTHING", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS
        )
        assert result.market_ticker == "KXHIGHNY-MYTHING"

    def test_fair_value_propagated(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.fair_value == pytest.approx(0.75)

    def test_market_price_propagated(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.market_price == pytest.approx(0.60)

    def test_recommended_dollars_equals_contracts_times_entry_price(self):
        result = compute_edge("T", 0.60, make_ensemble(0.80), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        expected = result.recommended_contracts * result.entry_price
        assert result.recommended_dollars == pytest.approx(expected)

    def test_medium_confidence_passes_with_sufficient_edge(self):
        # Edge = 0.10, medium threshold = 0.06, slippage 0.01 → effective 0.09 > 0.06
        result = compute_edge("T", 0.50, make_ensemble(0.60, confidence="medium"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation in ("TRADE", "SANITY_CHECK")

    def test_low_confidence_passes_with_large_edge(self):
        # Edge = 0.15, low threshold = 0.10, slippage 0.01 → effective 0.14 > 0.10
        result = compute_edge("T", 0.50, make_ensemble(0.65, confidence="low"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation in ("TRADE", "SANITY_CHECK")


# ===========================================================================
# compute_edge — SANITY_CHECK threshold
# ===========================================================================

class TestComputeEdgeSanityCheck:

    def test_large_edge_triggers_sanity_check(self):
        # Edge = 0.25, well above the ~0.15 threshold
        result = compute_edge("T", 0.50, make_ensemble(0.75, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation == "SANITY_CHECK"
        assert result.recommended_contracts > 0  # still sized, just flagged

    def test_moderate_edge_does_not_trigger_sanity(self):
        # Edge = 0.08, below sanity threshold
        result = compute_edge("T", 0.60, make_ensemble(0.68, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation == "TRADE"

    def test_sanity_check_skip_reason_is_none(self):
        # SANITY_CHECK should not have a skip_reason (it's not a hard rejection)
        result = compute_edge("T", 0.50, make_ensemble(0.75, confidence="high"), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation == "SANITY_CHECK"
        assert result.skip_reason is None


# ===========================================================================
# compute_edge — three-scenario fee math
# ===========================================================================

class TestThreeScenarioFees:

    def test_scenario_1_best_case_is_zero_fee(self):
        """Maker entry + hold: edge_after_maker == raw_edge."""
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_maker == pytest.approx(result.raw_edge)

    def test_scenario_2_likely_case_subtracts_one_fee(self):
        """Taker entry + hold: edge_after_taker == raw_edge - taker_fee."""
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_taker == pytest.approx(result.raw_edge - result.taker_fee)

    def test_scenario_3_worst_case_subtracts_two_fees(self):
        """Taker entry + taker exit: edge_after_round_trip == raw_edge - 2x fee."""
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.edge_after_round_trip == pytest.approx(
            result.raw_edge - result.taker_fee - result.early_exit_taker_fee
        )

    def test_early_exit_fee_equals_taker_fee_at_entry(self):
        """Early exit fee uses same price as entry (conservative approximation)."""
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.early_exit_taker_fee == pytest.approx(result.taker_fee)


# ===========================================================================
# compute_edge — Kelly sizing math
# ===========================================================================

class TestKellySizing:

    def test_kelly_fraction_stored(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        # Quarter-Kelly: kelly_fraction = 0.25 * (edge_after_maker / (1 - entry_price))
        expected = 0.25 * (result.edge_after_maker / (1.0 - result.entry_price))
        assert result.kelly_fraction == pytest.approx(expected)

    def test_position_does_not_exceed_max(self):
        for balance in [100.0, 500.0, 5000.0]:
            result = compute_edge("T", 0.60, make_ensemble(0.80), FEE_COEFF, balance, 50.0, DEFAULT_PARAMS)
            assert result.recommended_dollars <= 50.0 + 0.01

    def test_zero_kelly_fraction_param(self):
        params = EdgeParams(
            min_edge_high_confidence=0.04,
            min_edge_medium_confidence=0.06,
            min_edge_low_confidence=0.10,
            assumed_slippage=0.01,
            fee_multiple_required=3.0,
            kelly_fraction=0.0,   # ← zero
            extreme_price_lower_bound=0.02,
            extreme_price_upper_bound=0.98,
        )
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, params)
        # Even with 0 kelly_fraction, we still produce 1 contract minimum
        assert result.recommended_contracts >= 1


# ===========================================================================
# compute_edge — edge cases / boundary conditions
# ===========================================================================

class TestEdgeCasesAndBoundaries:

    def test_no_trade_has_zero_contracts(self):
        result = compute_edge("T", 0.50, make_ensemble(0.50), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.recommendation == "NO_TRADE"
        assert result.recommended_contracts == 0
        assert result.recommended_dollars == 0.0
        assert result.kelly_fraction == 0.0

    def test_default_params_used_when_none(self):
        # Should not raise; defaults kick in
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, None)
        assert result.recommendation in ("TRADE", "SANITY_CHECK", "NO_TRADE")

    def test_str_representation(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        s = str(result)
        assert "T" in s
        assert "BUY_YES" in s

    def test_fee_coefficient_zero_reduces_fee_threshold(self):
        # With zero-fee series, the fee floor collapses — only confidence floor applies
        result = compute_edge("T", 0.60, make_ensemble(0.75), 0.0, 500.0, 50.0, DEFAULT_PARAMS)
        # Edge of 15% should comfortably pass even high-confidence threshold
        assert result.recommendation in ("TRADE", "SANITY_CHECK")

    def test_buy_no_entry_price_is_complement(self):
        result = compute_edge("T", 0.75, make_ensemble(0.45), FEE_COEFF, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.direction == "BUY_NO"
        assert result.entry_price == pytest.approx(0.25)  # 1 - 0.75

    def test_fee_coefficient_propagated(self):
        result = compute_edge("T", 0.60, make_ensemble(0.75), 0.05, 500.0, 50.0, DEFAULT_PARAMS)
        assert result.fee_coefficient == 0.05
