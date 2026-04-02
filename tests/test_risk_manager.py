"""
Tests for src/risk_manager.py.

Covers:
  - RiskLimits.from_settings: config loading
  - RiskManager state: exposure tracking, daily P&L, reset
  - check_trade: all 11 checks individually, compound failures,
    full approval path, open-orders cap
  - is_daily_loss_limit_breached
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from src.risk_manager import RiskLimits, RiskManager, RiskCheckOutcome
from src.edge_calculator import EdgeAssessment, EdgeParams
from src.data_feeds.weather_ensemble import EnsembleResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_LIMITS = RiskLimits(
    max_position_dollars=50.0,
    max_total_exposure_dollars=250.0,
    max_daily_loss_dollars=30.0,
    max_open_orders=10,
    max_contract_price_dollars=0.50,
    min_volume_fp=50.0,
    max_spread_dollars=0.08,
    min_resolution_hours=6.0,
    max_resolution_days=7.0,
    min_ensemble_members=30,
)


def make_edge(
    market_ticker: str = "TEST",
    recommendation: str = "TRADE",
    direction: str = "BUY_YES",
    raw_edge: float = 0.10,
    entry_price: float = 0.40,
    recommended_contracts: int = 10,
    recommended_dollars: float = 4.0,
    edge_after_taker: float = 0.08,
    skip_reason: str | None = None,
) -> EdgeAssessment:
    return EdgeAssessment(
        market_ticker=market_ticker,
        market_price=entry_price,
        fair_value=entry_price + raw_edge,
        direction=direction,
        entry_price=entry_price,
        fee_coefficient=0.07,
        raw_edge=raw_edge,
        taker_fee=0.02,
        maker_fee=0.0,
        early_exit_taker_fee=0.02,
        edge_after_maker=raw_edge,
        edge_after_taker=edge_after_taker,
        edge_after_round_trip=raw_edge - 0.04,
        kelly_fraction=0.05,
        recommended_contracts=recommended_contracts,
        recommended_dollars=recommended_dollars,
        recommendation=recommendation,
        skip_reason=skip_reason,
    )


def make_ensemble(
    confidence: str = "high",
    total_members: int = 139,
    probability: float = 0.70,
    spread: float = 4.0,
) -> EnsembleResult:
    spread_map = {"high": 4.0, "medium": 8.0, "low": 15.0}
    return EnsembleResult(
        market_ticker="TEST",
        target_date=date(2026, 4, 1),
        threshold_f=75.0,
        station_lat=40.77,
        station_lon=-73.96,
        probability=probability,
        total_members=total_members,
        members_exceeding=int(probability * total_members),
        ensemble_mean=74.0,
        ensemble_spread=spread_map.get(confidence, spread),
        ensemble_std=2.0,
        confidence_interval_90=(70.0, 80.0),
        models_used=["gfs_seamless"],
        member_keys_found=total_members,
        freshness_minutes=0.0,
        bias_correction_applied=0.0,
        fetched_at=datetime.now(timezone.utc),
    )


def make_rm() -> RiskManager:
    return RiskManager(DEFAULT_LIMITS)


def approved_call(rm: RiskManager, **overrides) -> RiskCheckOutcome:
    """Run check_trade with all-passing defaults, overriding specific args."""
    defaults = dict(
        edge=make_edge(),
        ensemble=make_ensemble(),
        market_status="open",
        volume_fp=100.0,
        spread_dollars=0.04,
        hours_to_resolution=24.0,
        open_orders_count=0,
    )
    defaults.update(overrides)
    return rm.check_trade(**defaults)


# ===========================================================================
# RiskLimits.from_settings
# ===========================================================================

class TestRiskLimitsFromSettings:

    def test_loads_risk_section(self):
        settings = {
            "risk": {
                "max_position_dollars": 75.0,
                "max_total_exposure_dollars": 300.0,
                "max_daily_loss_dollars": 40.0,
                "max_open_orders": 5,
                "max_contract_price_dollars": 0.30,
            }
        }
        limits = RiskLimits.from_settings(settings)
        assert limits.max_position_dollars == 75.0
        assert limits.max_total_exposure_dollars == 300.0
        assert limits.max_daily_loss_dollars == 40.0
        assert limits.max_open_orders == 5
        assert limits.max_contract_price_dollars == 0.30

    def test_loads_scanner_section(self):
        settings = {
            "scanner": {
                "min_volume_fp": 75.0,
                "max_spread_dollars": 0.06,
                "min_resolution_hours": 8.0,
                "max_resolution_days": 5.0,
            }
        }
        limits = RiskLimits.from_settings(settings)
        assert limits.min_volume_fp == 75.0
        assert limits.max_spread_dollars == 0.06

    def test_loads_ensemble_section(self):
        settings = {"ensemble": {"min_members_for_trade": 50}}
        limits = RiskLimits.from_settings(settings)
        assert limits.min_ensemble_members == 50

    def test_defaults_when_empty(self):
        limits = RiskLimits.from_settings({})
        assert limits.max_position_dollars == 1.0
        assert limits.max_total_exposure_dollars == 10.0
        assert limits.max_contract_price_dollars == 0.50
        assert limits.min_ensemble_members == 30


# ===========================================================================
# RiskManager state management
# ===========================================================================

class TestRiskManagerState:

    def test_initial_state_is_zero(self):
        rm = make_rm()
        assert rm.total_exposure == 0.0
        assert rm.daily_pnl == 0.0

    def test_record_position_opened_increases_exposure(self):
        rm = make_rm()
        rm.record_position_opened(25.0)
        assert rm.total_exposure == pytest.approx(25.0)

    def test_record_position_opened_accumulates(self):
        rm = make_rm()
        rm.record_position_opened(10.0)
        rm.record_position_opened(15.0)
        assert rm.total_exposure == pytest.approx(25.0)

    def test_record_position_closed_reduces_exposure(self):
        rm = make_rm()
        rm.record_position_opened(30.0)
        rm.record_position_closed(30.0, pnl=5.0)
        assert rm.total_exposure == pytest.approx(0.0)

    def test_record_position_closed_cannot_go_negative(self):
        rm = make_rm()
        rm.record_position_closed(100.0, pnl=-5.0)  # no prior opening
        assert rm.total_exposure == 0.0

    def test_record_position_closed_tracks_pnl(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-8.0)
        assert rm.daily_pnl == pytest.approx(-8.0)

    def test_multiple_pnl_accumulates(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=5.0)
        rm.record_position_closed(10.0, pnl=-3.0)
        assert rm.daily_pnl == pytest.approx(2.0)

    def test_reset_daily_state_clears_pnl(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-20.0)
        rm.reset_daily_state()
        assert rm.daily_pnl == pytest.approx(0.0)

    def test_reset_daily_state_does_not_clear_exposure(self):
        """Exposure persists across days — open positions don't evaporate."""
        rm = make_rm()
        rm.record_position_opened(40.0)
        rm.reset_daily_state()
        assert rm.total_exposure == pytest.approx(40.0)


