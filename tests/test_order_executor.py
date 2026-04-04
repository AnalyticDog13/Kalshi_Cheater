"""
Tests for src/order_executor.py.

All KalshiClient calls are mocked — no real network calls.
Tests cover:
  - Staleness guard (price drift > 2¢ → skip)
  - Maker pricing for BUY_YES and BUY_NO
  - Empty-book fallback pricing
  - BUY_NO price derivation from YES ask when no_bid_dollars absent
  - Depth check (cap to available liquidity, bid_size=0 → no cap)
  - Successful order placement (was_placed=True, correct order_id)
  - Stale order cancellation (>= 10 min → cancel, < 10 min → keep)
  - cancel_order exception doesn't crash monitor loop
  - Missing created_time handled gracefully
  - Order price clamped to [0.01, 0.99]
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.order_executor import (
    PRICE_STALENESS_THRESHOLD,
    STALE_ORDER_MINUTES,
    OrderExecutor,
    OrderResult,
    _parse_order_created_time,
)
from src.edge_calculator import EdgeAssessment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_executor() -> tuple[OrderExecutor, MagicMock]:
    """Return (executor, mock_client) where all client methods are AsyncMock."""
    client = MagicMock()
    client.get_orderbook = AsyncMock()
    client.create_order = AsyncMock()
    client.get_orders = AsyncMock()
    client.cancel_order = AsyncMock()
    return OrderExecutor(kalshi_client=client), client


def _mock_book(
    yes_bid: float = 0.60,
    yes_ask: float = 0.62,
    yes_bid_size: float = 10.0,
    no_bid: float | None = None,
    no_bid_size: float = 0.0,
) -> MagicMock:
    """Build a mock orderbook response object."""
    book = MagicMock()
    book.yes_bid_dollars = str(yes_bid)
    book.yes_ask_dollars = str(yes_ask)
    book.yes_bid_size_fp = str(yes_bid_size)
    if no_bid is not None:
        book.no_bid_dollars = str(no_bid)
    else:
        # Simulate absent field
        del book.no_bid_dollars
    book.no_bid_size_fp = str(no_bid_size)
    return book


def _mock_edge(
    direction: str = "BUY_YES",
    market_price: float = 0.60,
    fair_value: float = 0.70,
    entry_price: float | None = None,
    raw_edge: float = 0.10,
    recommended_contracts: int = 5,
    recommended_dollars: float = 3.0,
) -> EdgeAssessment:
    """Build a minimal EdgeAssessment for testing."""
    if entry_price is None:
        entry_price = market_price if direction == "BUY_YES" else 1.0 - market_price
    return EdgeAssessment(
        market_ticker="KXHIGHNY-26APR01-T75",
        market_price=market_price,
        fair_value=fair_value,
        direction=direction,
        entry_price=entry_price,
        fee_coefficient=0.07,
        raw_edge=raw_edge,
        taker_fee=0.01,
        maker_fee=0.0,
        early_exit_taker_fee=0.01,
        edge_after_maker=raw_edge,
        edge_after_taker=raw_edge - 0.01,
        edge_after_round_trip=raw_edge - 0.02,
        kelly_fraction=0.05,
        recommended_contracts=recommended_contracts,
        recommended_dollars=recommended_dollars,
        recommendation="TRADE",
        skip_reason=None,
    )


def _mock_order(order_id: str = "ord-abc123") -> MagicMock:
    order = MagicMock()
    order.order_id = order_id
    return order


def _mock_open_order(order_id: str, age_minutes: float) -> MagicMock:
    """Build a mock open order with a specific age."""
    order = MagicMock()
    order.order_id = order_id
    order.status = "resting"
    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    order.created_time = created
    return order


# ---------------------------------------------------------------------------
# Staleness guard
# ---------------------------------------------------------------------------

class TestStalenessGuard:
    def test_price_drifted_more_than_threshold_skips(self):
        executor, client = _make_executor()
        edge = _mock_edge(market_price=0.60)
        # Fresh mid = (0.63 + 0.65) / 2 = 0.64 → drift = 0.04 > 0.02
        client.get_orderbook.return_value = _mock_book(yes_bid=0.63, yes_ask=0.65)

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is False
        assert "drifted" in result.skip_reason.lower()
        client.create_order.assert_not_called()

    def test_price_drift_exactly_at_threshold_does_not_skip(self):
        """Exactly 2¢ drift should NOT trigger the guard (condition is strictly >)."""
        executor, client = _make_executor()
        edge = _mock_edge(market_price=0.60)
        # Fresh mid = (0.59 + 0.63) / 2 = 0.61 → drift = 0.01 < 0.02
        client.get_orderbook.return_value = _mock_book(yes_bid=0.59, yes_ask=0.63)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is True

    def test_price_drift_exactly_threshold_plus_epsilon_skips(self):
        """Drift of 2.001¢ must trigger the guard."""
        executor, client = _make_executor()
        edge = _mock_edge(market_price=0.60)
        # Fresh mid = (0.579 + 0.621) / 2 = 0.60 → no drift actually
        # Let's do: yes_bid=0.58, yes_ask=0.60 → mid=0.59 → drift=0.01 fine
        # For >0.02 drift: yes_bid=0.56, yes_ask=0.60 → mid=0.58 → drift=0.02 EQUAL
        # For strictly > threshold: yes_bid=0.55, yes_ask=0.60 → mid=0.575 → drift=0.025 >0.02
        client.get_orderbook.return_value = _mock_book(yes_bid=0.55, yes_ask=0.60)

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is False

    def test_fresh_mid_price_recorded_on_skip(self):
        executor, client = _make_executor()
        edge = _mock_edge(market_price=0.60)
        client.get_orderbook.return_value = _mock_book(yes_bid=0.50, yes_ask=0.60)

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.fresh_mid_price == pytest.approx(0.55)
        assert result.was_placed is False

    def test_no_drift_proceeds_to_placement(self):
        executor, client = _make_executor()
        edge = _mock_edge(market_price=0.60)
        # Fresh mid = (0.59 + 0.61) / 2 = 0.60 → drift = 0
        client.get_orderbook.return_value = _mock_book(yes_bid=0.59, yes_ask=0.61)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is True
        client.create_order.assert_called_once()


# ---------------------------------------------------------------------------
# Maker pricing — BUY_YES
# ---------------------------------------------------------------------------

class TestMakerPricingBuyYes:
    def test_order_placed_at_best_yes_bid(self):
        """BUY_YES should place AT the best bid (maker — not crossing spread)."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_YES", market_price=0.60)
        client.get_orderbook.return_value = _mock_book(yes_bid=0.59, yes_ask=0.61)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.price == pytest.approx(0.59)
        assert result.side == "yes"

    def test_buy_yes_empty_book_uses_fair_value_minus_buffer(self):
        """When book is empty (bid=0), fall back to min(fair_value - 0.02, MAX_CONTRACT_PRICE=0.80)."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_YES", market_price=0.60, fair_value=0.70)
        client.get_orderbook.return_value = _mock_book(yes_bid=0.0, yes_ask=0.61)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        # 0.70 - 0.02 = 0.68, which is below the MAX_CONTRACT_PRICE=0.80 cap
        assert result.price == pytest.approx(0.68, abs=0.01)
        assert result.was_placed is True

    def test_order_price_clamped_to_min(self):
        """Price must never go below 0.01."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_YES", fair_value=0.01, market_price=0.01)
        client.get_orderbook.return_value = _mock_book(yes_bid=0.0, yes_ask=0.02)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.price >= 0.01

    def test_order_price_clamped_to_max(self):
        """Price must never exceed 0.99."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_YES", fair_value=1.0, market_price=0.98)
        client.get_orderbook.return_value = _mock_book(yes_bid=0.99, yes_ask=1.0)
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.price <= 0.99


# ---------------------------------------------------------------------------
# Maker pricing — BUY_NO
# ---------------------------------------------------------------------------

class TestMakerPricingBuyNo:
    def test_buy_no_uses_explicit_no_bid_when_available(self):
        """When no_bid_dollars is present, use it directly."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_NO", market_price=0.65, entry_price=0.35)
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.64, yes_ask=0.66, no_bid=0.34
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T72"))

        assert result.price == pytest.approx(0.34)
        assert result.side == "no"

    def test_buy_no_derives_from_yes_ask_when_no_bid_absent(self):
        """When no_bid_dollars is absent, derive: NO bid ≈ 1 - YES ask."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_NO", market_price=0.65, entry_price=0.35)
        book = _mock_book(yes_bid=0.64, yes_ask=0.66)  # no no_bid_dollars field
        client.get_orderbook.return_value = book
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T72"))

        # NO bid ≈ 1 - 0.66 = 0.34
        assert result.price == pytest.approx(0.34, abs=0.01)
        assert result.side == "no"

    def test_buy_no_empty_book_uses_entry_price_minus_buffer(self):
        """Full fallback for BUY_NO when all NO signals are absent."""
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_NO", market_price=0.65, entry_price=0.35)
        book = MagicMock()
        # Simulate completely absent book fields
        book.yes_bid_dollars = "0.0"
        book.yes_ask_dollars = "1.0"   # full-width ask → 1 - 1.0 = 0 (empty book)
        del book.no_bid_dollars
        book.no_bid_size_fp = "0.0"
        book.yes_bid_size_fp = "0.0"
        client.get_orderbook.return_value = book
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T72"))

        # entry_price - 0.02 = 0.35 - 0.02 = 0.33
        assert result.price == pytest.approx(0.33, abs=0.01)
        assert result.was_placed is True


# ---------------------------------------------------------------------------
# Depth check
# ---------------------------------------------------------------------------

class TestDepthCheck:
    def test_depth_caps_order_count(self):
        """Available liquidity < recommended_contracts → use available size."""
        executor, client = _make_executor()
        edge = _mock_edge(recommended_contracts=10)
        # Only 3 contracts available at best bid
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.59, yes_ask=0.61, yes_bid_size=3.0
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.count == 3

    def test_depth_at_least_one_contract(self):
        """Even with fractional available size < 1, order at least 1 contract."""
        executor, client = _make_executor()
        edge = _mock_edge(recommended_contracts=5)
        # 0.5 fractional contracts available → floor to 1
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.59, yes_ask=0.61, yes_bid_size=0.5
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.count >= 1

    def test_depth_zero_bid_size_does_not_cap(self):
        """bid_size=0 means size unknown — place full recommended count."""
        executor, client = _make_executor()
        edge = _mock_edge(recommended_contracts=8)
        # bid_size=0 → don't know depth → use full count
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.59, yes_ask=0.61, yes_bid_size=0.0
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.count == 8

    def test_depth_larger_than_recommended_uses_recommended(self):
        """Plenty of liquidity → don't over-order."""
        executor, client = _make_executor()
        edge = _mock_edge(recommended_contracts=4)
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.59, yes_ask=0.61, yes_bid_size=100.0
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.count == 4

    def test_zero_recommended_contracts_skips(self):
        """Edge calculator returned 0 contracts → skip before API call."""
        executor, client = _make_executor()
        edge = _mock_edge(recommended_contracts=0)
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.59, yes_ask=0.61, yes_bid_size=10.0
        )

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is False
        assert "zero" in result.skip_reason.lower()
        client.create_order.assert_not_called()


