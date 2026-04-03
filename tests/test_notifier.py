"""
Tests for src/notifier.py.

All Telegram HTTP calls are mocked via unittest.mock — no real network traffic.
"""

from __future__ import annotations

import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.notifier import Notifier, NotifierConfig, TELEGRAM_API_URL, TELEGRAM_UPDATES_URL
from src.order_executor import OrderResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _log_only_notifier() -> Notifier:
    return Notifier(config=NotifierConfig(method="log_only"))


def _telegram_notifier() -> Notifier:
    return Notifier(config=NotifierConfig(
        method="telegram",
        bot_token="test-token",
        chat_id="123456",
    ))


def _order_result(
    ticker="KXHIGHNY-26APR01-T79",
    direction="BUY_YES",
    side="yes",
    price=0.55,
    count=5,
    order_id="ord-abc123",
    edge=0.12,
    fresh_mid=0.54,
) -> OrderResult:
    return OrderResult(
        ticker=ticker,
        direction=direction,
        side=side,
        price=price,
        count=count,
        order_id=order_id,
        was_placed=True,
        skip_reason=None,
        edge_at_placement=edge,
        fresh_mid_price=fresh_mid,
    )


def _mock_async_client(status_code=200):
    """Return a context manager mock for httpx.AsyncClient."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


# ---------------------------------------------------------------------------
# NotifierConfig
# ---------------------------------------------------------------------------

class TestNotifierConfig:
    def test_defaults_to_log_only(self):
        config = NotifierConfig()
        assert config.method == "log_only"

    def test_is_configured_false_when_log_only(self):
        config = NotifierConfig(method="log_only", bot_token="tok", chat_id="123")
        assert config.is_configured is False

    def test_is_configured_false_when_missing_token(self):
        config = NotifierConfig(method="telegram", bot_token="", chat_id="123")
        assert config.is_configured is False

    def test_is_configured_false_when_missing_chat_id(self):
        config = NotifierConfig(method="telegram", bot_token="tok", chat_id="")
        assert config.is_configured is False

    def test_is_configured_true_when_all_set(self):
        config = NotifierConfig(method="telegram", bot_token="tok", chat_id="123")
        assert config.is_configured is True

    def test_from_settings_reads_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mytoken")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "99999")
        settings = {"notifications": {"method": "telegram"}}
        config = NotifierConfig.from_settings(settings)
        assert config.method == "telegram"
        assert config.bot_token == "mytoken"
        assert config.chat_id == "99999"

    def test_from_settings_defaults_log_only(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        config = NotifierConfig.from_settings({})
        assert config.method == "log_only"

    def test_from_settings_missing_env_gives_empty_strings(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        config = NotifierConfig.from_settings({"notifications": {"method": "telegram"}})
        assert config.bot_token == ""
        assert config.chat_id == ""
        assert config.is_configured is False


# ---------------------------------------------------------------------------
# log_only mode — no HTTP calls, just logging
# ---------------------------------------------------------------------------

class TestLogOnlyMode:
    def test_send_trade_placed_does_not_call_http(self):
        n = _log_only_notifier()
        with patch("src.notifier.httpx.AsyncClient") as mock_cls:
            _run(n.send_trade_placed(_order_result()))
            mock_cls.assert_not_called()

    def test_send_trade_outcome_does_not_call_http(self):
        n = _log_only_notifier()
        with patch("src.notifier.httpx.AsyncClient") as mock_cls:
            _run(n.send_trade_outcome("TICKER", 1, 2.50))
            mock_cls.assert_not_called()

    def test_send_daily_summary_does_not_call_http(self):
        n = _log_only_notifier()
        with patch("src.notifier.httpx.AsyncClient") as mock_cls:
            _run(n.send_daily_summary(5.25, 0.15, 3))
            mock_cls.assert_not_called()

    def test_send_alert_does_not_call_http(self):
        n = _log_only_notifier()
        with patch("src.notifier.httpx.AsyncClient") as mock_cls:
            _run(n.send_alert("Something odd happened"))
            mock_cls.assert_not_called()

    def test_send_circuit_breaker_does_not_call_http(self):
        n = _log_only_notifier()
        with patch("src.notifier.httpx.AsyncClient") as mock_cls:
            _run(n.send_circuit_breaker("kalshi_api", "ConnectionError"))
            mock_cls.assert_not_called()

    def test_send_trade_placed_logs_message(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_placed(_order_result(ticker="KXHIGHNY-26APR01-T79")))
        assert "KXHIGHNY-26APR01-T79" in caplog.text

    def test_send_trade_outcome_logs_win(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_outcome("TICKER", 1, 3.50))
        assert "WIN" in caplog.text
        assert "3.50" in caplog.text

    def test_send_trade_outcome_logs_loss(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_outcome("TICKER", 0, -1.20))
        assert "LOSS" in caplog.text

    def test_send_daily_summary_logs_pnl(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_daily_summary(12.50, 0.14, 2))
        assert "12.50" in caplog.text
        assert "0.1400" in caplog.text

    def test_send_daily_summary_none_brier(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_daily_summary(0.0, None, 0))
        assert "N/A" in caplog.text

    def test_send_circuit_breaker_logs_service(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_circuit_breaker("open_meteo", "Timeout after 30s"))
        assert "open_meteo" in caplog.text
        assert "Timeout" in caplog.text

    def test_send_alert_logs_message(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_alert("Daily loss limit reached"))
        assert "Daily loss limit reached" in caplog.text


# ---------------------------------------------------------------------------
# Telegram mode — HTTP calls made correctly
# ---------------------------------------------------------------------------

class TestTelegramMode:
    def test_send_trade_placed_posts_to_telegram(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_trade_placed(_order_result()))
        mock_client.post.assert_called_once()

    def test_correct_url_used(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_alert("test"))
        call_args = mock_client.post.call_args
        url = call_args[0][0]
        assert "test-token" in url
        assert "sendMessage" in url

    def test_correct_chat_id_in_payload(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_alert("test"))
        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["json"]["chat_id"] == "123456"

    def test_message_text_in_payload(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_alert("My special alert"))
        call_kwargs = mock_client.post.call_args[1]
        assert "My special alert" in call_kwargs["json"]["text"]

    def test_trade_placed_message_contains_key_fields(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_trade_placed(_order_result(
                ticker="KXHIGHNY-26APR01-T79",
                price=0.55,
                count=5,
                edge=0.12,
                order_id="ord-xyz",
            )))
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "KXHIGHNY-26APR01-T79" in text
        assert "0.55" in text
        assert "5" in text
        assert "ord-xyz" in text

    def test_outcome_win_message_contains_pnl(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_trade_outcome("TICKER", 1, 4.75))
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "WIN" in text
        assert "4.75" in text

    def test_circuit_breaker_message_contains_service(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client()
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_circuit_breaker("noaa", "503 Service Unavailable"))
        text = mock_client.post.call_args[1]["json"]["text"]
        assert "noaa" in text
        assert "503" in text


# ---------------------------------------------------------------------------
# Fail-silent behavior — errors must never propagate
# ---------------------------------------------------------------------------

class TestFailSilent:
    def test_http_error_does_not_raise(self):
        n = _telegram_notifier()
        mock_client = _mock_async_client(status_code=500)
        mock_client.post.return_value.raise_for_status.side_effect = (
            __import__("httpx").HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=MagicMock(status_code=500, text="Internal Server Error"),
            )
        )
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            # Must not raise
            _run(n.send_alert("test"))

    def test_timeout_does_not_raise(self):
        import httpx as _httpx
        n = _telegram_notifier()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_alert("test"))  # must not raise

    def test_network_error_does_not_raise(self):
        import httpx as _httpx
        n = _telegram_notifier()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_trade_placed(_order_result()))  # must not raise

    def test_generic_exception_does_not_raise(self):
        n = _telegram_notifier()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.send_circuit_breaker("kalshi_api", "boom"))  # must not raise

    def test_http_error_logs_warning(self, caplog):
        n = _telegram_notifier()
        mock_client = _mock_async_client(status_code=400)
        mock_client.post.return_value.raise_for_status.side_effect = (
            __import__("httpx").HTTPStatusError(
                "bad request",
                request=MagicMock(),
                response=MagicMock(status_code=400, text="Bad Request"),
            )
        )
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level(logging.WARNING, logger="src.notifier"):
                _run(n.send_alert("test"))
        assert "400" in caplog.text or "Telegram" in caplog.text

    def test_timeout_logs_warning(self, caplog):
        import httpx as _httpx
        n = _telegram_notifier()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level(logging.WARNING, logger="src.notifier"):
                _run(n.send_alert("test"))
        assert "timed out" in caplog.text.lower() or "timeout" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Message content correctness
# ---------------------------------------------------------------------------

class TestMessageContent:
    def test_negative_pnl_shows_minus_sign(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_outcome("TICKER", 0, -2.50))
        assert "-2.50" in caplog.text

    def test_positive_pnl_shows_plus_sign(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_daily_summary(3.75, 0.16, 1))
        assert "+$3.75" in caplog.text or "3.75" in caplog.text

    def test_zero_pnl_outcome_is_flat(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_outcome("TICKER", 1, 0.0))
        assert "FLAT" in caplog.text

    def test_buy_no_direction_in_trade_placed(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_trade_placed(_order_result(direction="BUY_NO", side="no")))
        assert "BUY_NO" in caplog.text or "NO" in caplog.text

    def test_open_positions_count_in_daily_summary(self, caplog):
        n = _log_only_notifier()
        with caplog.at_level(logging.INFO, logger="src.notifier"):
            _run(n.send_daily_summary(-1.0, 0.20, 7))
        assert "7" in caplog.text


# ---------------------------------------------------------------------------
# poll_commands — remote command polling
# ---------------------------------------------------------------------------

def _mock_updates_client(updates: list[dict], ok: bool = True):
    """Return a mock httpx.AsyncClient that returns a getUpdates-style response."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"ok": ok, "result": updates})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _make_update(update_id: int, chat_id: str, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