# ===========================================================================
# is_daily_loss_limit_breached
# ===========================================================================

class TestDailyLossLimit:

    def test_not_breached_initially(self):
        rm = make_rm()
        assert not rm.is_daily_loss_limit_breached()

    def test_breached_at_exact_limit(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-30.0)  # exactly -$30
        assert rm.is_daily_loss_limit_breached()

    def test_not_breached_just_before_limit(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-29.99)
        assert not rm.is_daily_loss_limit_breached()

    def test_breached_exceeds_limit(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-50.0)
        assert rm.is_daily_loss_limit_breached()


# ===========================================================================
# check_trade — full approval path
# ===========================================================================

class TestCheckTradeApproval:

    def test_all_passing_inputs_approved(self):
        rm = make_rm()
        outcome = approved_call(rm)
        assert outcome.approved is True
        assert len(outcome.failed_checks) == 0

    def test_approved_outcome_has_notes(self):
        rm = make_rm()
        outcome = approved_call(rm)
        assert len(outcome.notes) > 0

    def test_str_approved(self):
        rm = make_rm()
        outcome = approved_call(rm)
        assert "APPROVED" in str(outcome)

    def test_str_rejected_mentions_failing_check(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-30.0)  # breach daily limit
        outcome = approved_call(rm)
        assert "REJECTED" in str(outcome)
        assert "Check 3" in str(outcome)


# ===========================================================================
# check_trade — individual checks
# ===========================================================================

class TestCheck1PositionSize:

    def test_position_above_limit_fails(self):
        rm = make_rm()
        big_edge = make_edge(recommended_dollars=55.0)
        outcome = approved_call(rm, edge=big_edge)
        assert not outcome.approved
        assert any("Check 1" in f for f in outcome.failed_checks)

    def test_position_exactly_at_limit_passes(self):
        rm = make_rm()
        edge = make_edge(recommended_dollars=50.0)
        outcome = approved_call(rm, edge=edge)
        assert not any("Check 1" in f for f in outcome.failed_checks)

    def test_position_below_limit_passes(self):
        rm = make_rm()
        outcome = approved_call(rm)  # $6 position
        assert not any("Check 1" in f for f in outcome.failed_checks)


