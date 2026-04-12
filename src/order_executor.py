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

STALE_ORDER_MINUTES: float = 30.0
PRICE_STALENESS_THRESHOLD: float = 0.02   # 2 cents — skip if mid drifted by this
MIN_ORDER_PRICE: float = 0.01
MAX_ORDER_PRICE: float = 0.99
MAX_CONTRACT_PRICE: float = 0.80  # hard cap — never pay more than $0.80 per contract on empty book
COMPETITIVE_PRICE_THRESHOLD: float = 0.10  # skip if our limit is >10¢ below the market bid for our side
EMPTY_BOOK_PRICE_THRESHOLD: float = 0.30   # empty-book fallback: skip if >30¢ below ensemble-implied fair bid


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
        # Step A diagnostic — logs the direction as received from the edge assessment.
        # This confirms whether direction is already wrong on arrival (mutation upstream)
        # or gets corrupted inside this function.
        logger.info(
            "place_order called: %s direction=%s entry_price=%.2f fair_value=%.2f",
            ticker, edge.direction, edge.entry_price, edge.fair_value,
        )

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
                # Full fallback: use NO fair value (1 - ensemble fair value) - 2¢ buffer.
                # Mirrors the BUY_YES fallback which uses fair_value - 0.02.
                # entry_price (1 - market_price) would be wrong here when the market is at 50¢
                # but ensemble strongly disagrees (e.g. NO should be 67¢, not 48¢).
                order_price = round(1.0 - edge.fair_value - 0.02, 2)

        order_price = max(MIN_ORDER_PRICE, min(MAX_ORDER_PRICE, round(order_price, 2)))

        # ------------------------------------------------------------------
        # Step 3b: Competitive price guard
        # Skip if our limit price is more than COMPETITIVE_PRICE_THRESHOLD below
        # the current market bid for the side we're buying. An order that far
        # below the book will never fill as a maker — it just occupies a slot.
        # Example: BUY_NO at $0.48 when NO is trading at $0.95 → gap $0.47 → skip.
        # ------------------------------------------------------------------
        if edge.direction == "BUY_YES":
            market_side_bid = fresh_yes_bid
        else:
            if fresh_no_bid > 0.0:
                market_side_bid = fresh_no_bid
            elif fresh_yes_ask < 1.0:
                market_side_bid = round(1.0 - fresh_yes_ask, 2)
            else:
                market_side_bid = 0.0

        if market_side_bid > 0.0 and order_price < market_side_bid - COMPETITIVE_PRICE_THRESHOLD:
            logger.info(
                "Competitive price guard: %s order_price=%.2f too far below market bid=%.2f "
                "(gap=%.2f, threshold=%.2f). Skipping.",
                ticker, order_price, market_side_bid,
                market_side_bid - order_price, COMPETITIVE_PRICE_THRESHOLD,
            )
            return _skipped(
                ticker, edge,
                skip_reason=(
                    f"Order price ${order_price:.2f} is ${market_side_bid - order_price:.2f} "
                    f"below market bid ${market_side_bid:.2f} — order would never fill"
                ),
                fresh_mid=fresh_mid,
            )

        # Empty-book fallback: when the orderbook returns no data, use ensemble fair value
        # to estimate where the market should be. Kalshi weather books are often empty on
        # new markets — but for near-certain outcomes the book has repriced and our $0.48
        # fallback would never fill. Use a wider threshold (30¢) since fair value ≠ market
        # price for mispriced markets we're trying to exploit.
        #   BUY_NO: fair NO price ≈ 1 - fair_value. If we're >30¢ below that, skip.
        #   BUY_YES: fair YES price ≈ fair_value.  If we're >30¢ below that, skip.
        if market_side_bid == 0.0:
            if edge.direction == "BUY_YES":
                implied_fair_bid = edge.fair_value
            else:
                implied_fair_bid = 1.0 - edge.fair_value
            if implied_fair_bid > 0.0 and order_price < implied_fair_bid - EMPTY_BOOK_PRICE_THRESHOLD:
                logger.info(
                    "Competitive price guard (empty book): %s order_price=%.2f far below "
                    "fair bid=%.2f (gap=%.2f, threshold=%.2f). Skipping.",
                    ticker, order_price, implied_fair_bid,
                    implied_fair_bid - order_price, EMPTY_BOOK_PRICE_THRESHOLD,
                )
                return _skipped(
                    ticker, edge,
                    skip_reason=(
                        f"Empty book: order ${order_price:.2f} is "
                        f"${implied_fair_bid - order_price:.2f} below ensemble-implied "
                        f"fair bid ${implied_fair_bid:.2f} — likely unfillable"
                    ),
                    fresh_mid=fresh_mid,
                )

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