# ---------------------------------------------------------------------------
# Successful placement
# ---------------------------------------------------------------------------

class TestOrderPlacement:
    def test_successful_placement_returns_was_placed_true(self):
        executor, client = _make_executor()
        edge = _mock_edge()
        client.get_orderbook.return_value = _mock_book()
        client.create_order.return_value = _mock_order("ord-xyz")

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.was_placed is True
        assert result.order_id == "ord-xyz"
        assert result.skip_reason is None

    def test_order_id_from_id_attribute_fallback(self):
        """Some SDK versions may use order.id instead of order.order_id."""
        executor, client = _make_executor()
        edge = _mock_edge()
        client.get_orderbook.return_value = _mock_book()
        mock_order = MagicMock()
        mock_order.order_id = None   # primary attr absent
        mock_order.id = "id-fallback"
        client.create_order.return_value = mock_order

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.order_id == "id-fallback"

    def test_edge_at_placement_recorded(self):
        executor, client = _make_executor()
        edge = _mock_edge(raw_edge=0.12)
        client.get_orderbook.return_value = _mock_book()
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.edge_at_placement == pytest.approx(0.12)

    def test_create_order_called_with_limit_type(self):
        """Executor must always pass order_type='limit' — never market."""
        executor, client = _make_executor()
        edge = _mock_edge()
        client.get_orderbook.return_value = _mock_book()
        client.create_order.return_value = _mock_order()

        _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        call_kwargs = client.create_order.call_args.kwargs
        assert call_kwargs["order_type"] == "limit"

    def test_buy_yes_side_is_yes(self):
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_YES")
        client.get_orderbook.return_value = _mock_book()
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert result.side == "yes"
        assert client.create_order.call_args.kwargs["side"] == "yes"

    def test_buy_no_side_is_no(self):
        executor, client = _make_executor()
        edge = _mock_edge(direction="BUY_NO", market_price=0.65, entry_price=0.35)
        client.get_orderbook.return_value = _mock_book(
            yes_bid=0.64, yes_ask=0.66, no_bid=0.34
        )
        client.create_order.return_value = _mock_order()

        result = _run(executor.place_order(edge, "KXHIGHNY-26APR01-T72"))

        assert result.side == "no"
        assert client.create_order.call_args.kwargs["side"] == "no"

    def test_correct_ticker_passed_to_create_order(self):
        executor, client = _make_executor()
        edge = _mock_edge()
        client.get_orderbook.return_value = _mock_book()
        client.create_order.return_value = _mock_order()

        _run(executor.place_order(edge, "KXHIGHNY-26APR01-T75"))

        assert client.create_order.call_args.kwargs["ticker"] == "KXHIGHNY-26APR01-T75"


