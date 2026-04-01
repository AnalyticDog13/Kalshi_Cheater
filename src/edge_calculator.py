"""
Edge Calculator.

Computes the fee-adjusted edge between our ensemble probability estimate and the
Kalshi market price. Decides whether to trade, how large, and which direction.

Three scenarios are always computed (spec section 5.1):
  1. Maker entry + hold to settlement  → fee = $0          (best case)
  2. Taker entry + hold to settlement  → fee = taker formula (likely case)
  3. Taker entry + taker exit before settlement → fee = 2x taker formula (worst case)

The system only trades if scenario 2 is profitable (conservative). Position
sizing uses scenario 1 (maker) since that's the target execution method.

Kelly formula for a binary market:
  f = edge / (1 - entry_price)     [full Kelly]
  We use quarter-Kelly for safety.

Fee formula (Kalshi taker fee per contract, rounded up to nearest cent):
  fee = ceil(coefficient * 1 * price * (1 - price) * 100) / 100

Edge thresholds come from settings.yaml via the EdgeParams dataclass —
never hardcoded in this file.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass

from src.data_feeds.weather_ensemble import EnsembleResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclass (loaded from settings.yaml by caller)
# ---------------------------------------------------------------------------

@dataclass
class EdgeParams:
    """
    All tunable edge-calculator parameters.
    Caller loads these from settings.yaml so we have no magic numbers here.
    """
    min_edge_high_confidence: float = 0.04
    min_edge_medium_confidence: float = 0.06
    min_edge_low_confidence: float = 0.10   # effectively skip low-confidence trades
    assumed_slippage: float = 0.01          # ¢ subtracted from edge before threshold comparison
    fee_multiple_required: float = 3.0      # edge must also cover N × taker fee
    kelly_fraction: float = 0.25            # quarter-Kelly
    extreme_price_lower_bound: float = 0.02
    extreme_price_upper_bound: float = 0.98
    sanity_check_edge_threshold: float = 0.15  # edges above this → SANITY_CHECK (from ai section)

    @classmethod
    def from_settings(cls, settings: dict) -> "EdgeParams":
        """Load from the 'edge' and 'ai' sections of settings.yaml (already parsed)."""
        e = settings.get("edge", {})
        ai = settings.get("ai", {})
        return cls(
            min_edge_high_confidence=e.get("min_edge_high_confidence", 0.04),
            min_edge_medium_confidence=e.get("min_edge_medium_confidence", 0.06),
            min_edge_low_confidence=e.get("min_edge_low_confidence", 0.10),
            assumed_slippage=e.get("assumed_slippage", 0.01),
            fee_multiple_required=e.get("fee_multiple_required", 3.0),
            kelly_fraction=e.get("kelly_fraction", 0.25),
            extreme_price_lower_bound=e.get("extreme_price_lower_bound", 0.02),
            extreme_price_upper_bound=e.get("extreme_price_upper_bound", 0.98),
            sanity_check_edge_threshold=ai.get("sanity_check_edge_threshold", 0.15),
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EdgeAssessment:
    """
    Full output of compute_edge(). Contains every number used in the decision.

    Fields grouped by category:
      - Market context: what we saw in the orderbook
      - Probabilities: ours vs theirs
      - Fee breakdown: three scenario costs
      - Sizing: Kelly → contracts → dollars
      - Decision: TRADE / NO_TRADE / SANITY_CHECK with skip reason
    """
    market_ticker: str

    # Market context
    market_price: float          # mid-price (0–1 scale)
    fair_value: float            # our ensemble probability
    direction: str               # "BUY_YES" or "BUY_NO"
    entry_price: float           # price we'd pay per contract (market_price or 1-market_price)
    fee_coefficient: float       # Kalshi fee coefficient for this series (typically 0.07)

    # Raw edge
    raw_edge: float              # |fair_value - market_price|

    # Three-scenario fee breakdown (per contract)
    taker_fee: float             # fee if taker entry (scenario 2 & 3)
    maker_fee: float             # $0 always (Kalshi charges nothing for maker)
    early_exit_taker_fee: float  # fee if we exit early as taker (scenario 3)

    # Edge after fees
    edge_after_maker: float      # raw_edge - 0 (scenario 1)
    edge_after_taker: float      # raw_edge - taker_fee (scenario 2 — used for trade/no-trade)
    edge_after_round_trip: float # raw_edge - taker_fee - early_exit_taker_fee (scenario 3)

    # Sizing
    kelly_fraction: float        # quarter-Kelly fraction
    recommended_contracts: int   # how many contracts to buy
    recommended_dollars: float   # dollar cost at entry_price

    # Decision
    recommendation: str          # "TRADE", "NO_TRADE", "SANITY_CHECK"
    skip_reason: str | None      # populated when recommendation is NO_TRADE

    def __str__(self) -> str:
        return (
            f"EdgeAssessment({self.market_ticker}: {self.recommendation} | "
            f"{self.direction} @ {self.entry_price:.2f} | "
            f"raw_edge={self.raw_edge:.1%} edge_after_taker={self.edge_after_taker:.1%} | "
            f"{self.recommended_contracts} contracts @ ${self.recommended_dollars:.2f})"
        )


# ---------------------------------------------------------------------------
# Fee computation helper
# ---------------------------------------------------------------------------

def _taker_fee_per_contract(coefficient: float, price: float) -> float:
    """
    Kalshi taker fee formula (per contract, rounded up to nearest cent).

      fee = ceil(coefficient * 1 * price * (1 - price) * 100) / 100

    The fee is maximized at price=0.50 and approaches zero at the tails.
    price must be in (0, 1) exclusive.
    """
    if price <= 0 or price >= 1:
        return 0.0
    raw = coefficient * 1.0 * price * (1.0 - price) * 100.0
    return math.ceil(raw) / 100.0


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_edge(
    market_ticker: str,
    market_price: float,
    ensemble_result: EnsembleResult,
    fee_coefficient: float,
    portfolio_balance: float,
    max_position_dollars: float,
    params: EdgeParams | None = None,
) -> EdgeAssessment:
    """
    Compute fee-adjusted edge and Kelly-optimal position size.

    Args:
        market_ticker:       Kalshi market ticker (for logging and result)
        market_price:        Current mid-price in (0,1) range, from market_mid_price()
        ensemble_result:     Full EnsembleResult from the probability engine
        fee_coefficient:     Kalshi taker fee coefficient (from series, typically 0.07)
        portfolio_balance:   Available balance in dollars (for Kelly sizing)
        max_position_dollars: Hard cap on single-market position (from risk settings)
        params:              EdgeParams; uses defaults if None

    Returns:
        EdgeAssessment with TRADE / NO_TRADE / SANITY_CHECK recommendation.
    """
    if params is None:
        params = EdgeParams()

    ensemble_prob = ensemble_result.probability
    confidence = ensemble_result.confidence   # "high", "medium", "low"

    # ------------------------------------------------------------------
    # Direction and entry price
    # ------------------------------------------------------------------
    if ensemble_prob > market_price:
        direction = "BUY_YES"
        entry_price = market_price          # we pay market_price per YES contract
    else:
        direction = "BUY_NO"
        entry_price = 1.0 - market_price    # we pay (1 - market_price) per NO contract

    raw_edge = abs(ensemble_prob - market_price)

    # ------------------------------------------------------------------
    # Guard: extreme prices — Kelly breaks down near 0 or 1
    # ------------------------------------------------------------------
    if entry_price <= params.extreme_price_lower_bound or entry_price >= params.extreme_price_upper_bound:
        return _no_trade(
            market_ticker, market_price, ensemble_prob, direction, entry_price,
            fee_coefficient, raw_edge, params,
            reason=f"Entry price {entry_price:.2f} outside safe Kelly range "
                   f"[{params.extreme_price_lower_bound}, {params.extreme_price_upper_bound}]",
        )

    # ------------------------------------------------------------------
    # Fee calculations — three scenarios
    # ------------------------------------------------------------------
    taker_fee = _taker_fee_per_contract(fee_coefficient, entry_price)
    maker_fee = 0.0

    # Exit fee is computed at the likely exit price — if we hold to settlement
    # the exit is free (settled by Kalshi at $1 or $0). If we exit early via a
    # limit sell, the price will be near entry_price (approximately). Using
    # entry_price for the exit fee is a conservative approximation.
    early_exit_taker_fee = _taker_fee_per_contract(fee_coefficient, entry_price)

    edge_after_maker = raw_edge - maker_fee           # scenario 1 (best)
    edge_after_taker = raw_edge - taker_fee           # scenario 2 (likely)
    edge_after_round_trip = raw_edge - taker_fee - early_exit_taker_fee  # scenario 3 (worst)

    # ------------------------------------------------------------------
    # Guard: taker scenario must be profitable (conservative)
    # ------------------------------------------------------------------
    if edge_after_taker <= 0:
        return _no_trade(
            market_ticker, market_price, ensemble_prob, direction, entry_price,
            fee_coefficient, raw_edge, params,
            reason=f"Edge {raw_edge:.1%} does not survive taker fee of {taker_fee:.2f}",
            taker_fee=taker_fee, maker_fee=maker_fee, early_exit_taker_fee=early_exit_taker_fee,
            edge_after_maker=edge_after_maker, edge_after_taker=edge_after_taker,
            edge_after_round_trip=edge_after_round_trip,
        )

    # ------------------------------------------------------------------
    # Minimum edge threshold
    # The minimum is the LARGER of:
    #   (a) confidence-based floor from settings
    #   (b) fee_multiple_required × taker fee (edge must cover N× the fee)
    # AND we deduct assumed_slippage before comparing.
    # ------------------------------------------------------------------
    if confidence == "high":
        confidence_floor = params.min_edge_high_confidence
    elif confidence == "medium":
        confidence_floor = params.min_edge_medium_confidence
    else:
        confidence_floor = params.min_edge_low_confidence

    fee_based_floor = params.fee_multiple_required * taker_fee
    min_edge = max(confidence_floor, fee_based_floor)

    # Effective edge after slippage deduction
    edge_after_slippage = raw_edge - params.assumed_slippage

    if edge_after_slippage < min_edge:
        return _no_trade(
            market_ticker, market_price, ensemble_prob, direction, entry_price,
            fee_coefficient, raw_edge, params,
            reason=(
                f"Edge {raw_edge:.1%} - slippage {params.assumed_slippage:.1%} "
                f"= {edge_after_slippage:.1%} < min {min_edge:.1%} "
                f"({confidence} confidence, fee floor {fee_based_floor:.1%})"
            ),
            taker_fee=taker_fee, maker_fee=maker_fee, early_exit_taker_fee=early_exit_taker_fee,
            edge_after_maker=edge_after_maker, edge_after_taker=edge_after_taker,
            edge_after_round_trip=edge_after_round_trip,
        )

    # ------------------------------------------------------------------
    # Kelly sizing — use maker edge (scenario 1) since that's the target
    # Binary Kelly: f = edge / (1 - entry_price)
    # ------------------------------------------------------------------
    full_kelly = edge_after_maker / (1.0 - entry_price)
    quarter_kelly = full_kelly * params.kelly_fraction

    position_dollars = min(
        portfolio_balance * quarter_kelly,
        max_position_dollars,
    )
    position_dollars = max(0.0, position_dollars)

    # Contracts: floor so we don't overshoot the dollar cap
    if entry_price > 0:
        recommended_contracts = max(1, int(position_dollars / entry_price))
        # Cap so we don't exceed position limit even after rounding
        max_contracts = int(max_position_dollars / entry_price)
        recommended_contracts = min(recommended_contracts, max_contracts)
    else:
        recommended_contracts = 0

    actual_dollars = recommended_contracts * entry_price

    # ------------------------------------------------------------------
    # Recommendation
    # ------------------------------------------------------------------
    if raw_edge > params.sanity_check_edge_threshold:
        # Large edge — needs AI sanity check before trading.
        # Threshold from settings.yaml ai.sanity_check_edge_threshold (default 0.15).
        recommendation = "SANITY_CHECK"
    else:
        recommendation = "TRADE"

    result = EdgeAssessment(
        market_ticker=market_ticker,
        market_price=market_price,
        fair_value=ensemble_prob,
        direction=direction,
        entry_price=entry_price,
        fee_coefficient=fee_coefficient,
        raw_edge=raw_edge,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
        early_exit_taker_fee=early_exit_taker_fee,
        edge_after_maker=edge_after_maker,
        edge_after_taker=edge_after_taker,
        edge_after_round_trip=edge_after_round_trip,
        kelly_fraction=quarter_kelly,
        recommended_contracts=recommended_contracts,
        recommended_dollars=actual_dollars,
        recommendation=recommendation,
        skip_reason=None,
    )

    logger.info(
        "Edge: %s | %s | raw=%.1%% after_taker=%.1%% | %d contracts $%.2f | %s",
        market_ticker, direction, raw_edge, edge_after_taker,
        recommended_contracts, actual_dollars, recommendation,
    )

    return result


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _no_trade(
    market_ticker: str,
    market_price: float,
    ensemble_prob: float,
    direction: str,
    entry_price: float,
    fee_coefficient: float,
    raw_edge: float,
    params: EdgeParams,
    reason: str,
    taker_fee: float = 0.0,
    maker_fee: float = 0.0,
    early_exit_taker_fee: float = 0.0,
    edge_after_maker: float = 0.0,
    edge_after_taker: float = 0.0,
    edge_after_round_trip: float = 0.0,
) -> EdgeAssessment:
    """Return a NO_TRADE EdgeAssessment with zero sizing."""
    logger.debug("NO_TRADE: %s — %s", market_ticker, reason)
    return EdgeAssessment(
        market_ticker=market_ticker,
        market_price=market_price,
        fair_value=ensemble_prob,
        direction=direction,
        entry_price=entry_price,
        fee_coefficient=fee_coefficient,
        raw_edge=raw_edge,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
        early_exit_taker_fee=early_exit_taker_fee,
        edge_after_maker=edge_after_maker,
        edge_after_taker=edge_after_taker,
        edge_after_round_trip=edge_after_round_trip,
        kelly_fraction=0.0,
        recommended_contracts=0,
        recommended_dollars=0.0,
        recommendation="NO_TRADE",
        skip_reason=reason,
    )
