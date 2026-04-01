"""
Performance Tracker.

Records all trades, fills, outcomes, and computes performance metrics.

Design decisions:
  - Pure synchronous SQLite via sqlite-utils. No async — callers either run
    this in a thread or accept that DB writes are fast enough not to matter.
  - DB path comes from settings.yaml key `db_path` (default `./data/trades.db`).
    Pass `:memory:` to get an in-memory DB for tests.
  - `trade_id` is the primary key. It should be set by the caller (e.g. the
    Kalshi order ID, or a UUID generated before submission).
  - Datetime fields are stored as ISO 8601 strings; conversion is handled
    internally so callers always work with Python datetime objects.
  - `strategy_arm`: "conservative" or "exploratory" for dual-arm logging
    (spec section 3.5). The exploratory arm never places real orders but its
    hypothetical P&L is tracked here for comparison.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from typing import Any

from sqlite_utils import Database

logger = logging.getLogger(__name__)

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


# ---------------------------------------------------------------------------
# TradeRecord dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """
    Full record of a single trade from signal to resolution.

    All fields match spec §1.10. Fields that are not yet known at order
    placement time should be passed as None and updated later via
    update_fill() and record_outcome().
    """
    trade_id: str
    market_ticker: str
    city: str
    entry_time: datetime
    entry_price: float
    side: str                           # "YES" or "NO"
    quantity: float                     # supports fractional contracts
    ensemble_prob_at_entry: float
    ensemble_members_at_entry: int
    ensemble_spread_at_entry: float
    bias_correction_applied: float
    edge_at_entry: float
    mid_price_at_signal: float          # mid-price when edge was first computed
    fill_price: float                   # actual fill price (0.0 until filled)
    realized_slippage: float            # fill_price - mid_price_at_signal
    was_maker_fill: bool                # True → zero fees
    fees_paid: float
    order_placed_time: datetime         # when limit order was submitted
    order_filled_time: datetime | None  # None until filled
    order_cancelled: bool               # True if order expired unfilled
    resolution_time: datetime | None    # None until market resolves
    actual_outcome: int | None          # 0 or 1 after resolution
    actual_max_temp_f: float | None     # NOAA ground truth
    pnl: float | None                   # None until resolved
    ai_was_consulted: bool
    ai_cost_usd: float
    bias_version: str
    calibration_version: str
    param_version: str
    strategy_arm: str                   # "conservative" or "exploratory"


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class Tracker:
    """
    SQLite-backed trade recorder and performance calculator.

    Usage:
        tracker = Tracker(db_path="./data/trades.db")
        tracker.record_trade(record)
        tracker.update_fill(order_id, fill_price, fill_time, was_maker)
        tracker.record_outcome(ticker, outcome, temp_f, resolution_time)
        score = tracker.compute_brier_score()
        pnl   = tracker.compute_daily_pnl()
        open_positions = tracker.get_open_positions()
    """

    TABLE = "trades"

    def __init__(self, db_path: str = "./data/trades.db") -> None:
        self._db_path = db_path
        self.db = Database(db_path)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the trades table if it does not exist."""
        if self.TABLE not in self.db.table_names():
            self.db[self.TABLE].create(
                {
                    "trade_id":                   str,
                    "market_ticker":              str,
                    "city":                       str,
                    "entry_time":                 str,
                    "entry_price":                float,
                    "side":                       str,
                    "quantity":                   float,
                    "ensemble_prob_at_entry":     float,
                    "ensemble_members_at_entry":  int,
                    "ensemble_spread_at_entry":   float,
                    "bias_correction_applied":    float,
                    "edge_at_entry":              float,
                    "mid_price_at_signal":        float,
                    "fill_price":                 float,
                    "realized_slippage":          float,
                    "was_maker_fill":             int,   # SQLite has no bool
                    "fees_paid":                  float,
                    "order_placed_time":          str,
                    "order_filled_time":          str,   # nullable
                    "order_cancelled":            int,
                    "resolution_time":            str,   # nullable
                    "actual_outcome":             int,   # nullable
                    "actual_max_temp_f":          float, # nullable
                    "pnl":                        float, # nullable
                    "ai_was_consulted":           int,
                    "ai_cost_usd":                float,
                    "bias_version":               str,
                    "calibration_version":        str,
                    "param_version":              str,
                    "strategy_arm":               str,
                },
                pk="trade_id",
            )
            logger.info("Created trades table in %s", self._db_path)

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def record_trade(self, record: TradeRecord) -> None:
        """
        Insert a new trade record. Call this when the limit order is placed.

        Args:
            record: TradeRecord with at minimum trade_id, market_ticker,
                    entry_time, entry_price, and order_placed_time.

        Raises:
            sqlite3.IntegrityError: if trade_id already exists.
        """
        row = _to_row(record)
        self.db[self.TABLE].insert(row)
        logger.debug("Recorded trade %s on %s", record.trade_id, record.market_ticker)

    def update_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_time: datetime,
        was_maker: bool,
    ) -> None:
        """
        Update a trade when the limit order fills.

        Computes realized_slippage = fill_price - mid_price_at_signal.

        Args:
            order_id:   trade_id of the order that filled
            fill_price: price the order was filled at
            fill_time:  when the fill occurred
            was_maker:  True if maker fill (zero fees)
        """
        row = self.db[self.TABLE].get(order_id)
        mid = row["mid_price_at_signal"]
        slippage = fill_price - mid

        self.db[self.TABLE].update(
            order_id,
            {
                "fill_price":        fill_price,
                "order_filled_time": _dt_to_str(fill_time),
                "was_maker_fill":    int(was_maker),
                "realized_slippage": slippage,
            },
        )
        logger.debug(
            "Updated fill for %s: price=%.4f slippage=%.4f maker=%s",
            order_id, fill_price, slippage, was_maker,
        )

    def record_outcome(
        self,
        market_ticker: str,
        actual_outcome: int,
        actual_max_temp_f: float | None,
        resolution_time: datetime,
    ) -> None:
        """
        Record settlement outcome for ALL trades on a resolved market.

        Computes P&L for each trade on the market:
          - If side == "YES": pnl = (outcome - fill_price) * quantity - fees_paid
          - If side == "NO":  pnl = ((1 - outcome) - fill_price) * quantity - fees_paid
          - Cancelled or unfilled trades: pnl = 0
          - Not-yet-filled trades: pnl = 0 (order was placed but didn't fill)

        Args:
            market_ticker:    the Kalshi ticker
            actual_outcome:   1 if the condition was TRUE (e.g. temp exceeded threshold), 0 otherwise
            actual_max_temp_f: NOAA-recorded max temp (None if not available)
            resolution_time:  when the market settled
        """
        trades = list(
            self.db[self.TABLE].rows_where(
                "market_ticker = ? AND actual_outcome IS NULL AND order_cancelled = 0",
                [market_ticker],
            )
        )

        updated = 0
        for row in trades:
            fill_price = row["fill_price"]
            was_filled = row["order_filled_time"] is not None

            if not was_filled or fill_price == 0.0:
                pnl = 0.0
            else:
                qty = row["quantity"]
                fees = row["fees_paid"]
                side = row["side"].upper()
                if side == "YES":
                    pnl = (actual_outcome - fill_price) * qty - fees
                else:  # NO
                    pnl = ((1 - actual_outcome) - fill_price) * qty - fees

            self.db[self.TABLE].update(
                row["trade_id"],
                {
                    "actual_outcome":   actual_outcome,
                    "actual_max_temp_f": actual_max_temp_f,
                    "resolution_time":  _dt_to_str(resolution_time),
                    "pnl":              round(pnl, 4),
                },
            )
            updated += 1

        logger.info(
            "Recorded outcome for %s: outcome=%d updated=%d trades",
            market_ticker, actual_outcome, updated,
        )

    # ------------------------------------------------------------------
    # Read / analytics methods
    # ------------------------------------------------------------------

    def compute_brier_score(self, n_recent: int | None = None) -> float | None:
        """
        Compute Brier score: mean((ensemble_prob - actual_outcome)^2).

        Only includes resolved trades (actual_outcome IS NOT NULL) that were
        not cancelled. Lower is better; target < 0.18.

        Args:
            n_recent: if provided, only use the most recent N resolved trades
                      (ordered by resolution_time descending). None = all.

        Returns:
            Brier score as a float, or None if no resolved trades exist.
        """
        query = (
            "SELECT ensemble_prob_at_entry, actual_outcome "
            "FROM trades "
            "WHERE actual_outcome IS NOT NULL AND order_cancelled = 0 "
            "ORDER BY resolution_time DESC"
        )
        rows = list(self.db.execute(query).fetchall())

        if not rows:
            return None

        if n_recent is not None:
            rows = rows[:n_recent]

        total = sum(
            (prob - outcome) ** 2
            for prob, outcome in rows
        )
        return total / len(rows)

    def compute_daily_pnl(self) -> dict[str, float]:
        """
        Compute net P&L per calendar day (UTC) for all resolved trades.

        Returns:
            Dict mapping ISO date string (e.g. "2026-04-01") to total P&L for
            that day. Only dates with at least one resolved trade appear.
        """
        query = (
            "SELECT resolution_time, pnl "
            "FROM trades "
            "WHERE pnl IS NOT NULL AND resolution_time IS NOT NULL"
        )
        rows = self.db.execute(query).fetchall()

        daily: dict[str, float] = {}
        for res_time_str, pnl in rows:
            if res_time_str is None or pnl is None:
                continue
            day = _str_to_dt(res_time_str).date().isoformat()
            daily[day] = round(daily.get(day, 0.0) + pnl, 4)

        return daily

    def get_open_positions(self) -> list[TradeRecord]:
        """
        Return all trades that:
          - Were filled (order_filled_time IS NOT NULL)
          - Have not yet resolved (actual_outcome IS NULL)
          - Were not cancelled

        These are contracts currently held in the portfolio.

        Returns:
            List of TradeRecord objects, ordered by order_filled_time ascending.
        """
        rows = list(
            self.db[self.TABLE].rows_where(
                "order_filled_time IS NOT NULL "
                "AND actual_outcome IS NULL "
                "AND order_cancelled = 0",
                order_by="order_filled_time ASC",
            )
        )
        return [_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: datetime | None) -> str | None:
    """Convert datetime to ISO 8601 string for storage. None → None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    """Parse ISO 8601 string from DB back to datetime. None → None."""
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_row(record: TradeRecord) -> dict[str, Any]:
    """Convert a TradeRecord to a DB-ready dict."""
    d = asdict(record)
    # Convert datetime fields to strings
    for field_name in (
        "entry_time", "order_placed_time", "order_filled_time", "resolution_time"
    ):
        d[field_name] = _dt_to_str(d[field_name])
    # Convert booleans to ints
    d["was_maker_fill"] = int(d["was_maker_fill"])
    d["order_cancelled"] = int(d["order_cancelled"])
    d["ai_was_consulted"] = int(d["ai_was_consulted"])
    return d


def _from_row(row: dict[str, Any]) -> TradeRecord:
    """Convert a DB row dict back to a TradeRecord."""
    d = dict(row)
    # Convert datetime strings back to datetime objects
    for field_name in (
        "entry_time", "order_placed_time", "order_filled_time", "resolution_time"
    ):
        d[field_name] = _str_to_dt(d[field_name])
    # Convert ints back to booleans
    d["was_maker_fill"] = bool(d["was_maker_fill"])
    d["order_cancelled"] = bool(d["order_cancelled"])
    d["ai_was_consulted"] = bool(d["ai_was_consulted"])
    return TradeRecord(**d)
