"""
Tests for src/tracker.py.

All tests use an in-memory SQLite database (:memory:) — no files written to disk.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from src.tracker import Tracker, TradeRecord, _dt_to_str, _str_to_dt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker() -> Tracker:
    return Tracker(db_path=":memory:")


def _dt(year=2026, month=4, day=1, hour=12, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _record(
    trade_id: str = "order-001",
    market_ticker: str = "KXHIGHNY-26APR01-T79",
    city: str = "New York",
    side: str = "YES",
    entry_price: float = 0.55,
    fill_price: float = 0.0,
    order_filled_time: datetime | None = None,
    order_cancelled: bool = False,
    actual_outcome: int | None = None,
    pnl: float | None = None,
    strategy_arm: str = "conservative",
    quantity: float = 5.0,
    ensemble_prob: float = 0.70,
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        market_ticker=market_ticker,
        city=city,
        entry_time=_dt(),
        entry_price=entry_price,
        side=side,
        quantity=quantity,
        ensemble_prob_at_entry=ensemble_prob,
        ensemble_members_at_entry=139,
        ensemble_spread_at_entry=8.0,
        bias_correction_applied=0.5,
        edge_at_entry=0.15,
        mid_price_at_signal=0.55,
        fill_price=fill_price,
        realized_slippage=0.0,
        was_maker_fill=False,
        fees_paid=0.0,
        order_placed_time=_dt(),
        order_filled_time=order_filled_time,
        order_cancelled=order_cancelled,
        resolution_time=None,
        actual_outcome=actual_outcome,
        actual_max_temp_f=None,
        pnl=pnl,
        ai_was_consulted=False,
        ai_cost_usd=0.0,
        bias_version="v1",
        calibration_version="v1",
        param_version="v1",
        strategy_arm=strategy_arm,
    )


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_table_on_first_use(self):
        t = _make_tracker()
        assert "trades" in t.db.table_names()

    def test_second_init_does_not_crash(self):
        t = _make_tracker()
        t.record_trade(_record())
        # Re-open same in-memory db object — _ensure_schema must be idempotent
        t._ensure_schema()
        rows = list(t.db["trades"].rows)
        assert len(rows) == 1

    def test_all_columns_present(self):
        t = _make_tracker()
        cols = {col.name for col in t.db["trades"].columns}
        expected = {
            "trade_id", "market_ticker", "city", "entry_time", "entry_price",
            "side", "quantity", "ensemble_prob_at_entry", "ensemble_members_at_entry",
            "ensemble_spread_at_entry", "bias_correction_applied", "edge_at_entry",
            "mid_price_at_signal", "fill_price", "realized_slippage", "was_maker_fill",
            "fees_paid", "order_placed_time", "order_filled_time", "order_cancelled",
            "resolution_time", "actual_outcome", "actual_max_temp_f", "pnl",
            "ai_was_consulted", "ai_cost_usd", "bias_version", "calibration_version",
            "param_version", "strategy_arm",
        }
        assert expected.issubset(cols)


# ---------------------------------------------------------------------------
# record_trade
# ---------------------------------------------------------------------------

class TestRecordTrade:
    def test_inserts_row(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        assert t.db["trades"].count == 1

    def test_round_trip_fields(self):
        t = _make_tracker()
        rec = _record("t1", side="NO", entry_price=0.35, quantity=3.0, strategy_arm="exploratory")
        t.record_trade(rec)
        row = t.db["trades"].get("t1")
        assert row["market_ticker"] == "KXHIGHNY-26APR01-T79"
        assert row["side"] == "NO"
        assert row["entry_price"] == pytest.approx(0.35)
        assert row["quantity"] == pytest.approx(3.0)
        assert row["strategy_arm"] == "exploratory"

    def test_booleans_stored_as_ints(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        row = t.db["trades"].get("t1")
        assert row["was_maker_fill"] == 0
        assert row["order_cancelled"] == 0
        assert row["ai_was_consulted"] == 0

    def test_duplicate_trade_id_raises(self):
        import sqlite3
        t = _make_tracker()
        t.record_trade(_record("dup"))
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            t.record_trade(_record("dup"))

    def test_nullable_fields_stored_as_none(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        row = t.db["trades"].get("t1")
        assert row["order_filled_time"] is None
        assert row["resolution_time"] is None
        assert row["actual_outcome"] is None
        assert row["actual_max_temp_f"] is None
        assert row["pnl"] is None

    def test_datetime_stored_as_iso_string(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        row = t.db["trades"].get("t1")
        # Must be a string, not a datetime object
        assert isinstance(row["entry_time"], str)
        assert "2026" in row["entry_time"]

    def test_multiple_records(self):
        t = _make_tracker()
        for i in range(5):
            t.record_trade(_record(f"t{i}"))
        assert t.db["trades"].count == 5


# ---------------------------------------------------------------------------
# update_fill
# ---------------------------------------------------------------------------

class TestUpdateFill:
    def test_updates_fill_price(self):
        t = _make_tracker()
        t.record_trade(_record("t1", entry_price=0.55))
        fill_time = _dt(hour=13)
        t.update_fill("t1", fill_price=0.56, fill_time=fill_time, was_maker=True)
        row = t.db["trades"].get("t1")
        assert row["fill_price"] == pytest.approx(0.56)

    def test_computes_slippage(self):
        # mid_price_at_signal = 0.55 (set in _record default)
        t = _make_tracker()
        t.record_trade(_record("t1"))
        t.update_fill("t1", fill_price=0.57, fill_time=_dt(hour=13), was_maker=False)
        row = t.db["trades"].get("t1")
        assert row["realized_slippage"] == pytest.approx(0.57 - 0.55)

    def test_negative_slippage_when_filled_below_signal(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        t.update_fill("t1", fill_price=0.53, fill_time=_dt(hour=13), was_maker=True)
        row = t.db["trades"].get("t1")
        assert row["realized_slippage"] == pytest.approx(0.53 - 0.55)

    def test_maker_fill_stored_as_int_1(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        t.update_fill("t1", fill_price=0.55, fill_time=_dt(hour=13), was_maker=True)
        row = t.db["trades"].get("t1")
        assert row["was_maker_fill"] == 1

    def test_taker_fill_stored_as_int_0(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        t.update_fill("t1", fill_price=0.55, fill_time=_dt(hour=13), was_maker=False)
        row = t.db["trades"].get("t1")
        assert row["was_maker_fill"] == 0

    def test_fill_time_stored_as_iso_string(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        fill_time = datetime(2026, 4, 1, 14, 30, 0, tzinfo=timezone.utc)
        t.update_fill("t1", fill_price=0.55, fill_time=fill_time, was_maker=True)
        row = t.db["trades"].get("t1")
        assert isinstance(row["order_filled_time"], str)
        assert "14:30" in row["order_filled_time"]

    def test_filled_quantity_updates_quantity_when_provided(self):
        t = _make_tracker()
        t.record_trade(_record("t1", quantity=10.0))
        t.update_fill("t1", fill_price=0.55, fill_time=_dt(hour=13), was_maker=True, filled_quantity=7.0)
        row = t.db["trades"].get("t1")
        assert row["quantity"] == pytest.approx(7.0)

    def test_filled_quantity_none_leaves_quantity_unchanged(self):
        t = _make_tracker()
        t.record_trade(_record("t1", quantity=10.0))
        t.update_fill("t1", fill_price=0.55, fill_time=_dt(hour=13), was_maker=True, filled_quantity=None)
        row = t.db["trades"].get("t1")
        assert row["quantity"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------

class TestRecordOutcome:
    def _filled_record(self, trade_id: str, side: str = "YES", qty: float = 5.0,
                       fill_price: float = 0.55, fees: float = 0.02) -> TradeRecord:
        rec = _record(trade_id, side=side, quantity=qty, fill_price=fill_price)
        rec.order_filled_time = _dt(hour=13)
        rec.fees_paid = fees
        return rec

    def test_yes_win_pnl(self):
        t = _make_tracker()
        rec = self._filled_record("t1", side="YES", qty=5.0, fill_price=0.55, fees=0.02)
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=82.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        # pnl = (1 - 0.55) * 5 - 0.02 = 2.25 - 0.02 = 2.23
        assert row["pnl"] == pytest.approx(2.23)

    def test_yes_loss_pnl(self):
        t = _make_tracker()
        rec = self._filled_record("t1", side="YES", qty=5.0, fill_price=0.55, fees=0.02)
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=0,
                         actual_max_temp_f=72.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        # pnl = (0 - 0.55) * 5 - 0.02 = -2.75 - 0.02 = -2.77
        assert row["pnl"] == pytest.approx(-2.77)

    def test_no_win_pnl(self):
        t = _make_tracker()
        rec = self._filled_record("t1", side="NO", qty=4.0, fill_price=0.40, fees=0.01)
        t.record_trade(rec)
        # NO wins when outcome=0
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=0,
                         actual_max_temp_f=72.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        # pnl = (1 - 0 - 0.40) * 4 - 0.01 = 0.60 * 4 - 0.01 = 2.40 - 0.01 = 2.39
        assert row["pnl"] == pytest.approx(2.39)

    def test_no_loss_pnl(self):
        t = _make_tracker()
        rec = self._filled_record("t1", side="NO", qty=4.0, fill_price=0.40, fees=0.01)
        t.record_trade(rec)
        # NO loses when outcome=1
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=85.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        # pnl = (1 - 1 - 0.40) * 4 - 0.01 = -0.40 * 4 - 0.01 = -1.60 - 0.01 = -1.61
        assert row["pnl"] == pytest.approx(-1.61)

    def test_unfilled_order_gets_zero_pnl(self):
        t = _make_tracker()
        rec = _record("t1")  # no fill (order_filled_time=None, fill_price=0.0)
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=82.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        assert row["pnl"] == pytest.approx(0.0)

    def test_cancelled_order_skipped(self):
        t = _make_tracker()
        rec = _record("t1", order_cancelled=True)
        rec.order_filled_time = _dt(hour=13)  # shouldn't matter
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=82.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        assert row["actual_outcome"] is None  # cancelled → skipped entirely

    def test_actual_temp_stored(self):
        t = _make_tracker()
        rec = self._filled_record("t1")
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=83.5, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        assert row["actual_max_temp_f"] == pytest.approx(83.5)

    def test_multiple_trades_same_market(self):
        t = _make_tracker()
        for i in range(3):
            rec = self._filled_record(f"t{i}", side="YES", qty=2.0, fill_price=0.60, fees=0.01)
            t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=85.0, resolution_time=_dt(day=2))
        for i in range(3):
            row = t.db["trades"].get(f"t{i}")
            assert row["actual_outcome"] == 1
            assert row["pnl"] is not None

    def test_only_updates_matching_ticker(self):
        t = _make_tracker()
        rec_ny = self._filled_record("t1", fill_price=0.55)
        rec_chi = _record("t2", market_ticker="KXHIGHCHI-26APR01-T65", fill_price=0.0)
        t.record_trade(rec_ny)
        t.record_trade(rec_chi)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=82.0, resolution_time=_dt(day=2))
        row_ny = t.db["trades"].get("t1")
        row_chi = t.db["trades"].get("t2")
        assert row_ny["actual_outcome"] == 1
        assert row_chi["actual_outcome"] is None  # untouched

    def test_already_resolved_not_double_updated(self):
        t = _make_tracker()
        rec = self._filled_record("t1", fill_price=0.55)
        t.record_trade(rec)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=1,
                         actual_max_temp_f=82.0, resolution_time=_dt(day=2))
        # Second call — should find no matching rows (already has outcome)
        t.record_outcome("KXHIGHNY-26APR01-T79", actual_outcome=0,
                         actual_max_temp_f=70.0, resolution_time=_dt(day=2))
        row = t.db["trades"].get("t1")
        assert row["actual_outcome"] == 1  # unchanged


# ---------------------------------------------------------------------------
# compute_brier_score
# ---------------------------------------------------------------------------

class TestComputeBrierScore:
    def _resolved(self, trade_id: str, prob: float, outcome: int,
                  resolution_day: int = 2) -> TradeRecord:
        rec = _record(trade_id, ensemble_prob=prob, actual_outcome=outcome)
        rec.order_filled_time = _dt(hour=13)
        rec.resolution_time = _dt(day=resolution_day)
        return rec

    def test_returns_none_when_no_resolved_trades(self):
        t = _make_tracker()
        assert t.compute_brier_score() is None

    def test_perfect_prediction(self):
        t = _make_tracker()
        # prob=1.0, outcome=1 → BS = (1-1)^2 = 0
        rec = self._resolved("t1", prob=1.0, outcome=1)
        t.record_trade(rec)
        assert t.compute_brier_score() == pytest.approx(0.0)

    def test_worst_prediction(self):
        t = _make_tracker()
        # prob=1.0, outcome=0 → BS = (1-0)^2 = 1
        rec = self._resolved("t1", prob=1.0, outcome=0)
        t.record_trade(rec)
        assert t.compute_brier_score() == pytest.approx(1.0)

    def test_multiple_trades_average(self):
        t = _make_tracker()
        # (0.7-1)^2 = 0.09, (0.3-0)^2 = 0.09 → mean = 0.09
        t.record_trade(self._resolved("t1", prob=0.7, outcome=1))
        t.record_trade(self._resolved("t2", prob=0.3, outcome=0))
        assert t.compute_brier_score() == pytest.approx(0.09)

    def test_n_recent_limits_to_latest(self):
        t = _make_tracker()
        # older: prob=1.0, outcome=0 → BS contribution 1.0
        # recent: prob=0.5, outcome=1 → BS contribution 0.25
        t.record_trade(self._resolved("old", prob=1.0, outcome=0, resolution_day=1))
        t.record_trade(self._resolved("recent", prob=0.5, outcome=1, resolution_day=3))
        # n_recent=1 → only the most recent (day 3): BS = (0.5-1)^2 = 0.25
        assert t.compute_brier_score(n_recent=1) == pytest.approx(0.25)

    def test_cancelled_trades_excluded(self):
        t = _make_tracker()
        rec = _record("t1", order_cancelled=True, actual_outcome=None, ensemble_prob=0.9)
        rec.resolution_time = _dt(day=2)
        t.record_trade(rec)
        assert t.compute_brier_score() is None

    def test_unresolved_trades_excluded(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))  # actual_outcome=None
        assert t.compute_brier_score() is None


# ---------------------------------------------------------------------------
# compute_daily_pnl
# ---------------------------------------------------------------------------

class TestComputeDailyPnl:
    def test_empty_returns_empty_dict(self):
        t = _make_tracker()
        assert t.compute_daily_pnl() == {}

    def test_single_trade(self):
        t = _make_tracker()
        rec = _record("t1", pnl=2.50)
        rec.resolution_time = _dt(day=1)
        t.record_trade(rec)
        result = t.compute_daily_pnl()
        assert "2026-04-01" in result
        assert result["2026-04-01"] == pytest.approx(2.50)

    def test_multiple_trades_same_day_summed(self):
        t = _make_tracker()
        for i, pnl in enumerate([1.0, 2.0, -0.5]):
            rec = _record(f"t{i}", pnl=pnl)
            rec.resolution_time = _dt(day=1)
            t.record_trade(rec)
        result = t.compute_daily_pnl()
        assert result["2026-04-01"] == pytest.approx(2.5)

    def test_trades_on_different_days(self):
        t = _make_tracker()
        rec1 = _record("t1", pnl=1.0)
        rec1.resolution_time = _dt(day=1)
        rec2 = _record("t2", pnl=3.0)
        rec2.resolution_time = _dt(day=2)
        t.record_trade(rec1)
        t.record_trade(rec2)
        result = t.compute_daily_pnl()
        assert result["2026-04-01"] == pytest.approx(1.0)
        assert result["2026-04-02"] == pytest.approx(3.0)

    def test_unresolved_trades_excluded(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))  # pnl=None, resolution_time=None
        assert t.compute_daily_pnl() == {}

    def test_negative_pnl_day(self):
        t = _make_tracker()
        rec = _record("t1", pnl=-5.00)
        rec.resolution_time = _dt(day=1)
        t.record_trade(rec)
        result = t.compute_daily_pnl()
        assert result["2026-04-01"] == pytest.approx(-5.00)


# ---------------------------------------------------------------------------
# get_open_positions
# ---------------------------------------------------------------------------

class TestGetOpenPositions:
    def test_empty_when_no_trades(self):
        t = _make_tracker()
        assert t.get_open_positions() == []

    def test_returns_filled_unresolved(self):
        t = _make_tracker()
        rec = _record("t1")
        rec.order_filled_time = _dt(hour=13)
        t.record_trade(rec)
        positions = t.get_open_positions()
        assert len(positions) == 1
        assert positions[0].trade_id == "t1"

    def test_excludes_unfilled_orders(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))  # order_filled_time=None
        assert t.get_open_positions() == []

    def test_excludes_cancelled(self):
        t = _make_tracker()
        rec = _record("t1", order_cancelled=True)
        rec.order_filled_time = _dt(hour=13)
        t.record_trade(rec)
        assert t.get_open_positions() == []

    def test_excludes_resolved(self):
        t = _make_tracker()
        rec = _record("t1", actual_outcome=1, pnl=2.0)
        rec.order_filled_time = _dt(hour=13)
        rec.resolution_time = _dt(day=2)
        t.record_trade(rec)
        assert t.get_open_positions() == []

    def test_returns_trade_record_objects(self):
        t = _make_tracker()
        rec = _record("t1", side="NO", entry_price=0.40, strategy_arm="exploratory")
        rec.order_filled_time = _dt(hour=13)
        t.record_trade(rec)
        positions = t.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert isinstance(pos, TradeRecord)
        assert pos.side == "NO"
        assert pos.strategy_arm == "exploratory"
        assert pos.entry_price == pytest.approx(0.40)

    def test_ordered_by_fill_time_ascending(self):
        t = _make_tracker()
        for i, hour in enumerate([15, 9, 12]):
            rec = _record(f"t{i}")
            rec.order_filled_time = _dt(hour=hour)
            t.record_trade(rec)
        positions = t.get_open_positions()
        fill_hours = [p.order_filled_time.hour for p in positions]
        assert fill_hours == sorted(fill_hours)

    def test_multiple_markets(self):
        t = _make_tracker()
        for i, ticker in enumerate(["KXHIGHNY-26APR01-T79", "KXHIGHCHI-26APR01-T65"]):
            rec = _record(f"t{i}", market_ticker=ticker)
            rec.order_filled_time = _dt(hour=12 + i)
            t.record_trade(rec)
        positions = t.get_open_positions()
        assert len(positions) == 2

    def test_booleans_round_trip_correctly(self):
        t = _make_tracker()
        rec = _record("t1")
        rec.order_filled_time = _dt(hour=13)
        rec.was_maker_fill = True
        rec.ai_was_consulted = True
        t.record_trade(rec)
        pos = t.get_open_positions()[0]
        assert pos.was_maker_fill is True
        assert pos.ai_was_consulted is True
        assert pos.order_cancelled is False


# ---------------------------------------------------------------------------
# get_pending_trades
# ---------------------------------------------------------------------------

class TestGetPendingTrades:
    def test_returns_unfilled_conservative_trades(self):
        t = _make_tracker()
        t.record_trade(_record("t1", strategy_arm="conservative"))  # order_filled_time=None
        pending = t.get_pending_trades()
        assert len(pending) == 1
        assert pending[0].trade_id == "t1"

    def test_excludes_filled_trades(self):
        t = _make_tracker()
        rec = _record("t1")
        rec.order_filled_time = _dt(hour=13)
        t.record_trade(rec)
        assert t.get_pending_trades() == []

    def test_excludes_cancelled_trades(self):
        t = _make_tracker()
        t.record_trade(_record("t1", order_cancelled=True))
        assert t.get_pending_trades() == []

    def test_excludes_exploratory_trades(self):
        t = _make_tracker()
        t.record_trade(_record("t1", strategy_arm="exploratory"))
        assert t.get_pending_trades() == []

    def test_returns_multiple_pending(self):
        t = _make_tracker()
        t.record_trade(_record("t1"))
        t.record_trade(_record("t2", market_ticker="KXHIGHCHI-26APR01-T65"))
        assert len(t.get_pending_trades()) == 2

    def test_empty_when_no_trades(self):
        t = _make_tracker()
        assert t.get_pending_trades() == []


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

class TestDatetimeHelpers:
    def test_dt_to_str_utc(self):
        dt = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        s = _dt_to_str(dt)
        assert "2026-04-01" in s
        assert "12:00:00" in s

    def test_dt_to_str_naive_assumes_utc(self):
        dt = datetime(2026, 4, 1, 12, 0, 0)
        s = _dt_to_str(dt)
        assert s is not None
        assert "2026-04-01" in s

    def test_dt_to_str_none(self):
        assert _dt_to_str(None) is None

    def test_str_to_dt_round_trip(self):
        dt = datetime(2026, 4, 1, 15, 30, 0, tzinfo=timezone.utc)
        assert _str_to_dt(_dt_to_str(dt)) == dt

    def test_str_to_dt_none(self):
        assert _str_to_dt(None) is None

    def test_str_to_dt_z_suffix(self):
        result = _str_to_dt("2026-04-01T12:00:00Z")
        assert result.year == 2026
        assert result.tzinfo is not None
