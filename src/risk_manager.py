"""
Risk Manager.

Enforces the 12-point pre-trade checklist and hard stops for the small-bet live configuration.
Every trade must pass ALL checks — there are no overrides, no exceptions.

Hard limits (from settings.yaml):
  max_position_dollars:       $1    (one contract per trade at most prices)
  max_total_exposure_dollars: $10   (never more than $10 at risk at once)
  max_daily_loss_dollars:     $2    (stop trading for the day after $2 loss)
  max_contract_price_dollars: $0.50 (hard cap — never pay more than $0.50 per contract)

The risk manager is STATEFUL — it tracks total current exposure and today's
realized P&L so checks 2 and 3 are accurate across multiple calls in the
same trading session. The scanner/daemon is responsible for updating exposure
and P&L after fills resolve.

Design note: This module has NO async code and NO external I/O. It is pure
Python logic on numbers the caller passes in. This keeps it fully testable
without mocking anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum

from src.edge_calculator import EdgeAssessment, EdgeParams
from src.data_feeds.weather_ensemble import EnsembleResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk limits configuration
# ---------------------------------------------------------------------------

@dataclass
class RiskLimits:
    """
    All hard limits loaded from settings.yaml risk section.
    These are not guidelines — they are hard stops.
    """
    max_position_dollars: float = 1.0
    max_total_exposure_dollars: float = 10.0
    max_daily_loss_dollars: float = 2.0
    max_open_orders: int = 3
    max_contract_price_dollars: float = 0.50  # hard cap per contract

    # From scanner section
    min_volume_fp: float = 50.0
    max_spread_dollars: float = 0.08
    min_resolution_hours: float = 6.0
    max_resolution_days: float = 7.0

    # From ensemble section
    min_ensemble_members: int = 30

    @classmethod
    def from_settings(cls, settings: dict) -> "RiskLimits":
        r = settings.get("risk", {})
        s = settings.get("scanner", {})
        e = settings.get("ensemble", {})
        return cls(
            max_position_dollars=r.get("max_position_dollars", 1.0),
            max_total_exposure_dollars=r.get("max_total_exposure_dollars", 10.0),
            max_daily_loss_dollars=r.get("max_daily_loss_dollars", 2.0),
            max_open_orders=r.get("max_open_orders", 3),
            max_contract_price_dollars=r.get("max_contract_price_dollars", 0.50),
            min_volume_fp=s.get("min_volume_fp", 50.0),
            max_spread_dollars=s.get("max_spread_dollars", 0.08),
            min_resolution_hours=s.get("min_resolution_hours", 6.0),
            max_resolution_days=s.get("max_resolution_days", 7.0),
            min_ensemble_members=e.get("min_members_for_trade", 30),
        )


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------

class CheckResult(Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class RiskCheckOutcome:
    """
    Result of the full 11-point pre-trade checklist.

    approved = True only if ALL 11 checks pass.
    failed_checks lists every check that failed (not just the first one)
    so the operator can diagnose compound rejections.
    """
    approved: bool
    failed_checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if self.approved:
            return "RiskCheck: APPROVED"
        return f"RiskCheck: REJECTED — {', '.join(self.failed_checks)}"


# ---------------------------------------------------------------------------
# Risk Manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Stateful risk manager that tracks session-level exposure and P&L.

    The caller (daemon/scanner) must call:
      - record_position_opened(dollars) when an order fills
      - record_position_closed(dollars, pnl) when a position resolves or is sold
      - reset_daily_state() at the start of each trading day

    All other state (open_orders count, etc.) is passed directly into check_trade().
    """

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._total_exposure_dollars: float = 0.0
        self._daily_realized_pnl: float = 0.0
        self._state_date: date = datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------
    # State management (called by daemon after fills/resolutions)
    # ------------------------------------------------------------------

    def record_position_opened(self, dollars: float) -> None:
        """Call when an order fills. Increases tracked exposure."""
        self._total_exposure_dollars += dollars
        logger.debug("Exposure +$%.2f → total $%.2f", dollars, self._total_exposure_dollars)

    def record_position_closed(self, dollars: float, pnl: float) -> None:
        """
        Call when a position is closed (settlement or early exit).

        Args:
            dollars: the original entry cost of the position (reduces exposure)
            pnl:     net P&L of the closed position (negative = loss)
        """
        self._total_exposure_dollars = max(0.0, self._total_exposure_dollars - dollars)
        self._daily_realized_pnl += pnl
        logger.debug(
            "Position closed: PnL=%.2f, exposure $%.2f, day PnL $%.2f",
            pnl, self._total_exposure_dollars, self._daily_realized_pnl,
        )

    def reset_daily_state(self) -> None:
        """Call at the start of each calendar day to reset the daily P&L counter."""
        today = datetime.now(timezone.utc).date()
        logger.info(
            "Resetting daily P&L (was $%.2f) for new trading day %s",
            self._daily_realized_pnl, today,
        )
        self._daily_realized_pnl = 0.0
        self._state_date = today

    @property
    def total_exposure(self) -> float:
        return self._total_exposure_dollars

    @property
    def daily_pnl(self) -> float:
        return self._daily_realized_pnl

    # ------------------------------------------------------------------
    # Pre-trade checklist
    # ------------------------------------------------------------------

    def check_trade(
        self,
        edge: EdgeAssessment,
        ensemble: EnsembleResult,
        market_status: str,
        volume_fp: float,
        spread_dollars: float,
        hours_to_resolution: float,
        open_orders_count: int,
    ) -> RiskCheckOutcome:
        """
        Run the full 11-point pre-trade checklist.

        ALL checks must pass for the trade to be approved.
        The method evaluates ALL checks (not short-circuit) so the operator
        sees every failing check in one response.

        Args:
            edge:                EdgeAssessment from edge_calculator
            ensemble:            EnsembleResult — used for member count and confidence
            market_status:       Kalshi market status string (must be "open")
            volume_fp:           Total volume in contracts (from market.volume_fp)
            spread_dollars:      Bid-ask spread in dollars
            hours_to_resolution: Hours until market resolves
            open_orders_count:   How many open orders we currently have on Kalshi

        Returns:
            RiskCheckOutcome with approved=True only if all 11 pass.
        """
        failed: list[str] = []
        notes: list[str] = []

        # ----------------------------------------------------------
        # Check 1: Position size within per-market dollar limit
        # ----------------------------------------------------------
        if edge.recommended_dollars > self.limits.max_position_dollars:
            failed.append(
                f"Check 1 FAIL: position ${edge.recommended_dollars:.2f} "
                f"> max ${self.limits.max_position_dollars:.2f}"
            )
        else:
            notes.append(f"Check 1 OK: position ${edge.recommended_dollars:.2f}")

        # ----------------------------------------------------------
        # Check 2: Adding this position won't exceed total exposure cap
        # ----------------------------------------------------------
        projected_exposure = self._total_exposure_dollars + edge.recommended_dollars
        if projected_exposure > self.limits.max_total_exposure_dollars:
            failed.append(
                f"Check 2 FAIL: projected exposure ${projected_exposure:.2f} "
                f"> max ${self.limits.max_total_exposure_dollars:.2f} "
                f"(current ${self._total_exposure_dollars:.2f})"
            )
        else:
            notes.append(f"Check 2 OK: projected exposure ${projected_exposure:.2f}")

        # ----------------------------------------------------------
        # Check 3: Daily loss limit not breached
        # ----------------------------------------------------------
        if self._daily_realized_pnl <= -self.limits.max_daily_loss_dollars:
            failed.append(
                f"Check 3 FAIL: daily P&L ${self._daily_realized_pnl:.2f} "
                f"<= -${self.limits.max_daily_loss_dollars:.2f} limit"
            )
        else:
            notes.append(f"Check 3 OK: daily P&L ${self._daily_realized_pnl:.2f}")

        # ----------------------------------------------------------
        # Check 4: Edge above minimum for this confidence level
        # (Edge calculator already enforced this, but we double-check here
        #  since the edge object is the single source of truth.)
        # ----------------------------------------------------------
        if edge.recommendation == "NO_TRADE":
            failed.append(
                f"Check 4 FAIL: edge calculator rejected — {edge.skip_reason}"
            )
        else:
            notes.append(f"Check 4 OK: edge={edge.raw_edge:.1%} ({edge.recommendation})")

        # ----------------------------------------------------------
        # Check 5: Edge survives taker fee (even if we get a taker fill)
        # ----------------------------------------------------------
        if edge.edge_after_taker <= 0:
            failed.append(
                f"Check 5 FAIL: edge_after_taker={edge.edge_after_taker:.3f} <= 0"
            )
        else:
            notes.append(f"Check 5 OK: edge_after_taker={edge.edge_after_taker:.3f}")

        # ----------------------------------------------------------
        # Check 6: Ensemble has sufficient members
        # ----------------------------------------------------------
        if ensemble.total_members < self.limits.min_ensemble_members:
            failed.append(
                f"Check 6 FAIL: {ensemble.total_members} ensemble members "
                f"< min {self.limits.min_ensemble_members}"
            )
        else:
            notes.append(f"Check 6 OK: {ensemble.total_members} members")

        # ----------------------------------------------------------
        # Check 7: Ensemble confidence is medium or high (not low)
        # ----------------------------------------------------------
        if ensemble.confidence == "low":
            failed.append(
                f"Check 7 FAIL: ensemble confidence is 'low' "
                f"(spread={ensemble.ensemble_spread:.1f}F) — skip low confidence markets"
            )
        else:
            notes.append(f"Check 7 OK: confidence={ensemble.confidence}")

        # ----------------------------------------------------------
        # Check 8: Market has sufficient volume
        # ----------------------------------------------------------
        if volume_fp < self.limits.min_volume_fp:
            failed.append(
                f"Check 8 FAIL: volume {volume_fp:.0f} < min {self.limits.min_volume_fp:.0f}"
            )
        else:
            notes.append(f"Check 8 OK: volume={volume_fp:.0f}")

        # ----------------------------------------------------------
        # Check 9: Spread below maximum
        # ----------------------------------------------------------
        if spread_dollars > self.limits.max_spread_dollars:
            failed.append(
                f"Check 9 FAIL: spread ${spread_dollars:.3f} > max ${self.limits.max_spread_dollars:.3f}"
            )
        else:
            notes.append(f"Check 9 OK: spread=${spread_dollars:.3f}")

        # ----------------------------------------------------------
        # Check 10: Time to resolution within acceptable window
        # ----------------------------------------------------------
        if hours_to_resolution < self.limits.min_resolution_hours:
            failed.append(
                f"Check 10 FAIL: {hours_to_resolution:.1f}h to resolution "
                f"< min {self.limits.min_resolution_hours:.0f}h"
            )
        elif hours_to_resolution > self.limits.max_resolution_days * 24:
            failed.append(
                f"Check 10 FAIL: {hours_to_resolution:.1f}h to resolution "
                f"> max {self.limits.max_resolution_days:.0f} days"
            )
        else:
            notes.append(f"Check 10 OK: {hours_to_resolution:.1f}h to resolution")

        # ----------------------------------------------------------
        # Check 11: Market status is "open"
        # ----------------------------------------------------------
        if market_status not in ("open", "active"):
            failed.append(
                f"Check 11 FAIL: market status is '{market_status}' (need 'open' or 'active')"
            )
        else:
            notes.append(f"Check 11 OK: market status={market_status}")

        # ----------------------------------------------------------
        # Check 12: Contract price hard cap — never pay > $0.50 per contract
        # This ensures individual bets stay tiny regardless of Kelly sizing.
        # ----------------------------------------------------------
        if edge.entry_price > self.limits.max_contract_price_dollars:
            failed.append(
                f"Check 12 FAIL: entry_price ${edge.entry_price:.2f} "
                f"> max_contract_price ${self.limits.max_contract_price_dollars:.2f}"
            )
        else:
            notes.append(f"Check 12 OK: entry_price=${edge.entry_price:.2f}")

        # ----------------------------------------------------------
        # Also enforce: open orders cap
        # (Not in the formal numbered list but a hard operational limit)
        # ----------------------------------------------------------
        if open_orders_count >= self.limits.max_open_orders:
            failed.append(
                f"Open orders cap FAIL: {open_orders_count} >= max {self.limits.max_open_orders}"
            )

        approved = len(failed) == 0

        if approved:
            logger.info(
                "Risk check APPROVED: %s | $%.2f @ %s",
                edge.market_ticker, edge.recommended_dollars, edge.direction,
            )
        else:
            logger.warning(
                "Risk check REJECTED: %s | %d check(s) failed: %s",
                edge.market_ticker, len(failed), "; ".join(failed),
            )

        return RiskCheckOutcome(approved=approved, failed_checks=failed, notes=notes)

    # ------------------------------------------------------------------
    # Convenience: is trading halted for today?
    # ------------------------------------------------------------------

    def is_daily_loss_limit_breached(self) -> bool:
        """True if we've hit the daily loss limit and should stop trading."""
        return self._daily_realized_pnl <= -self.limits.max_daily_loss_dollars