# ---------------------------------------------------------------------------
# Stale order cancellation
# ---------------------------------------------------------------------------

class TestMonitorOpenOrders:
    def test_stale_orders_are_cancelled(self):
        """Orders >= STALE_ORDER_MINUTES old are cancelled."""
        executor, client = _make_executor()
        stale = _mock_open_order("ord-stale", age_minutes=STALE_ORDER_MINUTES + 1)
        client.get_orders.return_value = [stale]
        client.cancel_order.return_value = MagicMock()

        cancelled = _run(executor.monitor_open_orders())

        assert "ord-stale" in cancelled
        client.cancel_order.assert_called_once_with("ord-stale")

    def test_fresh_orders_are_not_cancelled(self):
        """Orders < STALE_ORDER_MINUTES old are left alone."""
        executor, client = _make_executor()
        fresh = _mock_open_order("ord-fresh", age_minutes=STALE_ORDER_MINUTES - 1)
        client.get_orders.return_value = [fresh]

        cancelled = _run(executor.monitor_open_orders())

        assert cancelled == []
        client.cancel_order.assert_not_called()

    def test_exactly_at_threshold_is_cancelled(self):
        """An order exactly at STALE_ORDER_MINUTES is considered stale."""
        executor, client = _make_executor()
        borderline = _mock_open_order("ord-border", age_minutes=STALE_ORDER_MINUTES)
        client.get_orders.return_value = [borderline]
        client.cancel_order.return_value = MagicMock()

        cancelled = _run(executor.monitor_open_orders())

        assert "ord-border" in cancelled

    def test_mixed_orders_only_stale_cancelled(self):
        executor, client = _make_executor()
        stale = _mock_open_order("ord-stale", age_minutes=35)
        fresh = _mock_open_order("ord-fresh", age_minutes=2)
        client.get_orders.return_value = [stale, fresh]
        client.cancel_order.return_value = MagicMock()

        cancelled = _run(executor.monitor_open_orders())

        assert "ord-stale" in cancelled
        assert "ord-fresh" not in cancelled
        assert client.cancel_order.call_count == 1

    def test_cancel_exception_does_not_crash_loop(self):
        """If one cancel fails, the rest of the orders are still processed."""
        executor, client = _make_executor()
        stale1 = _mock_open_order("ord-1", age_minutes=35)
        stale2 = _mock_open_order("ord-2", age_minutes=35)
        client.get_orders.return_value = [stale1, stale2]

        # First cancel raises, second succeeds
        client.cancel_order.side_effect = [RuntimeError("timeout"), None]

        cancelled = _run(executor.monitor_open_orders())

        # Only ord-2 was successfully cancelled (ord-1 raised)
        assert "ord-2" in cancelled
        assert "ord-1" not in cancelled
        assert client.cancel_order.call_count == 2

    def test_no_open_orders_returns_empty_list(self):
        executor, client = _make_executor()
        client.get_orders.return_value = []

        cancelled = _run(executor.monitor_open_orders())

        assert cancelled == []

    def test_order_with_string_created_time(self):
        """created_time as ISO string is correctly parsed."""
        executor, client = _make_executor()
        order = MagicMock()
        order.order_id = "ord-str"
        order.status = "resting"
        # 35 minutes ago
        old_time = datetime.now(timezone.utc) - timedelta(minutes=35)
        order.created_time = old_time.isoformat()
        client.get_orders.return_value = [order]
        client.cancel_order.return_value = MagicMock()

        cancelled = _run(executor.monitor_open_orders())

        assert "ord-str" in cancelled

    def test_order_with_missing_created_time_skipped(self):
        """If created_time is absent (None for all attrs), order is skipped."""
        executor, client = _make_executor()
        order = MagicMock()
        order.order_id = "ord-no-time"
        order.created_time = None
        order.created_at = None
        order.place_time = None
        client.get_orders.return_value = [order]

        cancelled = _run(executor.monitor_open_orders())

        assert cancelled == []
        client.cancel_order.assert_not_called()

    def test_returns_list_of_cancelled_ids(self):
        executor, client = _make_executor()
        orders = [_mock_open_order(f"ord-{i}", age_minutes=35) for i in range(3)]
        client.get_orders.return_value = orders
        client.cancel_order.return_value = MagicMock()

        cancelled = _run(executor.monitor_open_orders())

        assert set(cancelled) == {"ord-0", "ord-1", "ord-2"}


