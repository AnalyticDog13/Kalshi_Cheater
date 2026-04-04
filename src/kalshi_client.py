"""
Kalshi API client wrapper.

Wraps the official kalshi-python SDK with:
  - Exponential backoff + jitter on 429 and 5xx errors
  - Per-request structured logging (method, path, status, latency)
  - Response caching with configurable TTL
  - Automatic cursor pagination for list_markets()
  - March 2026 API format helpers (dollar-string prices, fp quantities)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    value: Any
    expires_at: float  # unix timestamp


class _TTLCache:
    """Simple in-memory TTL cache. Not thread-safe — fine for single-threaded asyncio."""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._store[key] = _CacheEntry(value=value, expires_at=time.monotonic() + ttl_seconds)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class RetryableError(Exception):
    """Raised when the Kalshi API returns a retryable error (429, 5xx)."""
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _should_retry(exc: Exception) -> bool:
    return isinstance(exc, RetryableError) and exc.status_code in (429, 500, 502, 503, 504)


async def _with_retry(
    coro_fn,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> Any:
    """
    Execute an async callable with exponential backoff + full jitter.
    Retries only on RetryableError with a retryable status code.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries or not _should_retry(exc):
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = random.uniform(0, delay)
            logger.warning(
                "Kalshi API retryable error (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1, max_retries, exc, jitter,
            )
            await asyncio.sleep(jitter)
    raise last_exc  # unreachable but satisfies type checker


# ---------------------------------------------------------------------------
# Price / quantity helpers (March 2026 API format)
# ---------------------------------------------------------------------------

