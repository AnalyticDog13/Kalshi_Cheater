"""
Notifier.

Sends Telegram messages for key trading events. Designed to be completely
non-blocking: every method catches all exceptions and logs them — a Telegram
failure must never interrupt the trading loop.

Modes (from settings.yaml notifications.method):
  "telegram"  — live HTTP calls to Telegram Bot API
  "log_only"  — skip HTTP, log message text at INFO level (used in tests
                and when no Telegram credentials are configured)

Message philosophy (thinking like a trader monitoring a live system):
  - Trade placed:   need ticker, direction, price, size, edge — did we get in?
  - Trade outcome:  need ticker, win/loss, P&L — did we make money?
  - Daily summary:  need total P&L, Brier score, open position count — are we on track?
  - Alert:          free-form — circuit breakers, budget exhaustion, anomalies
  - Circuit breaker: which service failed, why — is infra healthy?

All messages are plain text (no Markdown) to avoid Telegram parse_mode errors
from special characters in tickers or error messages.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from src.order_executor import OrderResult

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
REQUEST_TIMEOUT = 10.0  # seconds — don't let a slow Telegram block the loop


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class NotifierConfig:
    """
    Loaded from settings.yaml notifications section plus environment variables.
    """
    method: str = "log_only"          # "telegram" or "log_only"
    bot_token: str = ""               # from TELEGRAM_BOT_TOKEN env var
    chat_id: str = ""                 # from TELEGRAM_CHAT_ID env var

    @classmethod
    def from_settings(cls, settings: dict) -> "NotifierConfig":
        notif = settings.get("notifications", {})
        return cls(
            method=notif.get("method", "log_only"),
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    @property
    def is_configured(self) -> bool:
        """True only if telegram mode and credentials are present."""
        return self.method == "telegram" and bool(self.bot_token) and bool(self.chat_id)


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class Notifier:
    """
    Async Telegram notifier. All public methods are fire-and-forget safe:
    they catch every exception and return None without raising.

    Inject via dependency injection from the daemon. When notifications are
    not needed (e.g. unit tests of other modules), pass notifier=None and
    check before calling.
    """

    def __init__(self, config: NotifierConfig | None = None) -> None:
        self.config = config or NotifierConfig()
        # Tracks the highest processed update_id so getUpdates only returns new messages
        self._update_offset: int = 0

    @classmethod
    def from_settings(cls, settings: dict) -> "Notifier":
        return cls(config=NotifierConfig.from_settings(settings))

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    async def send_trade_placed(self, order_result: OrderResult) -> None:
        """
        Notify when a limit order is successfully submitted to Kalshi.

        Includes: ticker, direction (YES/NO), limit price, contract count,
        edge at placement, and the order ID for reference.
        """
        msg = (
            f"[TRADE PLACED]\n"
            f"Ticker:    {order_result.ticker}\n"
            f"Direction: {order_result.direction}\n"
            f"Side:      {order_result.side.upper()}\n"
            f"Price:     ${order_result.price:.2f}\n"
            f"Count:     {order_result.count}\n"
            f"Edge:      {order_result.edge_at_placement:.1%}\n"
            f"Order ID:  {order_result.order_id or 'N/A'}"
        )
        await self._send(msg)

    async def send_trade_outcome(
        self,
        ticker: str,
        outcome: int,
        pnl: float,
    ) -> None:
        """
        Notify when a market resolves and P&L is known.

        outcome: 1 = condition was TRUE (e.g. temp exceeded threshold), 0 = FALSE
        pnl:     net profit/loss in dollars after fees
        """
        result_str = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"[TRADE OUTCOME] {result_str}\n"
            f"Ticker:  {ticker}\n"
            f"Outcome: {'YES resolved' if outcome == 1 else 'NO resolved'}\n"
            f"P&L:     {sign}${pnl:.2f}"
        )
        await self._send(msg)

    async def send_daily_summary(
        self,
        daily_pnl: float,
        brier_score: float | None,
        open_positions: int,
    ) -> None:
        """
        End-of-day summary: total P&L, calibration quality, portfolio state.

        brier_score: current rolling Brier score (None if insufficient data)
        open_positions: count of filled but unresolved contracts
        """
        sign = "+" if daily_pnl >= 0 else ""
        brier_str = f"{brier_score:.4f}" if brier_score is not None else "N/A (insufficient data)"
        msg = (
            f"[DAILY SUMMARY]\n"
            f"P&L today:      {sign}${daily_pnl:.2f}\n"
            f"Brier score:    {brier_str}\n"
            f"Open positions: {open_positions}"
        )
        await self._send(msg)

    async def send_alert(self, message: str) -> None:
        """
        Free-form alert for anomalies: budget exhaustion, degraded metrics,
        unexpected API responses, or operator-triggered messages.
        """
        msg = f"[ALERT]\n{message}"
        await self._send(msg)

    async def send_circuit_breaker(self, service: str, reason: str) -> None:
        """
        Notify when a circuit breaker trips (3 consecutive failures → 5-min pause).

        service: "kalshi_api", "open_meteo", "noaa", etc.
        reason:  last error message or exception summary
        """
        msg = (
            f"[CIRCUIT BREAKER TRIPPED]\n"
            f"Service: {service}\n"
            f"Reason:  {reason}\n"
            f"Action:  Trading paused for 5 minutes. Will retry automatically."
        )
        await self._send(msg)

    # ------------------------------------------------------------------
    # Remote command polling
    # ------------------------------------------------------------------

    async def poll_commands(self) -> list[str]:
        """
        Poll Telegram for new incoming commands from the configured chat.

        Uses getUpdates with offset tracking so each update is only returned
        once. Non-blocking (timeout=0). Security: messages from any chat_id
        other than the configured one are silently ignored.

        Supported commands: /stop, /pause, /resume

        Returns:
            List of command strings received since the last poll, e.g. ["/stop"].
            Returns [] on any error or when not configured.

        Never raises.
        """
        if not self.config.is_configured:
            return []

        try:
            url = TELEGRAM_UPDATES_URL.format(token=self.config.bot_token)
            params = {"offset": self._update_offset, "timeout": 0, "limit": 10}
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()

            data = resp.json()
            if not data.get("ok"):
                logger.warning("getUpdates returned ok=false: %s", data)
                return []

            commands: list[str] = []
            for update in data.get("result", []):
                # Advance offset so this update is not returned again
                self._update_offset = update["update_id"] + 1

                msg = update.get("message") or update.get("edited_message") or {}
                sender_chat_id = str(msg.get("chat", {}).get("id", ""))

                # Security: only accept messages from the configured chat
                if sender_chat_id != str(self.config.chat_id):
                    logger.debug(
                        "Ignoring Telegram message from unknown chat_id %s", sender_chat_id
                    )
                    continue

                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    cmd = text.split()[0].lower()
                    commands.append(cmd)
                    logger.info("Telegram command received: %s", cmd)

            return commands

        except httpx.TimeoutException:
            logger.warning("poll_commands timed out after %.0fs", REQUEST_TIMEOUT)
            return []
        except Exception as exc:
            logger.warning("poll_commands failed (%s: %s)", type(exc).__name__, exc)
            return []

    # ------------------------------------------------------------------
    # Internal send
    # ------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        """
        Dispatch a message. Logs the text regardless of mode, then sends
        via Telegram if configured.

        Never raises — all exceptions are caught and logged as warnings.
        """
        logger.info("Notifier: %s", text.replace("\n", " | "))

        if not self.config.is_configured:
            # log_only mode or missing credentials — already logged above
            return

        try:
            url = TELEGRAM_API_URL.format(token=self.config.bot_token)
            payload = {
                "chat_id": self.config.chat_id,
                "text": text,
            }
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Telegram API returned %d: %s",
                exc.response.status_code, exc.response.text[:200],
            )
        except httpx.TimeoutException:
            logger.warning("Telegram request timed out after %.0fs", REQUEST_TIMEOUT)
        except Exception as exc:
            logger.warning(
                "Telegram send failed (%s: %s)",
                type(exc).__name__, exc,
            )