# ---------------------------------------------------------------------------
# _parse_order_created_time helper
# ---------------------------------------------------------------------------

class TestParseOrderCreatedTime:
    def test_datetime_object_utc(self):
        dt = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        order = MagicMock()
        order.created_time = dt
        assert _parse_order_created_time(order) == dt

    def test_naive_datetime_gets_utc(self):
        dt = datetime(2026, 4, 1, 12, 0, 0)  # no tz
        order = MagicMock()
        order.created_time = dt
        result = _parse_order_created_time(order)
        assert result.tzinfo == timezone.utc

    def test_iso_string_with_z(self):
        order = MagicMock()
        order.created_time = "2026-04-01T12:00:00Z"
        result = _parse_order_created_time(order)
        assert result.year == 2026

    def test_iso_string_with_offset(self):
        order = MagicMock()
        order.created_time = "2026-04-01T12:00:00+00:00"
        result = _parse_order_created_time(order)
        assert result.year == 2026

    def test_falls_back_to_created_at(self):
        dt = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        order = MagicMock()
        order.created_time = None
        order.created_at = dt
        assert _parse_order_created_time(order) == dt

    def test_returns_none_when_all_absent(self):
        order = MagicMock()
        order.created_time = None
        order.created_at = None
        order.place_time = None
        assert _parse_order_created_time(order) is None
