"""
Order Executor.

Places limit orders on Kalshi and cancels stale open orders.

Execution strategy:
  - ALWAYS limit orders — market orders are forbidden by policy.
  - Target maker fills: place at the current best bid (BUY_YES) or best NO bid
    (BUY_NO), resting on the book. Maker fills are zero-fee.
  - Staleness guard: re-fetch orderbook immediately before placing. If the
    mid-price has moved more than PRICE_STALENESS_THRESHOLD (2¢) since the edge
    was computed, skip the trade. The daemon will pick it up in the next scan.
  - Depth check: cap the order size to the quantity available at the best price
    level. Avoids orders that would partially cross to worse prices.
  - Stale cancellation: cancel any open order older than STALE_ORDER_MINUTES (10).
    Stale orders are dead weight — they block the open-orders count check and
    indicate the market moved away from us.

All methods are async. No state is held between calls — this class is stateless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.kalshi_client import KalshiClient, parse_price, parse_quantity
from src.edge_calculator import EdgeAssessment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALE_ORDER_MINUTES: float = 10.0
PRICE_STALENESS_THRESHOLD: float = 0.02   # 2 cents — skip if mid drifted by this
MIN_ORDER_PRICE: float = 0.01
MAX_ORDER_PRICE: float = 0.99
MAX_CONTRACT_PRICE: float = 0.50  # hard cap — never pay more than $0.50 per contract


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    """
    Outcome of a single place_order() call.

    was_placed=True means the limit order was submitted to Kalshi.
    was_placed=False means a guard triggered and no order was sent.
    In either case, the caller should check skip_reason for diagnostics.
    """
    ticker: str
    direction: str          # "BUY_YES" or "BUY_NO"
    side: str               # "yes" or "no" (Kalshi API field)
    price: float            # limit price in dollars
    count: int | float      # contracts ordered (0 if skipped)
    order_id: str | None    # Kalshi order ID (None if not placed)
    was_placed: bool
    skip_reason: str | None
    edge_at_placement: float    # raw_edge from EdgeAssessment
    fresh_mid_price: float      # actual mid-price at placement time

    def __str__(self) -> str:
        if self.was_placed:
            return (
                f"OrderResult(PLACED {self.side.upper()} {self.count} @ {self.price:.2f} "
                f"on {self.ticker} | order_id={self.order_id})"
            )
        return f"OrderResult(SKIPPED {self.ticker}: {self.skip_reason})"


# ---------------------------------------------------------------------------
# Order Executor
# ---------------------------------------------------------------------------

class OrderExecutor:
    """
    Stateless async executor. Inject a KalshiClient and call place_order()
    or monitor_open_orders() from the daemon loop.

    All guards return was_placed=False with a skip_reason; they never raise.
    API errors from create_order() propagate up to the caller.
    """

    def __init__(self, kalshi_client: KalshiClient) -> None:
        self.client = kalshi_client

    # ------------------------------------------------------------------
    # Place a limit order
    # ------------------------------------------------------------------

    async def place_order(
        self,
        edge: EdgeAssessment,
        ticker: str,
    ) -> OrderResult:
        """
        Place a maker-targeted limit order based on an EdgeAssessment.

        Guard sequence:
          1. Re-fetch orderbook → compute fresh mid-price
          2. Staleness check: skip if mid moved > PRICE_STALENESS_THRESHOLD
          3. Compute maker price (at best bid, not crossing spread)
          4. Depth check: cap count to available liquidity at best price
          5. Place limit order via KalshiClient.create_order()

        Args:
            edge:   EdgeAssessment from edge_calculator (direction, sizing, price)
            ticker: Kalshi market ticker

        Returns:
            OrderResult with was_placed=True on successful submission.
        """
        # ------------------------------------------------------------------
        # Step 1: Fresh orderbook
        # ------------------------------------------------------------------
        book = await self.client.get_orderbook(ticker)

        fresh_yes_bid = parse_price(getattr(book, "yes_bid_dollars", None), default=0.0)
        fresh_yes_ask = parse_price(getattr(book, "yes_ask_dollars", None), default=1.0)

        # Compute mid only from the sides that are actually present.
        # A bid of 0 or ask of 1.0 means that side of the book is empty —
        # averaging a real price with 0 or 1 would give a misleading mid and
        # could falsely trip the staleness guard.
        if fresh_yes_bid > 0.0 and fresh_yes_ask < 1.0:
            fresh_mid = (fresh_yes_bid + fresh_yes_ask) / 2.0
        elif fresh_yes_bid > 0.0:
            fresh_mid = fresh_yes_bid       # only bids present
        elif fresh_yes_ask < 1.0:
            fresh_mid = fresh_yes_ask       # only asks present
        else:
            fresh_mid = edge.market_price   # completely empty book — no drift check

        # ------------------------------------------------------------------
        # Step 2: Staleness guard
        # ------------------------------------------------------------------
        price_drift = abs(fresh_mid - edge.market_price)
        if price_drift > PRICE_STALENESS_THRESHOLD:
            logger.info(
                "Staleness guard: %s price drifted $%.3f (was %.3f → now %.3f). Skipping.",
                ticker, price_drift, edge.market_price, fresh_mid,
            )
            return _skipped(
                ticker, edge,
                skip_reason=(
                    f"Price drifted ${price_drift:.3f} since edge computed "
                    f"(threshold ${PRICE_STALENESS_THRESHOLD:.2f})"
                ),
                fresh_mid=fresh_mid,
            )

        # ------------------------------------------------------------------
        # Step 3: Maker price and side
        # ------------------------------------------------------------------
        if edge.direction == "BUY_YES":
            side = "yes"
            bid_size = parse_quantity(getattr(book, "yes_bid_size_fp", None), default=0.0)
            if fresh_yes_bid > 0.0:
                order_price = fresh_yes_bid
            else:
                # Empty book — place just below our fair value, capped at hard price limit.
                # Without the cap, 100%-probability markets would produce orders at $0.98+.
                order_price = min(round(edge.fair_value - 0.02, 2), MAX_CONTRACT_PRICE)

        else:  # BUY_NO
            side = "no"
            fresh_no_bid = parse_price(getattr(book, "no_bid_dollars", None), default=0.0)
            bid_size = parse_quantity(getattr(book, "no_bid_size_fp", None), default=0.0)

            if fresh_no_bid > 0.0:
                order_price = fresh_no_bid
            elif fresh_yes_ask < 1.0:
                # Derive NO bid from YES ask: NO bid ≈ 1 - YES ask
                order_price = round(1.0 - fresh_yes_ask, 2)
            else:
                # Full fallback: use entry_price - 2¢ buffer
                order_price = round(edge.entry_price - 0.02, 2)

        order_price = max(MIN_ORDER_PRICE, min(MAX_ORDER_PRICE, round(order_price, 2)))

        # ------------------------------------------------------------------
        # Step 4: Depth check
        # ------------------------------------------------------------------
        order_count: int | float = edge.recommended_contracts

        if bid_size > 0.0:
            # Cap to available liquidity at the best price level
            order_count = min(order_count, max(1, int(bid_size)))

        if order_count <= 0:
            return _skipped(
                ticker, edge,
                skip_reason="Depth check: zero contracts to order",
                fresh_mid=fresh_mid,
            )

        # ------------------------------------------------------------------
        # Step 5: Place order
        # ------------------------------------------------------------------
        logger.info(
            "Placing maker limit %s @ %.2f x%s on %s (edge=%.1f%%)",
            side.upper(), order_price, order_count, ticker, edge.raw_edge * 100,
        )

        order = await self.client.create_order(
            ticker=ticker,
            side=side,
            order_type="limit",
            price=order_price,
            count=order_count,
        )

        order_id = (
            getattr(order, "order_id", None)
            or getattr(order, "id", None)
        )

        return OrderResult(
            ticker=ticker,
            direction=edge.direction,
            side=side,
            price=order_price,
            count=order_count,
            order_id=str(order_id) if order_id is not None else None,
            was_placed=True,
            skip_reason=None,
            edge_at_placement=edge.raw_edge,
            fresh_mid_price=fresh_mid,
        )

    # ------------------------------------------------------------------
    # Stale order cancellation
    # ------------------------------------------------------------------

    async def monitor_open_orders(self) -> list[str]:
        """
        Fetch all open orders and cancel any that are older than
        STALE_ORDER_MINUTES (default 10 minutes).

        Returns:
            List of cancelled order IDs.

        Notes:
            - A stale order means the market has likely moved away from our
              entry price. Holding it risks a fill at a price where the edge
              no longer exists.
            - Individual cancel failures are logged but do not interrupt the
              loop — we still try to cancel all other stale orders.
        """
        _RESTING_STATUSES = frozenset({"resting", "open", "pending"})
        all_orders = await self.client.get_orders(status=None)
        orders = [o for o in all_orders if str(getattr(o, "status", "")).lower() in _RESTING_STATUSES]
        cancelled: list[str] = []
        now = datetime.now(timezone.utc)

        for order in orders:
            created = _parse_order_created_time(order)
            if created is None:
                logger.debug(
                    "Could not parse created_time for order %s — skipping age check",
                    getattr(order, "order_id", "?"),
                )
                continue

            age_minutes = (now - created).total_seconds() / 60.0

            if age_minutes >= STALE_ORDER_MINUTES:
                order_id = getattr(order, "order_id", None)
                if order_id is None:
                    continue
                order_id_str = str(order_id)
                try:
                    await self.client.cancel_order(order_id_str)
                    cancelled.append(order_id_str)
                    logger.info(
                        "Cancelled stale order %s (%.0f min old)",
                        order_id_str, age_minutes,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to cancel order %s: %s: %s",
                        order_id_str, type(exc).__name__, exc,
                    )

        if cancelled:
            logger.info("monitor_open_orders: cancelled %d stale order(s)", len(cancelled))

        return cancelled


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _skipped(
    ticker: str,
    edge: EdgeAssessment,
    skip_reason: str,
    fresh_mid: float,
) -> OrderResult:
    """Convenience constructor for a skipped (not placed) OrderResult."""
    return OrderResult(
        ticker=ticker,
        direction=edge.direction,
        side="yes" if edge.direction == "BUY_YES" else "no",
        price=0.0,
        count=0,
        order_id=None,
        was_placed=False,
        skip_reason=skip_reason,
        edge_at_placement=edge.raw_edge,
        fresh_mid_price=fresh_mid,
    )


def _parse_order_created_time(order: Any) -> datetime | None:
    """
    Extract creation datetime from a Kalshi order object.

    Tries multiple attribute names to handle SDK version differences.
    Returns timezone-aware UTC datetime, or None if no parseable time found.
    """
    for attr in ("created_time", "created_at", "place_time"):
        val = getattr(order, attr, None)
        if val is None:
            continue
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None