class TestCheck2TotalExposure:

    def test_would_exceed_total_exposure_fails(self):
        rm = make_rm()
        rm.record_position_opened(245.0)  # $245 already open
        edge = make_edge(recommended_dollars=10.0)  # would hit $255 > $250
        outcome = approved_call(rm, edge=edge)
        assert not outcome.approved
        assert any("Check 2" in f for f in outcome.failed_checks)

    def test_exactly_at_limit_passes(self):
        rm = make_rm()
        rm.record_position_opened(200.0)
        edge = make_edge(recommended_dollars=50.0)  # 200 + 50 = 250 exactly
        outcome = approved_call(rm, edge=edge)
        assert not any("Check 2" in f for f in outcome.failed_checks)

    def test_no_existing_exposure_passes(self):
        rm = make_rm()
        outcome = approved_call(rm)
        assert not any("Check 2" in f for f in outcome.failed_checks)


class TestCheck3DailyLoss:

    def test_daily_loss_at_limit_fails(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-30.0)
        outcome = approved_call(rm)
        assert not outcome.approved
        assert any("Check 3" in f for f in outcome.failed_checks)

    def test_daily_loss_just_below_limit_passes(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-29.99)
        outcome = approved_call(rm)
        assert not any("Check 3" in f for f in outcome.failed_checks)

    def test_positive_pnl_passes(self):
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=15.0)
        outcome = approved_call(rm)
        assert not any("Check 3" in f for f in outcome.failed_checks)


class TestCheck4EdgeCalculatorDecision:

    def test_no_trade_edge_fails(self):
        rm = make_rm()
        bad_edge = make_edge(recommendation="NO_TRADE", skip_reason="insufficient edge")
        outcome = approved_call(rm, edge=bad_edge)
        assert not outcome.approved
        assert any("Check 4" in f for f in outcome.failed_checks)

    def test_trade_edge_passes(self):
        rm = make_rm()
        good_edge = make_edge(recommendation="TRADE")
        outcome = approved_call(rm, edge=good_edge)
        assert not any("Check 4" in f for f in outcome.failed_checks)

    def test_sanity_check_edge_passes(self):
        """SANITY_CHECK is not a rejection — it passes check 4."""
        rm = make_rm()
        sanity_edge = make_edge(recommendation="SANITY_CHECK")
        outcome = approved_call(rm, edge=sanity_edge)
        assert not any("Check 4" in f for f in outcome.failed_checks)


class TestCheck5EdgeSurvivesTakerFee:

    def test_negative_edge_after_taker_fails(self):
        rm = make_rm()
        bad_edge = make_edge(edge_after_taker=-0.01)
        outcome = approved_call(rm, edge=bad_edge)
        assert not outcome.approved
        assert any("Check 5" in f for f in outcome.failed_checks)

    def test_zero_edge_after_taker_fails(self):
        rm = make_rm()
        bad_edge = make_edge(edge_after_taker=0.0)
        outcome = approved_call(rm, edge=bad_edge)
        assert any("Check 5" in f for f in outcome.failed_checks)

    def test_positive_edge_after_taker_passes(self):
        rm = make_rm()
        outcome = approved_call(rm)  # default edge_after_taker=0.08
        assert not any("Check 5" in f for f in outcome.failed_checks)


class TestCheck6EnsembleMembers:

    def test_too_few_members_fails(self):
        rm = make_rm()
        thin_ensemble = make_ensemble(total_members=20)
        outcome = approved_call(rm, ensemble=thin_ensemble)
        assert not outcome.approved
        assert any("Check 6" in f for f in outcome.failed_checks)

    def test_exactly_at_minimum_passes(self):
        rm = make_rm()
        ensemble = make_ensemble(total_members=30)
        outcome = approved_call(rm, ensemble=ensemble)
        assert not any("Check 6" in f for f in outcome.failed_checks)

    def test_above_minimum_passes(self):
        rm = make_rm()
        outcome = approved_call(rm)  # 139 members
        assert not any("Check 6" in f for f in outcome.failed_checks)


class TestCheck7EnsembleConfidence:

    def test_low_confidence_fails(self):
        rm = make_rm()
        low_conf = make_ensemble(confidence="low")
        outcome = approved_call(rm, ensemble=low_conf)
        assert not outcome.approved
        assert any("Check 7" in f for f in outcome.failed_checks)

    def test_medium_confidence_passes(self):
        rm = make_rm()
        med_conf = make_ensemble(confidence="medium")
        outcome = approved_call(rm, ensemble=med_conf)
        assert not any("Check 7" in f for f in outcome.failed_checks)

    def test_high_confidence_passes(self):
        rm = make_rm()
        outcome = approved_call(rm)  # high confidence
        assert not any("Check 7" in f for f in outcome.failed_checks)


class TestCheck8Volume:

    def test_volume_below_minimum_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, volume_fp=30.0)
        assert not outcome.approved
        assert any("Check 8" in f for f in outcome.failed_checks)

    def test_volume_exactly_at_minimum_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, volume_fp=50.0)
        assert not any("Check 8" in f for f in outcome.failed_checks)

    def test_volume_above_minimum_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, volume_fp=200.0)
        assert not any("Check 8" in f for f in outcome.failed_checks)


