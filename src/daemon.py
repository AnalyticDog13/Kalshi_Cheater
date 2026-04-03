"""
Trading Daemon.

Async event loop that orchestrates the full pipeline:
  scanner → edge → risk → executor → tracker → notifier

Key behaviors:
  - High-frequency scan windows 03:00-04:00 UTC and 15:00-16:00 UTC (2-min poll)
  - Default 5-min poll outside high-frequency windows
  - Graceful shutdown on SIGINT/SIGTERM: finish current cycle, stop loop
  - Startup position reconciliation with Kalshi API
  - Dual-arm: conservative arm executes real orders; exploratory arm paper-only
  - Circuit breakers on all external calls via HealthMonitor
  - Daily summary notification at first cycle after UTC midnight

No unit tests for this module — it is the integration orchestrator.
All logic is tested in individual component modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.kalshi_client import KalshiClient
from src.data_feeds.weather_ensemble import OpenMeteoEnsembleClient
from src.probability_engine import ProbabilityEngine, BiasDatabase
from src.market_scanner import MarketScanner, ScanResult
from src.edge_calculator import EdgeParams, compute_edge
from src.risk_manager import RiskManager, RiskLimits
from src.order_executor import OrderExecutor
from src.tracker import Tracker, TradeRecord
from src.calibration_engine import CalibrationEngine
from src.notifier import Notifier
from src.health_monitor import HealthMonitor
from src.ai_reasoner import AIReasoner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_settings(
    config_dir: str | Path = "./config",
) -> tuple[dict, dict, list[str]]:
    """
    Load all YAML config files from config_dir.

    Returns:
        (settings, station_mapping, series_tickers)
        - settings:        full settings.yaml dict
        - station_mapping: {series_ticker: {station_id, latitude, longitude, city, ...}}
        - series_tickers:  list of Kalshi series tickers to scan
    """
    config_dir = Path(config_dir)

    with open(config_dir / "settings.yaml") as f:
        settings: dict = yaml.safe_load(f)

    with open(config_dir / "station_mapping.yaml") as f:
        station_data: dict = yaml.safe_load(f)
    station_mapping: dict = station_data.get("stations", {})

    with open(config_dir / "market_categories.yaml") as f:
        categories: dict = yaml.safe_load(f)
    series_tickers: list[str] = (
        categories.get("weather_series", {}).get("temperature", [])
    )

    return settings, station_mapping, series_tickers


# ---------------------------------------------------------------------------
# Main daemon class
# ---------------------------------------------------------------------------

class TradingDaemon:
    """
    Autonomous trading daemon. Runs until stopped by signal or error.

    Typical usage (from main.py or CLI):
        daemon = TradingDaemon.from_config()
        asyncio.run(daemon.run())

    The constructor accepts pre-loaded config dicts so components can be
    injected for integration testing without hitting live APIs.
    """

    def __init__(
        self,
        settings: dict,
        station_mapping: dict,
        series_tickers: list[str],
        db_path: str = "./data/trades.db",
        calibration_db_path: str = "./data/calibration.db",
        bias_db_path: str = "./data/station_bias.db",
        pause_flag_path: str = "./data/daemon.pause",
    ) -> None:
        self._settings = settings
        self._station_mapping = station_mapping
        self._series_tickers = series_tickers

        # Shutdown coordination — set by signal handler; main loop checks it
        self._stop_event = asyncio.Event()

        # Daily summary tracking — send once per UTC day
        self._last_summary_date: date | None = None

        # Report tracking — generate file reports at midnight UTC
        self._last_report_date: date | None = None

        # Pause flag path — set by CLI `pause` command; checked each cycle
        self._pause_flag = Path(pause_flag_path)

        self._build_components(db_path, calibration_db_path, bias_db_path)

    @classmethod
    def from_config(
        cls,
        config_dir: str | Path = "./config",
        db_path: str = "./data/trades.db",
        calibration_db_path: str = "./data/calibration.db",
        bias_db_path: str = "./data/station_bias.db",
        pause_flag_path: str = "./data/daemon.pause",
    ) -> "TradingDaemon":
        """Create daemon from the config directory. Standard production usage."""
        settings, station_mapping, series_tickers = load_settings(config_dir)
        return cls(
            settings=settings,
            station_mapping=station_mapping,
            series_tickers=series_tickers,
            db_path=db_path,
            calibration_db_path=calibration_db_path,
            bias_db_path=bias_db_path,
            pause_flag_path=pause_flag_path,
        )

    # ------------------------------------------------------------------
    # Component construction
    # ------------------------------------------------------------------

    def _build_components(
        self,
        db_path: str,
        calibration_db_path: str,
        bias_db_path: str,
    ) -> None:
        """Construct all trading components from settings."""
        s = self._settings
        api_cfg = s.get("api", {})
        ens_cfg = s.get("ensemble", {})
        mode = s.get("trading_mode", "demo")

        # --- Kalshi client ---------------------------------------------------
        self.kalshi_client = KalshiClient.from_env(
            mode=mode,
            max_retries=api_cfg.get("max_retries", 3),
            retry_base_delay=api_cfg.get("retry_base_delay_seconds", 1.0),
            retry_max_delay=api_cfg.get("retry_max_delay_seconds", 60.0),
            cache_ttl_market=api_cfg.get("cache_ttl_market_seconds", 30.0),
            cache_ttl_portfolio=api_cfg.get("cache_ttl_portfolio_seconds", 5.0),
        )

        # --- Ensemble + probability engine -----------------------------------
        self.ensemble_client = OpenMeteoEnsembleClient(
            models=ens_cfg.get("models"),
            cache_ttl_minutes=ens_cfg.get("cache_ttl_minutes", 15.0),
        )
        self.bias_db = BiasDatabase(db_path=bias_db_path)
        self.probability_engine = ProbabilityEngine(
            ensemble_client=self.ensemble_client,
            bias_db=self.bias_db,
        )

        # --- AI reasoner (optional — fail open if key absent) ----------------
        ai_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if ai_api_key:
            self.ai_reasoner: AIReasoner | None = AIReasoner.from_settings(
                s, api_key=ai_api_key
            )
        else:
            self.ai_reasoner = None
            logger.warning("ANTHROPIC_API_KEY not set — AI sanity checks disabled.")

        # --- Conservative arm (executes real orders) -------------------------
        self.conservative_params = EdgeParams.from_settings(s)
        self.conservative_limits = RiskLimits.from_settings(s)
        self.conservative_risk = RiskManager(limits=self.conservative_limits)
        self.conservative_scanner = MarketScanner(
            kalshi_client=self.kalshi_client,
            probability_engine=self.probability_engine,
            edge_params=self.conservative_params,
            risk_limits=self.conservative_limits,
            station_mapping=self._station_mapping,
            series_tickers=self._series_tickers,
            ai_reasoner=self.ai_reasoner,
        )

        # --- Exploratory arm (paper-only, never executes) --------------------
        # Shares the same kalshi_client and probability_engine so ensemble
        # cache is reused — no extra API calls.
        self.exploratory_params = _build_exploratory_params(s)
        self.exploratory_limits = _build_exploratory_limits(s)

        # --- Execution, tracking, notifications, health ----------------------
        self.order_executor = OrderExecutor(kalshi_client=self.kalshi_client)
        self.tracker = Tracker(db_path=db_path)
        self.calibration_engine = CalibrationEngine(db_path=calibration_db_path)
        self.notifier = Notifier.from_settings(s)
        self.health_monitor = HealthMonitor()

        logger.info(
            "TradingDaemon built: mode=%s, %d series, %d stations",
            mode,
            len(self._series_tickers),
            len(self._station_mapping),
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Start the trading loop. Runs until SIGINT/SIGTERM or stop() is called.

        Graceful shutdown: the current cycle completes before exiting.
        Open orders are NOT cancelled on shutdown — they persist on Kalshi
        and will be reconciled on the next startup.
        """
        self._install_signal_handlers()

        logger.info("TradingDaemon starting.")
        await self.notifier.send_alert("Trading daemon started.")

        # Sync positions from Kalshi API (source of truth) before first cycle
        await self._reconcile_positions()

        cycle = 0
        while not self._stop_event.is_set():
            cycle += 1
            try:
                await self._run_cycle(cycle)
            except Exception as exc:
                logger.exception("Unhandled error in cycle %d: %s", cycle, exc)
                await self.notifier.send_alert(f"Daemon error in cycle {cycle}: {exc}")
                # Brief pause to avoid a tight crash loop
                await asyncio.sleep(30)
                continue

            if self._stop_event.is_set():
                break

            interval = self._get_poll_interval_seconds()
            logger.debug("Sleeping %.0fs until next cycle.", interval)
            try:
                # Wait for the interval OR an early stop signal
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()), timeout=interval
                )
            except asyncio.TimeoutError:
                pass  # normal — interval elapsed

        logger.info("TradingDaemon stopped cleanly.")
        await self.notifier.send_alert("Trading daemon stopped.")

    def stop(self) -> None:
        """Signal graceful shutdown. Current cycle will complete first."""
        logger.info("stop() called — setting stop event.")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Core cycle
    # ------------------------------------------------------------------

    async def _run_cycle(self, cycle: int) -> None:
        """
        One full trading cycle:
          1. Cancel stale open orders
          2. Check for fills on pending orders
          3. Fetch portfolio state
          4. Build skip set (dedup + manual skip) — before scan to suppress AI calls
          5. Scan markets (AI calls suppressed for skip-set tickers)
          6. Execute approved trades (conservative arm)
          7. Send daily summary if UTC day just rolled over
        """
        now_utc = datetime.now(timezone.utc)
        logger.info(
            "=== Cycle %d @ %s ===",
            cycle,
            now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        # --- 1. Monitor open orders (cancel stale) -----------------------
        await self._monitor_orders()

        # --- 2. Sync fills on pending orders -----------------------------
        await self._sync_fills()

        # --- 2b. Sync resolutions — record outcome for settled markets ---
        await self._sync_resolutions()

        # --- 3. Fetch portfolio state ------------------------------------
        if self.health_monitor.is_open("kalshi_api"):
            logger.warning("kalshi_api circuit OPEN — skipping cycle.")
            return

        try:
            all_kalshi_orders = await self.kalshi_client.get_orders(status=None)
            _RESTING_STATUSES = frozenset({"resting", "open", "pending"})
            open_orders = [o for o in all_kalshi_orders if str(getattr(o, "status", "")).lower() in _RESTING_STATUSES]
            open_orders_count = len(open_orders)
            open_positions = await self.kalshi_client.get_positions()
            self.health_monitor.record_success("kalshi_api")
        except Exception as exc:
            open_orders_count = 0
            open_positions = []
            just_tripped = self.health_monitor.record_failure("kalshi_api", str(exc))
            if just_tripped:
                await self.notifier.send_circuit_breaker("kalshi_api", str(exc))
            return

        try:
            balance_obj = await self.kalshi_client.get_balance()
            portfolio_balance = _parse_balance(balance_obj)
        except Exception as exc:
            logger.warning(
                "Could not fetch balance (%s) — using fallback $30.", exc
            )
            portfolio_balance = 30.0

        # --- 4. Build skip set (before scan so AI calls are suppressed for blocked tickers) ---
        # Build set of tickers that already have an open order OR an existing position.
        # Skip both — don't re-order a market we already bet on this cycle.
        def _ticker(obj: Any) -> str | None:
            return getattr(obj, "ticker", None) or getattr(obj, "market_ticker", None)

        # Manually excluded tickers — positions sold/closed manually outside the daemon.
        # Remove entries once the market has resolved.
        _manual_skip: set[str] = {
            "KXHIGHDEN-26APR03-T59",  # Denver 59F sold manually 2026-04-03
            "KXHIGHDEN-26APR03-T58",  # Denver 58F sold manually 2026-04-03
            "KXHIGHCHI-26APR04-T62",  # Chicago tomorrow 63F+ already filled 2026-04-03
            "KXHIGHDEN-26APR03-T61",  # Denver 60F or below already filled 2026-04-03
            "KXHIGHCHI-26APR03-T59",  # Chicago today 59F or below already filled 2026-04-03
            "KXHIGHDEN-26APR04-T60",  # Denver tomorrow 60F or below already filled 2026-04-03
            "KXHIGHDEN-26APR04-T61",  # Denver tomorrow 60F or below (alt ticker) 2026-04-03
        }

        # Pull filled-but-unresolved positions from local DB — persists across restarts.
        # This is the reliable source of truth since get_positions() returns None from Kalshi.
        # Filter to conservative arm only: exploratory paper trades should never block
        # real order placement on the same ticker.
        db_open_tickers: set[str] = {
            t.market_ticker
            for t in self.tracker.get_open_positions()
            if t.strategy_arm != "exploratory"
        } | {
            t.market_ticker
            for t in self.tracker.get_pending_trades()
        }
        if db_open_tickers:
            logger.info("DB open positions (dedup): %s", sorted(db_open_tickers))

        open_order_tickers: set[str] = (
            {_ticker(o) for o in open_orders}
            | {_ticker(p) for p in open_positions}
            | db_open_tickers
            | _manual_skip
        ) - {None}

        # --- 5. Market scan ----------------------------------------------
        if self.health_monitor.is_open("open_meteo"):
            logger.warning("open_meteo circuit OPEN — skipping scan.")
            return

        scan_results: list[ScanResult] = []
        try:
            scan_results, scan_summary = await self.conservative_scanner.scan(
                portfolio_balance=portfolio_balance,
                open_orders_count=open_orders_count,
                risk_manager=self.conservative_risk,
                skip_tickers=open_order_tickers,
            )
            self.health_monitor.record_success("open_meteo")
            self.health_monitor.record_success("kalshi_api")
        except Exception as exc:
            just_tripped = self.health_monitor.record_failure("open_meteo", str(exc))
            if just_tripped:
                await self.notifier.send_circuit_breaker("open_meteo", str(exc))
            return

        logger.info(
            "Scan: fetched=%d, evaluated=%d, approved=%d (%.1fs)",
            scan_summary.total_fetched,
            scan_summary.evaluated,
            scan_summary.approved,
            scan_summary.scan_duration_seconds,
        )

        # --- 6. Execute approved trades + log exploratory arm ---------------
        paused = self._pause_flag.exists()
        if paused:
            logger.info(
                "Daemon is PAUSED (flag: %s). Scan complete; skipping order execution.",
                self._pause_flag,
            )

        placed_this_cycle = 0
        max_orders = self.conservative_risk.limits.max_open_orders

        for result in scan_results:
            if self._stop_event.is_set():
                logger.info("Stop signal mid-cycle — halting execution loop.")
                break

            # Exploratory arm always evaluated (even for rejected results)
            await self._evaluate_exploratory(result, portfolio_balance)

            if result.risk.approved and not paused:
                if result.market_ticker in open_order_tickers:
                    logger.debug(
                        "Skipping %s — open order already exists.",
                        result.market_ticker,
                    )
                    continue
                if open_orders_count + placed_this_cycle >= max_orders:
                    logger.info(
                        "Open orders cap reached (%d+%d >= %d) — stopping execution for this cycle.",
                        open_orders_count, placed_this_cycle, max_orders,
                    )
                    break
                await self._execute_trade(result)
                placed_this_cycle += 1

        # --- 7. Daily summary + file reports -----------------------------
        today_utc = now_utc.date()
        if self._last_summary_date is None or today_utc > self._last_summary_date:
            await self._send_daily_summary()
            self._last_summary_date = today_utc

        if self._last_report_date is None or today_utc > self._last_report_date:
            await asyncio.to_thread(self._write_daily_report, today_utc)
            # Weekly report fires when yesterday (the just-completed day) was Sunday
            yesterday_utc = today_utc - timedelta(days=1)
            if yesterday_utc.weekday() == 6:  # Sunday = 6
                await asyncio.to_thread(self._write_weekly_report, yesterday_utc)
            self._last_report_date = today_utc

    # ------------------------------------------------------------------
    # Trade execution (conservative arm)
    # ------------------------------------------------------------------

    async def _execute_trade(self, result: ScanResult) -> None:
        """
        Place a limit order for an approved scan result and record in tracker.
        Updates risk manager exposure after placement (not after fill —
        conservative: reserve exposure at order time).
        """
        if self.health_monitor.is_open("kalshi_api"):
            logger.warning(
                "kalshi_api circuit OPEN — skipping order for %s",
                result.market_ticker,
            )
            return

        try:
            order_result = await self.order_executor.place_order(
                edge=result.edge,
                ticker=result.market_ticker,
            )
            self.health_monitor.record_success("kalshi_api")
        except Exception as exc:
            just_tripped = self.health_monitor.record_failure("kalshi_api", str(exc))
            if just_tripped:
                await self.notifier.send_circuit_breaker("kalshi_api", str(exc))
            return

        if not order_result.was_placed:
            logger.info(
                "Order skipped for %s: %s",
                result.market_ticker,
                order_result.skip_reason,
            )
            return

        # Determine city from station mapping (fall back to result.city)
        station = self._station_mapping.get(result.series_ticker, {})
        city = station.get("city") or result.city

        trade_id = order_result.order_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        record = TradeRecord(
            trade_id=trade_id,
            market_ticker=result.market_ticker,
            city=city,
            entry_time=now,
            entry_price=order_result.price,
            side=order_result.side.upper(),        # "YES" or "NO"
            quantity=float(order_result.count),
            ensemble_prob_at_entry=result.ensemble.probability,
            ensemble_members_at_entry=result.ensemble.total_members,
            ensemble_spread_at_entry=result.ensemble.ensemble_spread,
            bias_correction_applied=result.ensemble.bias_correction_applied,
            edge_at_entry=result.edge.raw_edge,
            mid_price_at_signal=result.market_price,
            fill_price=0.0,           # updated by _sync_fills() on confirmation
            realized_slippage=0.0,
            was_maker_fill=False,     # unknown until fill confirmed
            fees_paid=0.0,
            order_placed_time=now,
            order_filled_time=None,   # None until confirmed filled
            order_cancelled=False,
            resolution_time=None,
            actual_outcome=None,
            actual_max_temp_f=None,
            pnl=None,
            ai_was_consulted=(result.edge.recommendation == "SANITY_CHECK"),
            ai_cost_usd=0.0,
            bias_version="1.0",
            calibration_version="1.0",
            param_version="conservative_v1",
            strategy_arm="conservative",
        )

        try:
            self.tracker.record_trade(record)
        except Exception as exc:
            logger.error(
                "Failed to record trade %s in tracker: %s", trade_id, exc
            )
            return

        # Reserve exposure at order time (conservative: counts before fill)
        position_dollars = order_result.price * order_result.count
        self.conservative_risk.record_position_opened(position_dollars)

        await self.notifier.send_trade_placed(order_result)
        logger.info(
            "Order placed: %s %s x%d @ $%.2f (edge=%.1f%%)",
            result.market_ticker,
            order_result.direction,
            order_result.count,
            order_result.price,
            result.edge.raw_edge * 100,
        )

    # ------------------------------------------------------------------
    # Exploratory arm (paper-only)
    # ------------------------------------------------------------------

    async def _evaluate_exploratory(
        self,
        conservative_result: ScanResult,
        portfolio_balance: float,
    ) -> None:
        """
        Re-evaluate a conservative scan result under exploratory EdgeParams.

        NEVER places real orders. Logs and records to the tracker with
        strategy_arm="exploratory" so the calibration report can compare arms.

        The exploratory arm reuses the same ensemble data (already cached),
        so no extra API calls are made.
        """
        if conservative_result.ensemble is None:
            return  # Can't evaluate without ensemble data

        try:
            exp_edge = compute_edge(
                market_ticker=conservative_result.market_ticker,
                market_price=conservative_result.market_price,
                ensemble_result=conservative_result.ensemble,
                fee_coefficient=conservative_result.fee_coefficient,
                portfolio_balance=portfolio_balance,
                max_position_dollars=self.exploratory_limits.max_position_dollars,
                params=self.exploratory_params,
            )
        except Exception as exc:
            logger.debug(
                "Exploratory edge calc failed for %s: %s",
                conservative_result.market_ticker,
                exc,
            )
            return

        # Exploratory "would trade" decision (no stateful risk manager check)
        would_trade = (
            exp_edge.recommendation in ("TRADE", "SANITY_CHECK")
            and conservative_result.volume_fp
                >= self.exploratory_limits.min_volume_fp
            and conservative_result.ensemble.total_members
                >= self.exploratory_limits.min_ensemble_members
        )

        logger.debug(
            "Exploratory arm %s: would_trade=%s edge=%.1f%% "
            "(conservative_approved=%s)",
            conservative_result.market_ticker,
            would_trade,
            exp_edge.raw_edge * 100,
            conservative_result.risk.approved,
        )

        if not would_trade:
            return

        # Record as a paper trade — assume immediate fill at signal price
        now = datetime.now(timezone.utc)
        station = self._station_mapping.get(conservative_result.series_ticker, {})
        city = station.get("city") or conservative_result.city

        record = TradeRecord(
            trade_id=f"exp-{uuid.uuid4()}",
            market_ticker=conservative_result.market_ticker,
            city=city,
            entry_time=now,
            entry_price=exp_edge.entry_price,
            side="YES" if exp_edge.direction == "BUY_YES" else "NO",
            quantity=float(exp_edge.recommended_contracts),
            ensemble_prob_at_entry=conservative_result.ensemble.probability,
            ensemble_members_at_entry=conservative_result.ensemble.total_members,
            ensemble_spread_at_entry=conservative_result.ensemble.ensemble_spread,
            bias_correction_applied=conservative_result.ensemble.bias_correction_applied,
            edge_at_entry=exp_edge.raw_edge,
            mid_price_at_signal=conservative_result.market_price,
            fill_price=exp_edge.entry_price,       # paper: assume fill at signal price
            realized_slippage=0.0,
            was_maker_fill=True,                   # paper: assume best case
            fees_paid=0.0,
            order_placed_time=now,
            order_filled_time=now,                 # paper: instant fill
            order_cancelled=False,
            resolution_time=None,
            actual_outcome=None,
            actual_max_temp_f=None,
            pnl=None,
            ai_was_consulted=False,
            ai_cost_usd=0.0,
            bias_version="1.0",
            calibration_version="1.0",
            param_version="exploratory_v1",
            strategy_arm="exploratory",
        )

        try:
            self.tracker.record_trade(record)
        except Exception as exc:
            logger.debug(
                "Exploratory record failed for %s: %s",
                conservative_result.market_ticker,
                exc,
            )

    # ------------------------------------------------------------------
    # Order monitoring and fill sync
    # ------------------------------------------------------------------

    async def _monitor_orders(self) -> None:
        """
        Cancel stale open orders (>10 min old) via OrderExecutor.
        Mark cancelled orders in the tracker and release risk exposure.
        """
        if self.health_monitor.is_open("kalshi_api"):
            return

        try:
            cancelled_ids = await self.order_executor.monitor_open_orders()
            self.health_monitor.record_success("kalshi_api")
        except Exception as exc:
            just_tripped = self.health_monitor.record_failure("kalshi_api", str(exc))
            if just_tripped:
                await self.notifier.send_circuit_breaker("kalshi_api", str(exc))
            return

        for order_id in cancelled_ids:
            self._mark_order_cancelled(order_id)

    def _mark_order_cancelled(self, order_id: str) -> None:
        """
        Flag an order as cancelled in the tracker and release its reserved exposure.
        Only affects conservative-arm trades (exploratory trades have no real exposure).
        """
        try:
            open_positions = self.tracker.get_open_positions()
        except Exception as exc:
            logger.warning("Could not query positions for cancellation: %s", exc)
            return

        for trade in open_positions:
            if trade.trade_id != order_id:
                continue
            if trade.strategy_arm != "conservative":
                break

            try:
                self.tracker.db[self.tracker.TABLE].update(
                    trade.trade_id, {"order_cancelled": 1}
                )
            except Exception as exc:
                logger.warning(
                    "DB update failed marking %s cancelled: %s", order_id, exc
                )

            # Release the exposure reserved at order placement time
            position_dollars = trade.entry_price * trade.quantity
            self.conservative_risk.record_position_closed(
                position_dollars, pnl=0.0
            )
            logger.info(
                "Order %s cancelled; $%.2f exposure released.",
                order_id,
                position_dollars,
            )
            break

    async def _sync_fills(self) -> None:
        """
        Check Kalshi for recently filled or cancelled orders and update the tracker.

        Fetches ALL orders in a single call (no status filter) then inspects
        each order's `status` field directly.  This avoids the failure mode
        where get_orders(status="filled") returns 0 results because Kalshi
        uses a different status string (e.g. "executed" vs "filled", or
        "canceled" vs "cancelled").

        Every cycle logs a status-breakdown line so the actual Kalshi status
        values are visible in the daemon logs for debugging.
        """
        if self.health_monitor.is_open("kalshi_api"):
            return

        # Pending trades: conservative arm, placed but not yet confirmed filled.
        # Uses get_pending_trades() (order_filled_time IS NULL) — NOT
        # get_open_positions() (order_filled_time IS NOT NULL).  Those two
        # conditions are mutually exclusive; using the wrong one means pending
        # is always empty and fills are never detected.
        try:
            pending = self.tracker.get_pending_trades()
        except Exception as exc:
            logger.warning("Could not query pending trades for fill sync: %s", exc)
            return

        if not pending:
            return

        # Fetch ALL orders without a status filter — one call, inspect status
        # on each order object directly instead of relying on specific Kalshi
        # status string values that have changed across API/SDK versions.
        try:
            all_orders = await self.kalshi_client.get_orders(status=None)
            self.health_monitor.record_success("kalshi_api")
        except Exception as exc:
            just_tripped = self.health_monitor.record_failure("kalshi_api", str(exc))
            if just_tripped:
                await self.notifier.send_circuit_breaker("kalshi_api", str(exc))
            return

        # Diagnostic: log status distribution so we can see what Kalshi returns.
        # This is the key tool for figuring out which status strings are real.
        status_counts: dict[str, int] = {}
        for order in all_orders:
            s = str(getattr(order, "status", "unknown"))
            status_counts[s] = status_counts.get(s, 0) + 1
        logger.info(
            "_sync_fills: fetched %d Kalshi orders. Status breakdown: %s. "
            "Pending DB trades to match: %d",
            len(all_orders),
            status_counts or "(none)",
            len(pending),
        )

        # Build lookup by order_id for fast matching
        kalshi_by_id: dict[str, Any] = {}
        for order in all_orders:
            oid = str(
                getattr(order, "order_id", None)
                or getattr(order, "id", None)
                or ""
            )
            if oid:
                kalshi_by_id[oid] = order

        # Status strings that mean "filled" or "cancelled" across Kalshi versions.
        # Kalshi has used at least: "filled", "executed", "canceled", "cancelled",
        # "expired", "resting" (still open).  We match broadly and log what we see.
        FILLED_STATUSES:    frozenset[str] = frozenset({"filled", "executed", "complete", "closed"})
        CANCELLED_STATUSES: frozenset[str] = frozenset({"cancelled", "canceled", "expired", "voided"})

        for trade in pending:
            order = kalshi_by_id.get(trade.trade_id)

            if order is None:
                # trade_id not in Kalshi orders — likely a pre-session-13 UUID
                # trade whose order_id was never stored correctly.  Log at debug
                # level (too noisy at info for known-unfixable old records).
                logger.debug(
                    "_sync_fills: no Kalshi order found for trade_id=%s (%s) — "
                    "may be a pre-fix UUID trade that cannot be matched.",
                    trade.trade_id,
                    trade.market_ticker,
                )
                continue

            order_status = str(getattr(order, "status", "")).lower().strip()

            if order_status in CANCELLED_STATUSES:
                self._mark_order_cancelled(trade.trade_id)
                logger.info(
                    "Marked cancelled (Kalshi status=%r): %s",
                    order_status,
                    trade.market_ticker,
                )
                continue

            if order_status not in FILLED_STATUSES:
                # Still resting/pending — not filled yet
                logger.debug(
                    "_sync_fills: order %s status=%r — still open, skipping.",
                    trade.trade_id,
                    order_status,
                )
                continue

            # ---------------------------------------------------------------
            # Order is filled — extract real fill price from Kalshi
            # ---------------------------------------------------------------
            fill_price = _extract_fill_price(order, trade.side)
            if fill_price <= 0:
                logger.warning(
                    "_sync_fills: could not extract fill price for %s (status=%r). "
                    "Available order fields: %s",
                    trade.market_ticker,
                    order_status,
                    _order_field_summary(order),
                )
                continue

            # Quantity cross-check: verify actual filled quantity matches DB.
            # Partial fills are possible on thin books.
            kalshi_qty = _extract_filled_quantity(order)
            if kalshi_qty is not None and kalshi_qty > 0 and abs(kalshi_qty - trade.quantity) > 0.5:
                logger.warning(
                    "_sync_fills: quantity mismatch for %s — "
                    "DB has %.0f contracts, Kalshi reports %.0f. "
                    "Possible partial fill. Updating DB to Kalshi quantity.",
                    trade.market_ticker,
                    trade.quantity,
                    kalshi_qty,
                )
            else:
                kalshi_qty = None  # No mismatch — don't override DB value

            fill_time = _extract_fill_time(order)
            was_maker = _detect_maker_fill(order, trade.entry_price, trade.side)

            try:
                self.tracker.update_fill(
                    order_id=trade.trade_id,
                    fill_price=fill_price,
                    fill_time=fill_time,
                    was_maker=was_maker,
                    filled_quantity=kalshi_qty,
                )
                logger.info(
                    "Fill confirmed: %s @ $%.4f qty=%.0f maker=%s (Kalshi status=%r)",
                    trade.market_ticker,
                    fill_price,
                    kalshi_qty if kalshi_qty is not None else trade.quantity,
                    was_maker,
                    order_status,
                )
            except Exception as exc:
                logger.warning(
                    "tracker.update_fill failed for %s: %s",
                    trade.trade_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Resolution sync — auto-record outcomes for settled markets
    # ------------------------------------------------------------------

    async def _sync_resolutions(self) -> None:
        """
        Check if any open DB positions have settled on Kalshi, and if so,
        record the outcome automatically so they drop off the dedup list.

        Runs every cycle. For each unique ticker in open positions, fetches
        the market from Kalshi and checks if status is "settled". If it is,
        reads the result ("yes"/"no") and calls tracker.record_outcome().

        This means you never need to manually run any command after a market
        resolves — the daemon cleans up the DB and dedup list on its own.
        """
        if self.health_monitor.is_open("kalshi_api"):
            return

        try:
            open_positions = self.tracker.get_open_positions()
        except Exception as exc:
            logger.warning("Could not query open positions for resolution sync: %s", exc)
            return

        # Only check filled positions (order_filled_time set) — unfilled orders
        # can't have a resolution outcome that matters to us.
        filled = [p for p in open_positions if p.order_filled_time is not None]
        if not filled:
            return

        # Deduplicate — one Kalshi API call per unique ticker
        unique_tickers: set[str] = {p.market_ticker for p in filled}

        for ticker in unique_tickers:
            try:
                market = await self.kalshi_client.get_market(ticker)
            except Exception as exc:
                logger.debug("Could not fetch market %s for resolution check: %s", ticker, exc)
                continue

            # get_market() returns the inner market dict from GetMarketResponse
            if isinstance(market, dict):
                status = market.get("status", "") or ""
                result = market.get("result", "") or ""
                close_time_raw = market.get("close_time")
            else:
                status = getattr(market, "status", "") or ""
                result = getattr(market, "result", "") or ""
                close_time_raw = getattr(market, "close_time", None)

            if status != "settled":
                continue

            # Market has settled — extract result
            if result not in ("yes", "no"):
                logger.warning(
                    "Settled market %s has unexpected result=%r — skipping auto-record",
                    ticker, result,
                )
                continue

            actual_outcome = 1 if result == "yes" else 0

            # Use market close_time as the resolution timestamp
            resolution_time = _parse_datetime(close_time_raw) or datetime.now(timezone.utc)

            try:
                self.tracker.record_outcome(
                    market_ticker=ticker,
                    actual_outcome=actual_outcome,
                    actual_max_temp_f=None,   # NOAA fetch not implemented here; None is fine
                    resolution_time=resolution_time,
                )
                logger.info(
                    "Auto-resolved %s: result=%s (outcome=%d)",
                    ticker, result.upper(), actual_outcome,
                )
                # Release exposure in risk manager for the settled position
                settled_trades = [p for p in filled if p.market_ticker == ticker]
                for trade in settled_trades:
                    # Warn if fill_price is a sentinel value — means we never got
                    # the real price from Kalshi, so P&L for this trade is unreliable.
                    _SENTINEL_FILL_PRICES = {0.0, 0.50}
                    if trade.fill_price in _SENTINEL_FILL_PRICES:
                        logger.warning(
                            "Resolving %s (trade_id=%s) with fill_price=%.2f — "
                            "this is a placeholder, NOT the real Kalshi fill price. "
                            "P&L for this trade is unreliable.",
                            trade.market_ticker,
                            trade.trade_id,
                            trade.fill_price,
                        )
                    exposure = trade.fill_price * trade.quantity
                    self.conservative_risk.record_position_closed(
                        dollars=exposure,
                        pnl=0.0,   # actual P&L is computed inside record_outcome
                    )
            except Exception as exc:
                logger.warning("Failed to auto-record outcome for %s: %s", ticker, exc)

    # ------------------------------------------------------------------
    # Position reconciliation (startup)
    # ------------------------------------------------------------------

    async def _reconcile_positions(self) -> None:
        """
        On startup: compare local DB positions against live Kalshi positions.

        Kalshi API is the source of truth. Any DB positions not found in
        Kalshi's live portfolio may have already resolved or been cancelled —
        these are logged for manual review.

        Also primes the risk manager's exposure counter from the tracker DB
        so the daily exposure cap is enforced correctly from the first cycle.
        """
        logger.info("Reconciling positions against Kalshi API...")

        kalshi_positions: list[Any] = []
        if not self.health_monitor.is_open("kalshi_api"):
            try:
                kalshi_positions = await self.kalshi_client.get_positions()
                self.health_monitor.record_success("kalshi_api")
            except Exception as exc:
                just_tripped = self.health_monitor.record_failure(
                    "kalshi_api", str(exc)
                )
                if just_tripped:
                    await self.notifier.send_circuit_breaker("kalshi_api", str(exc))
                logger.warning(
                    "Reconciliation skipped — could not fetch Kalshi positions: %s",
                    exc,
                )

        # Build set of tickers with live Kalshi positions
        kalshi_tickers: set[str] = set()
        for pos in kalshi_positions:
            ticker = (
                getattr(pos, "ticker", None)
                or getattr(pos, "market_ticker", None)
            )
            if ticker:
                kalshi_tickers.add(ticker)

        # Rebuild risk manager exposure from local DB
        try:
            db_open = self.tracker.get_open_positions()
        except Exception as exc:
            logger.warning("Could not load open positions from DB: %s", exc)
            db_open = []

        total_exposure = 0.0
        for trade in db_open:
            if trade.strategy_arm != "conservative":
                continue
            if trade.order_filled_time is None or trade.order_cancelled:
                continue  # Not actually holding a position

            # This trade is filled and unresolved — counts as real exposure
            total_exposure += trade.fill_price * trade.quantity

            if kalshi_tickers and trade.market_ticker not in kalshi_tickers:
                logger.warning(
                    "Position %s in local DB but not in Kalshi portfolio — "
                    "market may have resolved. Manual review recommended.",
                    trade.market_ticker,
                )

        if total_exposure > 0:
            self.conservative_risk.record_position_opened(total_exposure)

        logger.info(
            "Reconciliation done: %d Kalshi positions, %d DB open, "
            "$%.2f exposure loaded into risk manager.",
            len(kalshi_positions),
            len(db_open),
            total_exposure,
        )

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    async def _send_daily_summary(self) -> None:
        """Compile yesterday's stats and send via notifier."""
        try:
            yesterday = (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).date().isoformat()

            daily_pnl_map = self.tracker.compute_daily_pnl()
            daily_pnl = daily_pnl_map.get(yesterday, 0.0)

            brier = self.tracker.compute_brier_score(n_recent=50)
            open_positions = self.tracker.get_open_positions()
            # Include any filled position that isn't explicitly the paper-only
            # exploratory arm. Records from earlier sessions may have strategy_arm
            # as None/"" rather than "conservative" — they are still real positions.
            conservative_open = [
                p for p in open_positions if p.strategy_arm != "exploratory"
            ]

            await self.notifier.send_daily_summary(
                daily_pnl=daily_pnl,
                brier_score=brier,
                open_positions=len(conservative_open),
            )
            logger.info(
                "Daily summary sent: yesterday=%s P&L=$%.2f Brier=%s open=%d",
                yesterday,
                daily_pnl,
                f"{brier:.4f}" if brier is not None else "N/A",
                len(conservative_open),
            )
        except Exception as exc:
            logger.warning("Failed to send daily summary: %s", exc)

    # ------------------------------------------------------------------
    # File report generation (called via asyncio.to_thread — sync)
    # ------------------------------------------------------------------

    def _write_daily_report(self, for_date: date) -> None:
        """
        Write the daily markdown report for yesterday's activity.
        Called once per UTC day at the first cycle after midnight.
        Errors are caught and logged so a report failure never stops trading.
        """
        try:
            from scripts.generate_reports import generate_daily_report
            yesterday = for_date - timedelta(days=1)
            path = generate_daily_report(
                db_path=self.tracker._db_path,
                report_date=yesterday,
                settings=self._settings,
            )
            logger.info("Daily report written: %s", path)
        except Exception as exc:
            logger.warning("Failed to write daily report: %s", exc)

    def _write_weekly_report(self, week_end: date) -> None:
        """
        Write the weekly markdown report and feed-to-AI package.
        Called once per week when yesterday was Sunday.
        """
        try:
            from scripts.generate_reports import generate_weekly_report, generate_feed_to_ai
            weekly_path = generate_weekly_report(
                db_path=self.tracker._db_path,
                week_end=week_end,
                settings=self._settings,
            )
            ai_path = generate_feed_to_ai(
                db_path=self.tracker._db_path,
                current_week_path=weekly_path,
                settings=self._settings,
            )
            logger.info("Weekly report written: %s", weekly_path)
            logger.info("Feed-to-AI package written: %s", ai_path)
        except Exception as exc:
            logger.warning("Failed to write weekly report: %s", exc)

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _get_poll_interval_seconds(self) -> float:
        """
        Return the poll interval in seconds based on current UTC time.

        Mapping (from settings.yaml scheduling section):
          03:00-04:00 UTC  → 2-min poll  (GFS 00Z model run just published)
          15:00-16:00 UTC  → 2-min poll  (GFS 12Z model run just published)
          04:00-10:00 UTC  → 15-min poll (overnight low-activity window)
          All other times  → 5-min poll  (default)
        """
        now_utc = datetime.now(timezone.utc)
        time_frac = now_utc.hour + now_utc.minute / 60.0

        sched = self._settings.get("scheduling", {})

        for window in sched.get("high_frequency_windows", []):
            start_h, start_m = _parse_hhmm(window.get("start_utc", "00:00"))
            end_h, end_m = _parse_hhmm(window.get("end_utc", "00:00"))
            if (start_h + start_m / 60.0) <= time_frac < (end_h + end_m / 60.0):
                return window.get("poll_interval_minutes", 2) * 60.0

        for window in sched.get("low_frequency_windows", []):
            start_h, start_m = _parse_hhmm(window.get("start_utc", "00:00"))
            end_h, end_m = _parse_hhmm(window.get("end_utc", "00:00"))
            if (start_h + start_m / 60.0) <= time_frac < (end_h + end_m / 60.0):
                return window.get("poll_interval_minutes", 15) * 60.0

        return sched.get("default_poll_interval_minutes", 5) * 60.0

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """
        Register SIGINT and SIGTERM handlers for graceful shutdown.

        On Unix: uses loop.add_signal_handler (runs inside the event loop).
        On Windows: falls back to signal.signal (runs in the main thread,
        then posts to the event loop via call_soon_threadsafe).
        """
        loop = asyncio.get_running_loop()

        def _handler() -> None:
            if not self._stop_event.is_set():
                logger.info(
                    "Shutdown signal received — finishing current cycle then stopping."
                )
                self._stop_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _handler)
            loop.add_signal_handler(signal.SIGTERM, _handler)
        except (NotImplementedError, OSError):
            # Windows: add_signal_handler not supported
            def _sync_handler(sig: int, frame: Any) -> None:
                loop.call_soon_threadsafe(_handler)

            signal.signal(signal.SIGINT, _sync_handler)
            try:
                signal.signal(signal.SIGTERM, _sync_handler)
            except OSError:
                pass  # SIGTERM not available on all Windows configurations


# ---------------------------------------------------------------------------
# Module-level helpers (private)
# ---------------------------------------------------------------------------

def _build_exploratory_params(settings: dict) -> EdgeParams:
    """
    Build EdgeParams for the exploratory (paper-only) arm.

    Reads from settings.experimentation.exploratory, overlaying those values
    onto the base edge settings. Only edge-specific keys are overlaid.
    """
    overrides = settings.get("experimentation", {}).get("exploratory", {})
    edge_section = dict(settings.get("edge", {}))

    edge_keys = {
        "min_edge_high_confidence",
        "min_edge_medium_confidence",
        "assumed_slippage",
        "fee_multiple_required",
        "kelly_fraction",
    }
    for key in edge_keys:
        if key in overrides:
            edge_section[key] = overrides[key]

    merged = dict(settings)
    merged["edge"] = edge_section
    return EdgeParams.from_settings(merged)


def _build_exploratory_limits(settings: dict) -> RiskLimits:
    """
    Build RiskLimits for the exploratory arm.

    Overrides min_volume_fp (from scanner section) and min_ensemble_members
    (from ensemble section) with exploratory values.
    """
    overrides = settings.get("experimentation", {}).get("exploratory", {})
    scanner_section = dict(settings.get("scanner", {}))
    ensemble_section = dict(settings.get("ensemble", {}))

    if "min_volume_fp" in overrides:
        scanner_section["min_volume_fp"] = overrides["min_volume_fp"]
    if "min_ensemble_members" in overrides:
        ensemble_section["min_members_for_trade"] = overrides["min_ensemble_members"]

    merged = dict(settings)
    merged["scanner"] = scanner_section
    merged["ensemble"] = ensemble_section
    return RiskLimits.from_settings(merged)


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute)."""
    try:
        h, m = time_str.split(":")
        return int(h), int(m)
    except Exception:
        return 0, 0


def _parse_balance(balance_obj: Any) -> float:
    """
    Extract portfolio balance in dollars from the Kalshi balance response.

    The kalshi-python SDK returns balance fields as integers (cents).
    Falls back to $100 if the object structure is unrecognised.
    """
    if balance_obj is None:
        return 100.0
    for attr in ("balance", "portfolio_value", "available_balance"):
        val = getattr(balance_obj, attr, None)
        if val is not None:
            try:
                cents = float(val)
                # SDK returns cents (integer) — convert to dollars
                return cents / 100.0 if cents >= 200 else cents
            except (TypeError, ValueError):
                pass
    return 100.0


def _extract_fill_price(order: Any, side: str) -> float:
    """
    Extract the actual execution price from a Kalshi filled order object.

    Priority order:
      1. avg_price   — average fill price; most accurate for partial fills
      2. yes_price / no_price — per-side price in cents or dollars
      3. yes_bid_dollars / no_bid_dollars — dollar-string format
      4. price       — generic fallback

    The SDK returns prices as integers (cents) or dollar strings depending
    on the endpoint/version.  Values > 1.0 are treated as cents and divided
    by 100; values <= 1.0 are already in dollars.

    Logs a debug line showing which field was used so fill-price sourcing
    is visible in the daemon logs.
    """
    # avg_price is the most reliable field for the actual execution price —
    # try it first regardless of side.
    for attr in ("avg_price",):
        val = getattr(order, attr, None)
        if val is None:
            continue
        try:
            price = float(val)
            if price > 0:
                result = price / 100.0 if price > 1.0 else price
                logger.debug("_extract_fill_price: used %r = %.4f", attr, result)
                return result
        except (TypeError, ValueError):
            pass

    # Side-specific fields
    side_attrs = (
        ["yes_price", "yes_bid_dollars"]
        if side.upper() == "YES"
        else ["no_price", "no_bid_dollars"]
    )
    for attr in side_attrs + ["price"]:
        val = getattr(order, attr, None)
        if val is None:
            continue
        try:
            price = float(val)
            if price > 0:
                result = price / 100.0 if price > 1.0 else price
                logger.debug("_extract_fill_price: used %r = %.4f", attr, result)
                return result
        except (TypeError, ValueError):
            pass
    return 0.0


def _extract_fill_time(order: Any) -> datetime:
    """
    Extract the fill timestamp from a Kalshi filled order object.

    Tries multiple attribute names in priority order:
      updated_time > fill_time > execution_time > close_time > placed_time

    Falls back to now() if none are parseable.
    """
    for attr in ("updated_time", "fill_time", "execution_time", "close_time", "placed_time"):
        raw = getattr(order, attr, None)
        if raw is not None:
            result = _parse_datetime(raw)
            if result is not None:
                return result
    return datetime.now(timezone.utc)


def _extract_filled_quantity(order: Any) -> float | None:
    """
    Extract the actual filled quantity from a Kalshi filled order object.

    Returns None if the quantity cannot be determined (caller should keep
    the DB value in that case).

    For a fully filled order: filled_count or count gives the total.
    For a partially filled order: count - remaining_count gives filled qty.
    """
    # Direct filled-quantity field
    for attr in ("filled_count", "filled_count_fp", "executed_count"):
        val = getattr(order, attr, None)
        if val is not None:
            try:
                qty = float(val)
                if qty > 0:
                    return qty
            except (TypeError, ValueError):
                pass

    # Derive from count - remaining_count
    count_val = getattr(order, "count", None) or getattr(order, "count_fp", None)
    remaining_val = getattr(order, "remaining_count", None)
    if count_val is not None and remaining_val is not None:
        try:
            filled = float(count_val) - float(remaining_val)
            if filled > 0:
                return filled
        except (TypeError, ValueError):
            pass

    # Fall back to total count for fully-filled orders
    if count_val is not None:
        try:
            qty = float(count_val)
            if qty > 0:
                return qty
        except (TypeError, ValueError):
            pass

    return None


def _order_field_summary(order: Any) -> str:
    """
    Return a compact summary of non-None attributes on a Kalshi order object.
    Used in warning logs when fill price extraction fails, so we can see
    exactly which fields Kalshi returned and diagnose the right field name.
    """
    try:
        if hasattr(order, "model_dump"):
            dump = order.model_dump()
            return str({k: v for k, v in dump.items() if v is not None})
        if hasattr(order, "__dict__"):
            return str({k: v for k, v in order.__dict__.items() if v is not None})
        return repr(order)
    except Exception:
        return repr(order)


def _detect_maker_fill(order: Any, entry_price: float, side: str) -> bool:
    """
    Heuristic: the fill was a maker fill if the execution price exactly
    matches the limit price we submitted (within 0.5 cents).

    The Kalshi SDK doesn't expose maker/taker status directly on fills.
    """
    fill_price = _extract_fill_price(order, side)
    if fill_price <= 0:
        return False
    return abs(fill_price - entry_price) < 0.005


def _parse_datetime(raw: Any) -> datetime:
    """
    Parse a datetime value from various formats the SDK may return.
    Returns a UTC-aware datetime; falls back to now() if parsing fails.
    """
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S+00:00",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return datetime.now(timezone.utc)