def parse_price(value: str | float | None, default: float = 0.0) -> float:
    """Parse a Kalshi dollar-string price to float. Returns default if None/empty."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        logger.warning("Could not parse price value: %r", value)
        return default


def parse_quantity(value: str | float | None, default: float = 0.0) -> float:
    """Parse a Kalshi fp-string quantity to float."""
    return parse_price(value, default)


def market_mid_price(market: Any) -> float:
    """Compute YES mid-price from a market object (post-March-2026 format)."""
    bid = parse_price(getattr(market, "yes_bid_dollars", None))
    ask = parse_price(getattr(market, "yes_ask_dollars", None), default=1.0)
    return (bid + ask) / 2.0


# ---------------------------------------------------------------------------
# Orderbook normaliser
# ---------------------------------------------------------------------------

@dataclass
class OrderbookSnapshot:
    """
    Flat, dollar-denominated view of the best bid/ask on each side.

    The Kalshi SDK returns a nested GetMarketOrderbookResponse with:
        .orderbook.var_true   → list[OrderbookLevel]  (YES bids, best first)
        .orderbook.var_false  → list[OrderbookLevel]  (NO  bids, best first)
        OrderbookLevel.price  → int cents (0-99) or float dollars for fractional markets
        OrderbookLevel.count  → int contracts at that level

    This dataclass normalises that into the flat string fields that
    order_executor.py and parse_price() / parse_quantity() already expect,
    exactly matching what a Kalshi market object looks like for the bid/ask
    fields (so no changes needed in order_executor.py or its tests).
    """
    yes_bid_dollars: str   # best YES bid in dollars, e.g. "0.6500"
    yes_ask_dollars: str   # best YES ask in dollars (= 1 - best NO bid), e.g. "0.6700"
    yes_bid_size_fp: str   # integer contracts available at best YES bid, e.g. "10"
    no_bid_dollars:  str   # best NO bid in dollars, e.g. "0.3500"
    no_bid_size_fp:  str   # integer contracts available at best NO bid, e.g. "8"


def _level_price_to_dollars(price) -> float:
    """
    Convert an OrderbookLevel.price to a 0–1 dollar float.

    Standard Kalshi markets send integer cents (e.g. 65 → $0.65).
    Fractional-trading-enabled markets may send a float already in the 0–1
    range.  The distinguishing rule: if the value is >= 1, treat it as cents.
    """
    if price is None:
        return 0.0
    val = float(price)
    return val / 100.0 if val >= 1.0 else val


def _normalize_orderbook(raw_response: Any) -> OrderbookSnapshot:
    """
    Convert a GetMarketOrderbookResponse (SDK object) to an OrderbookSnapshot.

    Handles None / empty book gracefully: missing sides default to the
    same sentinel values that order_executor.py already handles (bid=0,
    ask=1 → empty-book fallback pricing).
    """
    ob = getattr(raw_response, "orderbook", None)

    # YES bids are in var_true (alias "true"), sorted best (highest) first.
    yes_bids = (getattr(ob, "var_true", None) or []) if ob else []
    # NO bids are in var_false (alias "false"), sorted best (highest) first.
    no_bids  = (getattr(ob, "var_false", None) or []) if ob else []

    best_yes_bid = yes_bids[0] if yes_bids else None
    best_no_bid  = no_bids[0]  if no_bids  else None

    yes_bid_price = _level_price_to_dollars(getattr(best_yes_bid, "price", None)) if best_yes_bid else 0.0
    yes_bid_count = int(getattr(best_yes_bid, "count", 0) or 0)                   if best_yes_bid else 0

    no_bid_price  = _level_price_to_dollars(getattr(best_no_bid,  "price", None)) if best_no_bid  else 0.0
    no_bid_count  = int(getattr(best_no_bid,  "count", 0) or 0)                   if best_no_bid  else 0

    # YES ask = complement of best NO bid  (YES + NO always sum to $1 on Kalshi).
    # If there are no NO bids, the YES ask is unknown → default 1.0 (empty ask side).
    yes_ask_price = (1.0 - no_bid_price) if best_no_bid else 1.0

    return OrderbookSnapshot(
        yes_bid_dollars=f"{yes_bid_price:.4f}",
        yes_ask_dollars=f"{yes_ask_price:.4f}",
        yes_bid_size_fp=str(yes_bid_count),
        no_bid_dollars =f"{no_bid_price:.4f}",
        no_bid_size_fp =str(no_bid_count),
    )


# ---------------------------------------------------------------------------
# KalshiClient wrapper
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Authenticated Kalshi API client with retry, caching, and logging.

    Usage:
        client = KalshiClient.from_env(mode="demo")
        markets = await client.list_weather_markets(series_ticker="KXHIGHNY")
    """

    def __init__(
        self,
        mode: str = "demo",
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 60.0,
        cache_ttl_market: float = 30.0,
        cache_ttl_portfolio: float = 5.0,
        cache_ttl_orderbook: float = 10.0,
    ) -> None:
        self.mode = mode
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.cache_ttl_market = cache_ttl_market
        self.cache_ttl_portfolio = cache_ttl_portfolio
        self.cache_ttl_orderbook = cache_ttl_orderbook
        self._cache = _TTLCache()
        self._sdk: Any = None  # set by _connect()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, mode: str = "demo", **kwargs) -> "KalshiClient":
        """
        Create a KalshiClient reading credentials from environment variables.

        Required env vars (for demo):
            KALSHI_DEMO_API_KEY_ID
            KALSHI_DEMO_PRIVATE_KEY_PATH

        For live, replace DEMO with LIVE.
        """
        client = cls(mode=mode, **kwargs)
        client._connect()
        return client

    def _connect(self) -> None:
        """Initialise the underlying SDK client."""
        urls = {
            "demo": "https://demo-api.kalshi.co/trade-api/v2",
            "live": "https://api.elections.kalshi.com/trade-api/v2",
        }
        # Validate mode and env vars BEFORE importing SDK so tests can run without it.
        if self.mode not in urls:
            raise ValueError(f"mode must be 'demo' or 'live', got: {self.mode!r}")

        mode_upper = self.mode.upper()
        key_id_var = f"KALSHI_{mode_upper}_API_KEY_ID"
        key_path_var = f"KALSHI_{mode_upper}_PRIVATE_KEY_PATH"

        key_id = os.environ.get(key_id_var)
        key_path = os.environ.get(key_path_var)

        if not key_id:
            raise EnvironmentError(f"Missing env var: {key_id_var}")
        if not key_path:
            raise EnvironmentError(f"Missing env var: {key_path_var}")
        if not os.path.isfile(key_path):
            raise FileNotFoundError(f"Private key file not found: {key_path}")

        try:
            from kalshi_python import Configuration, KalshiClient as _SDK
        except ImportError as exc:
            raise ImportError(
                "kalshi-python is not installed. Run: pip install kalshi-python>=2.1.4"
            ) from exc

        config = Configuration(host=urls[self.mode])
        config.api_key_id = key_id
        with open(key_path, "r") as fh:
            config.private_key_pem = fh.read()

        self._sdk = _SDK(config)
        logger.info("Kalshi client connected (mode=%s)", self.mode)

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    async def _call(self, method_name: str, cache_key: str | None, ttl: float, **kwargs) -> Any:
        """
        Call an SDK method with retry, caching, and logging.

        Args:
            method_name: attribute path on self._sdk, e.g. "get_market"
            cache_key:   cache key, or None to skip caching
            ttl:         cache TTL in seconds
            **kwargs:    forwarded to the SDK method
        """
        if self._sdk is None:
            raise RuntimeError("KalshiClient not connected. Call _connect() first.")

        if cache_key:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit: %s", cache_key)
                return cached

        sdk_method = getattr(self._sdk, method_name)
        start = time.monotonic()

        async def _invoke():
            try:
                # The official SDK is synchronous — run in executor to avoid blocking.
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: sdk_method(**kwargs))
                return result
            except Exception as exc:
                # Map SDK exceptions to RetryableError where appropriate.
                msg = str(exc)
                status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
                if status in (429, 500, 502, 503, 504):
                    raise RetryableError(status, msg) from exc
                raise

        try:
            result = await _with_retry(
                _invoke,
                max_retries=self.max_retries,
                base_delay=self.retry_base_delay,
                max_delay=self.retry_max_delay,
            )
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            logger.debug("Kalshi API %-30s  %.0f ms", method_name, latency_ms)

        if cache_key and result is not None:
            self._cache.set(cache_key, result, ttl)

        return result

    # ------------------------------------------------------------------
    # Market methods
    # ------------------------------------------------------------------

    async def get_market(self, ticker: str) -> Any:
        """
        Fetch a single market by ticker.

        The SDK returns a GetMarketResponse wrapper; unwrap to the inner market
        dict so callers can access fields directly (e.g. market['status']).
        Falls back to the raw response if the inner attribute is absent.
        """
        raw = await self._call(
            "get_market",
            cache_key=f"market:{ticker}",
            ttl=self.cache_ttl_market,
            ticker=ticker,
        )
        inner = getattr(raw, "market", None)
        return inner if inner is not None else raw

    async def get_orderbook(self, ticker: str, depth: int = 10) -> OrderbookSnapshot:
        """
        Fetch orderbook for a market and return a normalised OrderbookSnapshot.

        The Kalshi SDK returns a nested GetMarketOrderbookResponse; this method
        flattens it into an OrderbookSnapshot with dollar-string fields that
        parse_price() and order_executor.py already understand.
        """
        raw = await self._call(
            "get_market_orderbook",
            cache_key=f"orderbook:{ticker}",
            ttl=self.cache_ttl_orderbook,
            ticker=ticker,
            depth=depth,
        )
        return _normalize_orderbook(raw)

    async def get_series(self, series_ticker: str) -> Any:
        """Fetch series metadata (includes settlement_sources, fee_type, fee_multiplier)."""
        return await self._call(
            "get_series_by_ticker",
            cache_key=f"series:{series_ticker}",
            ttl=600.0,  # series metadata rarely changes; cache 10 min
            ticker=series_ticker,
        )

    async def list_markets(
        self,
        series_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[Any]:
        """
        List markets with automatic cursor pagination.

        Kalshi returns max 1,000 per page. This method follows cursors until
        all pages are fetched (up to max_pages as a safety limit).

        Args:
            series_ticker: filter to a specific series (e.g. "KXHIGHNY")
            status: "open", "closed", "settled", "unopened"
            limit: page size (max 1000 per Kalshi docs)
            max_pages: safety cap on pagination loops
        """
        all_markets: list[Any] = []
        cursor: str | None = None
        pages = 0

        while pages < max_pages:
            kwargs: dict[str, Any] = {"status": status, "limit": min(limit, 1000)}
            if series_ticker:
                kwargs["series_ticker"] = series_ticker
            if cursor:
                kwargs["cursor"] = cursor

            # Don't cache paginated list calls — they're for discovery.
            response = await self._call(
                "get_markets",
                cache_key=None,
                ttl=0,
                **kwargs,
            )

            page_markets = getattr(response, "markets", []) or []
            all_markets.extend(page_markets)
            pages += 1

            cursor = getattr(response, "cursor", None)
            if not cursor or not page_markets:
                break

        logger.info(
            "list_markets(series=%s, status=%s) → %d markets (%d pages)",
            series_ticker, status, len(all_markets), pages,
        )
        return all_markets

    # ------------------------------------------------------------------
    # Portfolio methods
    # ------------------------------------------------------------------

    async def get_balance(self) -> Any:
        """Fetch portfolio balance."""
        return await self._call(
            "get_balance",
            cache_key="balance",
            ttl=self.cache_ttl_portfolio,
        )

    async def get_positions(self) -> list[Any]:
        """Fetch current open positions."""
        response = await self._call(
            "get_positions",
            cache_key="positions",
            ttl=self.cache_ttl_portfolio,
            limit=100,
        )
        # Try all known field names — SDK versions differ
        positions = (
            getattr(response, "positions", None)
            or getattr(response, "market_positions", None)
            or []
        )
        logger.warning(
            "get_positions: found %d positions | raw dump: %s",
            len(positions),
            response.model_dump() if response else "None",
        )
        return positions

    async def get_orders(self, status: str | None = "open") -> list[Any]:
        """
        Fetch orders filtered by status.

        Pass status=None to fetch ALL orders regardless of status — useful
        for _sync_fills() which needs to inspect order.status directly rather
        than relying on Kalshi using specific status string values (which have
        varied across SDK versions: "filled" vs "executed", "cancelled" vs
        "canceled").  When status is None the SDK call is made without the
        status kwarg so the server applies its own default (typically all
        non-expired orders).
        """
        kwargs: dict[str, Any] = {}
        if status is not None:
            kwargs["status"] = status
        cache_key = f"orders:{status if status is not None else 'all'}"
        response = await self._call(
            "get_orders",
            cache_key=cache_key,
            ttl=self.cache_ttl_portfolio,
            **kwargs,
        )
        return getattr(response, "orders", []) or []

    # ------------------------------------------------------------------
    # Order execution methods — these MUST invalidate cache
    # ------------------------------------------------------------------

    async def create_order(
        self,
        ticker: str,
        side: str,
        order_type: str,
        price: float,
        count: int | float,
    ) -> Any:
        """
        Place a limit order.

        Args:
            ticker:     market ticker
            side:       "yes" or "no"
            order_type: "limit" (always — never market orders)
            price:      order price in dollars (0.01 – 0.99)
            count:      number of contracts (int, or float for fractional markets)
        """
        if order_type != "limit":
            raise ValueError("Only limit orders are permitted by system policy.")
        if not (0.01 <= price <= 0.99):
            raise ValueError(f"Order price out of range: {price}")

        # Format price as two-decimal string for the API
        price_str = f"{price:.2f}"

        # The SDK's create_order signature varies — build a params dict and pass via kwargs.
        # The spec notes fractional trading may be enabled on some markets.
        params: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "type": order_type,
            "yes_price": int(round(price * 100)),  # API uses cents integer in some endpoints
            "count": count,
            "action": "buy",
        }

        logger.info(
            "Placing %s order: ticker=%s side=%s price=%.2f count=%s",
            order_type, ticker, side, price, count,
        )

        raw = await self._call(
            "create_order",
            cache_key=None,
            ttl=0,
            **params,
        )

        # Invalidate position/order/balance caches after any order.
        for key in ("positions", "orders:open", "balance"):
            self._cache.invalidate(key)

        # CreateOrderResponse wraps the actual Order inside a .order attribute.
        # Unwrap so callers can access order_id, status, etc. directly.
        inner = getattr(raw, "order", None)
        return inner if inner is not None else raw

    async def get_order(self, order_id: str) -> Any:
        """
        Fetch a single order by ID (GET /portfolio/orders/{order_id}).

        The list endpoint (get_orders) returns minimal Order objects — price
        fields are often None on executed orders.  The single-order endpoint
        returns full details including yes_price / no_price.

        Not cached — only called when the list endpoint failed to provide
        a fill price, so we always want fresh data.
        """
        raw = await self._call(
            "get_order",
            cache_key=None,
            ttl=0,
            order_id=order_id,
        )
        # GetOrderResponse wraps the actual Order inside a .order attribute.
        inner = getattr(raw, "order", None)
        return inner if inner is not None else raw

    async def cancel_order(self, order_id: str) -> Any:
        """Cancel an open order."""
        logger.info("Cancelling order: %s", order_id)
        result = await self._call(
            "cancel_order",
            cache_key=None,
            ttl=0,
            order_id=order_id,
        )
        self._cache.invalidate("orders:open")
        self._cache.invalidate("positions")
        return result

    # ------------------------------------------------------------------
    # Historical data (for backtesting / calibration)
    # ------------------------------------------------------------------

    async def list_historical_markets(
        self,
        series_ticker: str | None = None,
        limit: int = 200,
        max_pages: int = 50,
    ) -> list[Any]:
        """
        List settled markets for backtesting.
        Uses the /historical/markets endpoint with cursor pagination.
        """
        all_markets: list[Any] = []
        cursor: str | None = None
        pages = 0

        while pages < max_pages:
            kwargs: dict[str, Any] = {"limit": min(limit, 1000)}
            if series_ticker:
                kwargs["series_ticker"] = series_ticker
            if cursor:
                kwargs["cursor"] = cursor

            response = await self._call(
                "get_historical_markets",
                cache_key=None,
                ttl=0,
                **kwargs,
            )

            page_markets = getattr(response, "markets", []) or []
            all_markets.extend(page_markets)
            pages += 1

            cursor = getattr(response, "cursor", None)
            if not cursor or not page_markets:
                break

        logger.info(
            "list_historical_markets(series=%s) → %d markets",
            series_ticker, len(all_markets),
        )
        return all_markets

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Wipe all cached responses."""
        self._cache.clear()
        logger.debug("Cache cleared")

    def is_connected(self) -> bool:
        return self._sdk is not None
