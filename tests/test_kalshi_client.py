"""
Tests for src/kalshi_client.py

These tests use mocks exclusively — no real Kalshi API calls are made.
Run with: pytest tests/test_kalshi_client.py -v
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.kalshi_client import (
    KalshiClient,
    RetryableError,
    _TTLCache,
    _with_retry,
    market_mid_price,
    parse_price,
    parse_quantity,
)


# ---------------------------------------------------------------------------
# _TTLCache
# ---------------------------------------------------------------------------

class TestTTLCache:
    def test_miss_on_empty(self):
        cache = _TTLCache()
        assert cache.get("foo") is None

    def test_set_and_get(self):
        cache = _TTLCache()
        cache.set("k", "v", ttl_seconds=60)
        assert cache.get("k") == "v"

    def test_expires(self):
        cache = _TTLCache()
        cache.set("k", "v", ttl_seconds=0.01)
        time.sleep(0.05)
        assert cache.get("k") is None

    def test_invalidate(self):
        cache = _TTLCache()
        cache.set("k", "v", ttl_seconds=60)
        cache.invalidate("k")
        assert cache.get("k") is None

    def test_clear(self):
        cache = _TTLCache()
        cache.set("a", 1, 60)
        cache.set("b", 2, 60)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_invalidate_missing_key_is_noop(self):
        cache = _TTLCache()
        cache.invalidate("nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# parse_price / parse_quantity
# ---------------------------------------------------------------------------

class TestParsers:
    def test_parse_string(self):
        assert parse_price("0.5600") == pytest.approx(0.56)

    def test_parse_float(self):
        assert parse_price(0.40) == pytest.approx(0.40)

    def test_parse_none_returns_default(self):
        assert parse_price(None) == pytest.approx(0.0)
        assert parse_price(None, default=0.5) == pytest.approx(0.5)

    def test_parse_empty_string_returns_default(self):
        assert parse_price("") == pytest.approx(0.0)

    def test_parse_invalid_string_returns_default(self):
        assert parse_price("N/A") == pytest.approx(0.0)

    def test_parse_quantity_alias(self):
        assert parse_quantity("10.00") == pytest.approx(10.0)


class TestMarketMidPrice:
    def _mock_market(self, bid: str, ask: str) -> MagicMock:
        m = MagicMock()
        m.yes_bid_dollars = bid
        m.yes_ask_dollars = ask
        return m

    def test_mid_price_normal(self):
        m = self._mock_market("0.40", "0.60")
        assert market_mid_price(m) == pytest.approx(0.50)

    def test_mid_price_no_bid(self):
        m = self._mock_market("", "0.60")
        # bid parses to 0.0; ask to 0.60; mid = 0.30
        assert market_mid_price(m) == pytest.approx(0.30)

    def test_mid_price_no_ask(self):
        m = MagicMock(spec=[])  # no yes_ask_dollars attr
        m.yes_bid_dollars = "0.40"
        # ask falls back to default 1.0; mid = (0.40 + 1.0) / 2 = 0.70
        assert market_mid_price(m) == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# _with_retry
# ---------------------------------------------------------------------------

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        mock = AsyncMock(return_value="ok")
        result = await _with_retry(mock, max_retries=3, base_delay=0)
        assert result == "ok"
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RetryableError(429, "rate limited")
            return "done"

        result = await _with_retry(flaky, max_retries=3, base_delay=0, max_delay=0)
        assert result == "done"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        async def always_fail():
            raise RetryableError(503, "unavailable")

        with pytest.raises(RetryableError):
            await _with_retry(always_fail, max_retries=2, base_delay=0, max_delay=0)

    @pytest.mark.asyncio
    async def test_non_retryable_error_not_retried(self):
        calls = []

        async def bad():
            calls.append(1)
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await _with_retry(bad, max_retries=3, base_delay=0)

        assert len(calls) == 1  # only tried once


# ---------------------------------------------------------------------------
# KalshiClient — unit tests with mocked SDK
# ---------------------------------------------------------------------------

def _make_client(mode: str = "demo") -> KalshiClient:
    """Build a KalshiClient with a mocked SDK (no env vars needed)."""
    client = KalshiClient(mode=mode)
    client._sdk = MagicMock()  # replace real SDK with mock
    return client


class TestKalshiClientCaching:
    @pytest.mark.asyncio
    async def test_get_market_caches_response(self):
        client = _make_client()
        mock_market = MagicMock()
        mock_market.ticker = "KXHIGHNY-23DEC25-T50"
        client._sdk.get_market = MagicMock(return_value=mock_market)

        # First call — hits SDK
        r1 = await client.get_market("KXHIGHNY-23DEC25-T50")
        # Second call — should be cached
        r2 = await client.get_market("KXHIGHNY-23DEC25-T50")

        assert r1 is r2
        assert client._sdk.get_market.call_count == 1

    @pytest.mark.asyncio
    async def test_clear_cache_forces_refetch(self):
        client = _make_client()
        mock_market = MagicMock()
        client._sdk.get_market = MagicMock(return_value=mock_market)

        await client.get_market("TICKER")
        client.clear_cache()
        await client.get_market("TICKER")

        assert client._sdk.get_market.call_count == 2


class TestKalshiClientOrders:
    @pytest.mark.asyncio
    async def test_create_order_only_allows_limit(self):
        client = _make_client()
        with pytest.raises(ValueError, match="limit"):
            await client.create_order("TICKER", "yes", "market", price=0.50, count=1)

    @pytest.mark.asyncio
    async def test_create_order_rejects_extreme_price(self):
        client = _make_client()
        with pytest.raises(ValueError, match="out of range"):
            await client.create_order("TICKER", "yes", "limit", price=0.005, count=1)

    @pytest.mark.asyncio
    async def test_create_order_invalidates_cache(self):
        client = _make_client()
        client._cache.set("positions", ["pos1"], ttl_seconds=60)
        client._cache.set("orders:open", ["ord1"], ttl_seconds=60)
        client._cache.set("balance", {"balance": 1000}, ttl_seconds=60)

        mock_order = MagicMock()
        client._sdk.create_order = MagicMock(return_value=mock_order)

        await client.create_order("TICKER", "yes", "limit", price=0.50, count=1)

        assert client._cache.get("positions") is None
        assert client._cache.get("orders:open") is None
        assert client._cache.get("balance") is None

    @pytest.mark.asyncio
    async def test_cancel_order_invalidates_open_orders_cache(self):
        client = _make_client()
        client._cache.set("orders:open", ["ord1"], ttl_seconds=60)
        client._sdk.cancel_order = MagicMock(return_value=MagicMock())

        await client.cancel_order("order-123")

        assert client._cache.get("orders:open") is None


class TestKalshiClientPagination:
    @pytest.mark.asyncio
    async def test_list_markets_follows_cursor(self):
        client = _make_client()

        page1 = MagicMock()
        page1.markets = [MagicMock(), MagicMock()]
        page1.cursor = "cursor-abc"

        page2 = MagicMock()
        page2.markets = [MagicMock()]
        page2.cursor = None  # last page

        client._sdk.get_markets = MagicMock(side_effect=[page1, page2])

        markets = await client.list_markets(series_ticker="KXHIGHNY")
        assert len(markets) == 3
        assert client._sdk.get_markets.call_count == 2

    @pytest.mark.asyncio
    async def test_list_markets_respects_max_pages(self):
        client = _make_client()

        def always_has_cursor(**kwargs):
            page = MagicMock()
            page.markets = [MagicMock()]
            page.cursor = "always-more"
            return page

        client._sdk.get_markets = MagicMock(side_effect=always_has_cursor)

        markets = await client.list_markets(max_pages=3)
        # Should stop after 3 pages even though cursor never disappears
        assert client._sdk.get_markets.call_count == 3


class TestKalshiClientConnectionGuard:
    @pytest.mark.asyncio
    async def test_raises_if_not_connected(self):
        client = KalshiClient()  # _sdk is None
        with pytest.raises(RuntimeError, match="not connected"):
            await client.get_market("TICKER")

    def test_is_connected_false_before_connect(self):
        client = KalshiClient()
        assert client.is_connected() is False

    def test_is_connected_true_after_mock_connect(self):
        client = _make_client()
        assert client.is_connected() is True


class TestKalshiClientFromEnv:
    def test_from_env_raises_on_missing_key_id(self, monkeypatch):
        monkeypatch.delenv("KALSHI_DEMO_API_KEY_ID", raising=False)
        monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY_PATH", raising=False)

        with pytest.raises(EnvironmentError, match="KALSHI_DEMO_API_KEY_ID"):
            KalshiClient.from_env(mode="demo")

    def test_from_env_raises_on_missing_key_path(self, monkeypatch):
        monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "test-key-id")
        monkeypatch.delenv("KALSHI_DEMO_PRIVATE_KEY_PATH", raising=False)

        with pytest.raises(EnvironmentError, match="KALSHI_DEMO_PRIVATE_KEY_PATH"):
            KalshiClient.from_env(mode="demo")

    def test_from_env_raises_on_missing_pem_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "test-key-id")
        monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY_PATH", str(tmp_path / "nonexistent.pem"))

        with pytest.raises(FileNotFoundError):
            KalshiClient.from_env(mode="demo")

    def test_from_env_invalid_mode(self, monkeypatch, tmp_path):
        pem = tmp_path / "key.pem"
        pem.write_text("fake-pem")
        monkeypatch.setenv("KALSHI_DEMO_API_KEY_ID", "test-key-id")
        monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY_PATH", str(pem))

        with pytest.raises(ValueError, match="mode must be"):
            # Pass a valid env but invalid mode
            client = KalshiClient(mode="invalid")
            client._connect()