class TestPollCommands:
    def test_log_only_returns_empty(self):
        n = _log_only_notifier()
        result = _run(n.poll_commands())
        assert result == []

    def test_unconfigured_returns_empty(self):
        n = Notifier(config=NotifierConfig(method="telegram", bot_token="", chat_id=""))
        result = _run(n.poll_commands())
        assert result == []

    def test_returns_stop_command(self):
        n = _telegram_notifier()
        updates = [_make_update(1, "123456", "/stop")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == ["/stop"]

    def test_returns_pause_command(self):
        n = _telegram_notifier()
        updates = [_make_update(1, "123456", "/pause")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == ["/pause"]

    def test_returns_resume_command(self):
        n = _telegram_notifier()
        updates = [_make_update(1, "123456", "/resume")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == ["/resume"]

    def test_ignores_wrong_chat_id(self):
        n = _telegram_notifier()  # configured for chat_id "123456"
        updates = [_make_update(1, "999999", "/stop")]  # from a different chat
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == []

    def test_ignores_non_command_messages(self):
        n = _telegram_notifier()
        updates = [_make_update(1, "123456", "hello there")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == []

    def test_advances_offset_after_poll(self):
        n = _telegram_notifier()
        assert n._update_offset == 0
        updates = [_make_update(42, "123456", "/pause")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.poll_commands())
        assert n._update_offset == 43  # update_id + 1

    def test_empty_result_returns_empty_list(self):
        n = _telegram_notifier()
        mock_client = _mock_updates_client([])
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == []

    def test_network_error_returns_empty_not_raises(self):
        import httpx as _httpx
        n = _telegram_notifier()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == []

    def test_ok_false_returns_empty(self):
        n = _telegram_notifier()
        mock_client = _mock_updates_client([], ok=False)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == []

    def test_multiple_commands_returned(self):
        n = _telegram_notifier()
        updates = [
            _make_update(10, "123456", "/pause"),
            _make_update(11, "123456", "/resume"),
        ]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == ["/pause", "/resume"]
        assert n._update_offset == 12

    def test_command_normalized_to_lowercase(self):
        n = _telegram_notifier()
        updates = [_make_update(1, "123456", "/STOP")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            result = _run(n.poll_commands())
        assert result == ["/stop"]

    def test_offset_advances_even_for_wrong_chat(self):
        """Even ignored messages must advance the offset so they aren't re-fetched."""
        n = _telegram_notifier()
        updates = [_make_update(7, "999999", "/stop")]
        mock_client = _mock_updates_client(updates)
        with patch("src.notifier.httpx.AsyncClient", return_value=mock_client):
            _run(n.poll_commands())
        assert n._update_offset == 8