class TestCheck9Spread:

    def test_spread_above_maximum_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, spread_dollars=0.10)
        assert not outcome.approved
        assert any("Check 9" in f for f in outcome.failed_checks)

    def test_spread_exactly_at_maximum_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, spread_dollars=0.08)
        assert not any("Check 9" in f for f in outcome.failed_checks)

    def test_spread_below_maximum_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, spread_dollars=0.03)
        assert not any("Check 9" in f for f in outcome.failed_checks)


class TestCheck10TimeToResolution:

    def test_too_close_to_resolution_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, hours_to_resolution=4.0)  # < 6h
        assert not outcome.approved
        assert any("Check 10" in f for f in outcome.failed_checks)

    def test_too_far_from_resolution_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, hours_to_resolution=200.0)  # > 7 days
        assert not outcome.approved
        assert any("Check 10" in f for f in outcome.failed_checks)

    def test_exactly_at_minimum_hours_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, hours_to_resolution=6.0)
        assert not any("Check 10" in f for f in outcome.failed_checks)

    def test_exactly_at_maximum_hours_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, hours_to_resolution=168.0)  # 7 * 24
        assert not any("Check 10" in f for f in outcome.failed_checks)

    def test_within_window_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, hours_to_resolution=24.0)
        assert not any("Check 10" in f for f in outcome.failed_checks)


class TestCheck11MarketStatus:

    def test_closed_market_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, market_status="closed")
        assert not outcome.approved
        assert any("Check 11" in f for f in outcome.failed_checks)

    def test_settled_market_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, market_status="settled")
        assert not outcome.approved
        assert any("Check 11" in f for f in outcome.failed_checks)

    def test_open_market_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, market_status="open")
        assert not any("Check 11" in f for f in outcome.failed_checks)

    def test_unopened_market_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, market_status="unopened")
        assert any("Check 11" in f for f in outcome.failed_checks)


# ===========================================================================
# Check 12: Contract price hard cap
# ===========================================================================

class TestCheck12ContractPriceCap:

    def test_entry_price_above_cap_fails(self):
        rm = make_rm()
        expensive = make_edge(entry_price=0.60)  # $0.60 > $0.50 cap
        outcome = approved_call(rm, edge=expensive)
        assert not outcome.approved
        assert any("Check 12" in f for f in outcome.failed_checks)

    def test_entry_price_at_cap_passes(self):
        rm = make_rm()
        at_cap = make_edge(entry_price=0.50)
        outcome = approved_call(rm, edge=at_cap)
        assert not any("Check 12" in f for f in outcome.failed_checks)

    def test_entry_price_below_cap_passes(self):
        rm = make_rm()
        cheap = make_edge(entry_price=0.20)
        outcome = approved_call(rm, edge=cheap)
        assert not any("Check 12" in f for f in outcome.failed_checks)


# ===========================================================================
# Open orders cap (non-numbered but enforced)
# ===========================================================================

class TestOpenOrdersCap:

    def test_at_max_open_orders_fails(self):
        rm = make_rm()
        outcome = approved_call(rm, open_orders_count=10)  # at the limit
        assert not outcome.approved
        assert any("open orders" in f.lower() for f in outcome.failed_checks)

    def test_below_max_open_orders_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, open_orders_count=9)
        assert not any("open orders" in f.lower() for f in outcome.failed_checks)

    def test_zero_open_orders_passes(self):
        rm = make_rm()
        outcome = approved_call(rm, open_orders_count=0)
        assert not any("open orders" in f.lower() for f in outcome.failed_checks)


# ===========================================================================
# Compound failures — multiple checks fail simultaneously
# ===========================================================================

class TestCompoundFailures:

    def test_all_checks_evaluated_not_short_circuit(self):
        """Even when one check fails, remaining checks are still evaluated."""
        rm = make_rm()
        rm.record_position_closed(10.0, pnl=-30.0)  # fail check 3
        thin_ensemble = make_ensemble(total_members=10)  # fail check 6
        bad_edge = make_edge(recommendation="NO_TRADE", skip_reason="no edge")  # fail check 4

        outcome = approved_call(
            rm,
            edge=bad_edge,
            ensemble=thin_ensemble,
            market_status="closed",  # fail check 11
        )

        assert not outcome.approved
        assert len(outcome.failed_checks) >= 3  # at least checks 3, 4, 6, 11

    def test_single_failure_still_rejects(self):
        """One failing check is enough to reject."""
        rm = make_rm()
        outcome = approved_call(rm, volume_fp=10.0)  # only check 8 fails
        assert not outcome.approved

    def test_no_failure_approves(self):
        rm = make_rm()
        outcome = approved_call(rm)
        assert outcome.approved
        assert len(outcome.failed_checks) == 0
