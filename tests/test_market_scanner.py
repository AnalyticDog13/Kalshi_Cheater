"""
Tests for src/market_scanner.py

All tests use mocks — no Kalshi API, no Open-Meteo, no file I/O.
Run with: pytest tests/test_market_scanner.py -v
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.market_scanner import (
    MarketScanner,
    ScanResult,
    ScanSummary,
    parse_close_time,
    parse_direction_from_text,
    parse_measurement_date_from_ticker,
    parse_threshold_from_text,
    parse_threshold_from_ticker,
)
from src.edge_calculator import EdgeParams
from src.risk_manager import RiskLimits, RiskManager


# ---------------------------------------------------------------------------
# Helpers: build minimal mock objects
# ---------------------------------------------------------------------------

def _mock_market(
    ticker: str = "KXHIGHNY-26MAR31-T75",
    title: str = "Will NYC high exceed 75°F on Mar 31?",
    subtitle: str = "",
    yes_bid: str = "0.40",
    yes_ask: str = "0.46",
    volume_fp: str = "200",
    status: str = "open",
    close_time: str = "2026-03-31T23:59:59Z",
) -> MagicMock:
    m = MagicMock()
    m.ticker = ticker
    m.title = title
    m.subtitle = subtitle
    m.yes_bid_dollars = yes_bid
    m.yes_ask_dollars = yes_ask
    m.volume_fp = volume_fp
    m.status = status
    m.close_time = close_time
    return m


def _make_scanner(
    series_tickers: list[str] | None = None,
    station_mapping: dict | None = None,
    limits: RiskLimits | None = None,
    params: EdgeParams | None = None,
) -> MarketScanner:
    """Build a MarketScanner with mock dependencies."""
    from src.probability_engine import ProbabilityEngine

    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=[])
    kalshi.get_series = AsyncMock(return_value=_mock_series())

    prob_engine = MagicMock(spec=ProbabilityEngine)
    prob_engine.estimate_multiple = AsyncMock(return_value={})

    return MarketScanner(
        kalshi_client=kalshi,
        probability_engine=prob_engine,
        edge_params=params or EdgeParams(),
        risk_limits=limits or RiskLimits(),
        station_mapping=station_mapping or _default_station_mapping(),
        series_tickers=series_tickers or ["KXHIGHNY"],
    )


def _mock_series(fee_multiplier: float = 0.07) -> MagicMock:
    s = MagicMock()
    s.fee_multiplier = fee_multiplier
    return s


def _default_station_mapping() -> dict:
    return {
        "KXHIGHNY": {
            "station_id": "KNYC",
            "latitude": 40.7789,
            "longitude": -73.9692,
            "city": "New York",
        }
    }


def _mock_ensemble(
    ticker: str = "KXHIGHNY-26MAR31-T75",
    probability: float = 0.75,
    total_members: int = 139,
    ensemble_spread: float = 5.0,
    confidence: str = "high",
) -> MagicMock:
    """Return a mock EnsembleResult-like object."""
    e = MagicMock()
    e.probability = probability
    e.total_members = total_members
    e.ensemble_spread = ensemble_spread
    e.confidence = confidence
    e.is_usable = total_members >= 30
    return e


def _run(coro):
    """Run an async coroutine in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# parse_threshold_from_ticker
# ---------------------------------------------------------------------------

class TestParseThresholdFromTicker:
    def test_standard_format_with_dash(self):
        assert parse_threshold_from_ticker("KXHIGHNY-26MAR31-T75") == 75.0

    def test_standard_format_different_threshold(self):
        assert parse_threshold_from_ticker("KXHIGHCHI-26APR01-T80") == 80.0

    def test_three_digit_threshold(self):
        assert parse_threshold_from_ticker("KXHIGHPHX-26JUL04-T105") == 105.0

    def test_underscore_separator(self):
        assert parse_threshold_from_ticker("KXHIGHNY_26MAR31_T75") == 75.0

    def test_no_separator_at_end(self):
        # Ticker without trailing separator after T-value
        assert parse_threshold_from_ticker("KXHIGHNY26MAR31T75") == 75.0

    def test_case_insensitive(self):
        assert parse_threshold_from_ticker("KXHIGHNY-26MAR31-t75") == 75.0

    def test_no_threshold_returns_none(self):
        assert parse_threshold_from_ticker("KXHIGHNY-26MAR31") is None

    def test_empty_string(self):
        assert parse_threshold_from_ticker("") is None

    def test_threshold_zero(self):
        # Unusual but valid (32°F = freezing)
        assert parse_threshold_from_ticker("KXHIGHNY-26JAN15-T32") == 32.0


# ---------------------------------------------------------------------------
# parse_threshold_from_text
# ---------------------------------------------------------------------------

class TestParseThresholdFromText:
    def test_degree_symbol_f(self):
        assert parse_threshold_from_text("Will NYC exceed 75°F?") == 75.0

    def test_space_before_f(self):
        assert parse_threshold_from_text("High above 80 F on Tuesday") == 80.0

    def test_fahrenheit_word(self):
        assert parse_threshold_from_text("temperature of 72 Fahrenheit") == 72.0

    def test_above_pattern(self):
        assert parse_threshold_from_text("NYC high above 85") == 85.0

    def test_exceed_pattern(self):
        assert parse_threshold_from_text("Will NYC high exceed 78") == 78.0

    def test_exceeds_pattern(self):
        assert parse_threshold_from_text("temp exceeds 90 degrees") == 90.0

    def test_greater_than_pattern(self):
        assert parse_threshold_from_text("greater than 65") == 65.0

    def test_gt_symbol_pattern(self):
        assert parse_threshold_from_text("> 70 on Thursday") == 70.0

    def test_empty_string(self):
        assert parse_threshold_from_text("") is None

    def test_no_match(self):
        assert parse_threshold_from_text("Will it rain in New York?") is None

    def test_combined_title_subtitle(self):
        # Simulates how caller concatenates title + " " + subtitle
        combined = "NYC Weather Market  above 77"
        assert parse_threshold_from_text(combined) == 77.0

    # Verified live Kalshi title formats
    def test_verified_above_format(self):
        # "Will the **high temp in NYC** be >79° on Apr 1, 2026?"
        assert parse_threshold_from_text(
            "Will the **high temp in NYC** be >79\u00b0 on Apr 1, 2026?"
        ) == 79.0

    def test_verified_below_format(self):
        # "Will the **high temp in NYC** be <72° on Apr 1, 2026?"
        assert parse_threshold_from_text(
            "Will the **high temp in NYC** be <72\u00b0 on Apr 1, 2026?"
        ) == 72.0

    def test_operator_no_space(self):
        # >79° with no space between > and digit
        assert parse_threshold_from_text(">79\u00b0") == 79.0

    def test_lt_operator(self):
        assert parse_threshold_from_text("<72\u00b0") == 72.0


# ---------------------------------------------------------------------------
# parse_measurement_date_from_ticker
# ---------------------------------------------------------------------------

class TestParseMeasurementDateFromTicker:
    def test_verified_format(self):
        # Confirmed live: KXHIGHNY-26APR01-T79
        assert parse_measurement_date_from_ticker("KXHIGHNY-26APR01-T79") == date(2026, 4, 1)

    def test_march_date(self):
        assert parse_measurement_date_from_ticker("KXHIGHNY-26MAR31-T75") == date(2026, 3, 31)

    def test_date_at_end_of_ticker_no_trailing_separator(self):
        # Ticker ends with date (no T suffix): KXHIGHNY-26MAR31
        assert parse_measurement_date_from_ticker("KXHIGHNY-26MAR31") == date(2026, 3, 31)

    def test_december(self):
        assert parse_measurement_date_from_ticker("KXHIGHCHI-26DEC25-T45") == date(2026, 12, 25)

    def test_january(self):
        assert parse_measurement_date_from_ticker("KXHIGHNY-27JAN01-T32") == date(2027, 1, 1)

    def test_underscore_separator(self):
        assert parse_measurement_date_from_ticker("KXHIGHNY_26APR01_T79") == date(2026, 4, 1)

    def test_case_insensitive(self):
        assert parse_measurement_date_from_ticker("KXHIGHNY-26apr01-T79") == date(2026, 4, 1)

    def test_no_date_returns_none(self):
        assert parse_measurement_date_from_ticker("KXHIGHNY") is None

    def test_empty_string(self):
        assert parse_measurement_date_from_ticker("") is None


# ---------------------------------------------------------------------------
# parse_direction_from_text
# ---------------------------------------------------------------------------

class TestParseDirectionFromText:
    def test_above_market_default(self):
        # >79° → "above"
        assert parse_direction_from_text(
            "Will the **high temp in NYC** be >79\u00b0 on Apr 1, 2026?"
        ) == "above"

    def test_below_market(self):
        # <72° → "below"
        assert parse_direction_from_text(
            "Will the **high temp in NYC** be <72\u00b0 on Apr 1, 2026?"
        ) == "below"

    def test_lt_symbol_without_degree(self):
        assert parse_direction_from_text("be <70 on Tuesday") == "below"

    def test_no_operator_defaults_to_above(self):
        assert parse_direction_from_text("Will it be warm in NYC?") == "above"

    def test_empty_string_defaults_to_above(self):
        assert parse_direction_from_text("") == "above"

    def test_gt_symbol_is_above(self):
        assert parse_direction_from_text(">80 degrees") == "above"


# ---------------------------------------------------------------------------
# parse_close_time
# ---------------------------------------------------------------------------

class TestParseCloseTime:
    def test_iso_with_z(self):
        dt = parse_close_time("2026-03-31T23:59:59Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 31

    def test_iso_with_offset(self):
        dt = parse_close_time("2026-03-31T23:59:59+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_iso_without_seconds(self):
        dt = parse_close_time("2026-03-31T23:59")
        assert dt is not None
        assert dt.minute == 59

    def test_datetime_object_with_tz(self):
        original = datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc)
        dt = parse_close_time(original)
        assert dt == original

    def test_datetime_object_without_tz(self):
        original = datetime(2026, 3, 31, 23, 59)
        dt = parse_close_time(original)
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_none_returns_none(self):
        assert parse_close_time(None) is None

    def test_empty_string_returns_none(self):
        assert parse_close_time("") is None

    def test_garbage_string_returns_none(self):
        assert parse_close_time("not-a-date") is None

    def test_verified_sdk_format_space_separator(self):
        # Confirmed live: SDK returns datetime object "2026-04-02 04:59:00+00:00"
        # Test the string form (fromisoformat handles space separator in Python 3.11+
        # but our explicit fallback handles it too)
        dt = parse_close_time("2026-04-02 04:59:00+00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 2
        assert dt.hour == 4

    def test_sdk_returns_datetime_object_directly(self):
        # The real SDK returns a datetime object, not a string
        sdk_dt = datetime(2026, 4, 2, 4, 59, 0, tzinfo=timezone.utc)
        dt = parse_close_time(sdk_dt)
        assert dt == sdk_dt


# ---------------------------------------------------------------------------
# MarketScanner._compute_spread / _passes_liquidity_filter
# ---------------------------------------------------------------------------

class TestLiquidityFilter:
    def setup_method(self):
        self.scanner = _make_scanner()

    def test_normal_spread(self):
        m = _mock_market(yes_bid="0.40", yes_ask="0.46")
        assert self.scanner._compute_spread(m) == pytest.approx(0.06)

    def test_spread_zero_bid(self):
        m = _mock_market(yes_bid="0.00", yes_ask="0.50")
        assert self.scanner._compute_spread(m) == pytest.approx(0.50)

    def test_passes_liquidity_good_market(self):
        # volume=200 (>50), spread=0.06 (<0.08)
        m = _mock_market(yes_bid="0.40", yes_ask="0.46", volume_fp="200")
        assert self.scanner._passes_liquidity_filter(m) is True

    def test_fails_low_volume(self):
        m = _mock_market(volume_fp="10")
        assert self.scanner._passes_liquidity_filter(m) is False

    def test_fails_high_spread(self):
        # spread = 0.46 - 0.30 = 0.16 > 0.08
        m = _mock_market(yes_bid="0.30", yes_ask="0.46", volume_fp="200")
        assert self.scanner._passes_liquidity_filter(m) is False

    def test_boundary_volume_exactly_min(self):
        # volume=50 exactly — should pass (>=)
        m = _mock_market(volume_fp="50", yes_bid="0.40", yes_ask="0.46")
        assert self.scanner._passes_liquidity_filter(m) is True

    def test_boundary_spread_exactly_max(self):
        # spread=0.08 exactly — should pass (<=)
        m = _mock_market(yes_bid="0.40", yes_ask="0.48", volume_fp="200")
        assert self.scanner._passes_liquidity_filter(m) is True


# ---------------------------------------------------------------------------
# MarketScanner._hours_to_resolution
# ---------------------------------------------------------------------------

class TestHoursToResolution:
    def setup_method(self):
        self.scanner = _make_scanner()

    def test_future_resolution(self):
        future = datetime.now(timezone.utc) + timedelta(hours=24)
        m = _mock_market(close_time=future.isoformat())
        hours = self.scanner._hours_to_resolution(m)
        assert hours is not None
        assert 23.9 < hours < 24.1

    def test_past_resolution_returns_negative(self):
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        m = _mock_market(close_time=past.isoformat())
        hours = self.scanner._hours_to_resolution(m)
        assert hours is not None
        assert hours < 0

    def test_no_close_time_returns_none(self):
        m = MagicMock()
        m.close_time = None
        assert self.scanner._hours_to_resolution(m) is None

    def test_unparseable_close_time_returns_none(self):
        m = _mock_market(close_time="not-a-date")
        assert self.scanner._hours_to_resolution(m) is None


# ---------------------------------------------------------------------------
# MarketScanner._parse_market_info
# ---------------------------------------------------------------------------

class TestParseMarketInfo:
    def setup_method(self):
        self.scanner = _make_scanner()

    def test_parses_threshold_from_ticker(self):
        m = _mock_market(
            ticker="KXHIGHNY-26APR01-T79",
            title="Will the **high temp in NYC** be >79\u00b0 on Apr 1, 2026?",
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is not None
        assert info.threshold_value == 79.0
        assert info.resolution_date == date(2026, 4, 1)  # from ticker, NOT close_time
        assert info.series_ticker == "KXHIGHNY"
        assert direction == "above"

    def test_below_market_direction(self):
        m = _mock_market(
            ticker="KXHIGHNY-26APR01-T72",
            title="Will the **high temp in NYC** be <72\u00b0 on Apr 1, 2026?",
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is not None
        assert info.threshold_value == 72.0
        assert info.resolution_date == date(2026, 4, 1)
        assert direction == "below"

    def test_resolution_date_from_ticker_not_close_time(self):
        # CRITICAL: close_time is Apr 2 (day AFTER measurement), ticker says Apr 1.
        # resolution_date must be Apr 1, not Apr 2.
        m = _mock_market(
            ticker="KXHIGHNY-26APR01-T79",
            title="Will the **high temp in NYC** be >79\u00b0 on Apr 1, 2026?",
            close_time="2026-04-02T04:59:00Z",  # day after — must NOT use this for date
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is not None
        assert info.resolution_date == date(2026, 4, 1)  # from ticker
        assert info.resolution_date != date(2026, 4, 2)  # NOT from close_time

    def test_falls_back_to_title_if_no_T_suffix(self):
        # Ticker has parseable date but no T-suffix → threshold from title
        m = _mock_market(
            ticker="KXHIGHNY-26MAR31",
            title="Will NYC high exceed 80\u00b0F on Mar 31?",
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is not None
        assert info.threshold_value == 80.0
        assert info.resolution_date == date(2026, 3, 31)

    def test_returns_none_if_no_threshold(self):
        m = _mock_market(
            ticker="KXHIGHNY-26MAR31",
            title="Will it be warm in NYC?",
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is None

    def test_returns_none_if_no_parseable_date_in_ticker(self):
        # Ticker with no date segment at all → can't determine measurement date
        m = _mock_market(
            ticker="KXHIGHNY-T79",
            title="Will the **high temp in NYC** be >79\u00b0?",
        )
        info, direction = self.scanner._parse_market_info(m, "KXHIGHNY")
        assert info is None


# ---------------------------------------------------------------------------
# MarketScanner.scan() — full pipeline integration
# ---------------------------------------------------------------------------

class TestScanFull:
    """
    Integration tests for the full scan() pipeline using mocked I/O.
    All Kalshi API calls and ensemble fetches are mocked.
    """

    def _build_scanner_with_markets(self, markets, probability=0.75):
        """Build a scanner that returns the given markets and a fixed ensemble probability."""
        from src.probability_engine import ProbabilityEngine

        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(return_value=markets)
        kalshi.get_series = AsyncMock(return_value=_mock_series(0.07))

        # Build ensemble results for each market
        ensemble_map = {}
        for m in markets:
            ens = _mock_ensemble(ticker=m.ticker, probability=probability)
            ensemble_map[m.ticker] = ens

        prob_engine = MagicMock(spec=ProbabilityEngine)
        prob_engine.estimate_multiple = AsyncMock(return_value=ensemble_map)

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=prob_engine,
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping=_default_station_mapping(),
            series_tickers=["KXHIGHNY"],
        )
        return scanner

    def _make_risk_manager(self) -> RiskManager:
        return RiskManager(RiskLimits())

    def test_approved_market_appears_in_results(self):
        # Market with clear edge: ensemble=0.75, market=0.60 → 15pp edge
        m = _mock_market(
            ticker="KXHIGHNY-26MAR31-T75",
            yes_bid="0.58",
            yes_ask="0.62",   # mid = 0.60, spread = 0.04 (passes)
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        scanner = self._build_scanner_with_markets([m], probability=0.75)
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.total_fetched == 1
        assert summary.evaluated == 1
        assert len(results) == 1
        assert results[0].market_ticker == "KXHIGHNY-26MAR31-T75"
        assert results[0].city == "New York"
        assert results[0].fee_coefficient == pytest.approx(0.07)

    def test_no_edge_market_is_evaluated_but_rejected(self):
        # ensemble ≈ market → tiny edge → risk check rejects it
        m = _mock_market(
            ticker="KXHIGHNY-26MAR31-T75",
            yes_bid="0.74",
            yes_ask="0.76",   # mid = 0.75, spread = 0.02 (passes)
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        scanner = self._build_scanner_with_markets([m], probability=0.75)
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert len(results) == 1
        assert not results[0].risk.approved   # rejected due to insufficient edge
        assert results[0].edge.recommendation == "NO_TRADE"

    def test_low_volume_market_filtered_before_evaluation(self):
        m = _mock_market(volume_fp="5")  # below min_volume_fp=50
        scanner = self._build_scanner_with_markets([m])
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_liquidity == 1
        assert summary.evaluated == 0
        assert len(results) == 0

    def test_wide_spread_market_filtered(self):
        m = _mock_market(yes_bid="0.30", yes_ask="0.60")  # spread = 0.30 > 0.08
        scanner = self._build_scanner_with_markets([m])
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_liquidity == 1
        assert len(results) == 0

    def test_too_soon_market_filtered(self):
        m = _mock_market(
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=3)  # <6h
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_fp="200",
        )
        scanner = self._build_scanner_with_markets([m])
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_time == 1
        assert len(results) == 0

    def test_too_far_market_filtered(self):
        m = _mock_market(
            close_time=(
                datetime.now(timezone.utc) + timedelta(days=10)  # >7 days
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_fp="200",
        )
        scanner = self._build_scanner_with_markets([m])
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_time == 1
        assert len(results) == 0

    def test_unknown_series_ticker_filtered_no_station(self):
        from src.probability_engine import ProbabilityEngine

        # Use a future close_time so the market passes the time filter and
        # reaches the station-mapping check.
        future_market = _mock_market(
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_fp="200",
        )
        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(return_value=[future_market])
        kalshi.get_series = AsyncMock(return_value=_mock_series())
        prob_engine = MagicMock(spec=ProbabilityEngine)
        prob_engine.estimate_multiple = AsyncMock(return_value={})

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=prob_engine,
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping={},  # no mappings at all
            series_tickers=["KXHIGHNY"],
        )
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_no_station >= 1
        assert len(results) == 0

    def test_unparseable_threshold_filtered(self):
        m = _mock_market(
            ticker="KXHIGHNY-26MAR31",       # no T-suffix
            title="Will it be warm in NYC?", # no threshold in title
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            volume_fp="200",
        )
        scanner = self._build_scanner_with_markets([m])
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.filtered_parse_error == 1
        assert len(results) == 0

    def test_multiple_markets_same_series(self):
        """Two markets at same station → one ensemble fetch, two results."""
        markets = [
            _mock_market(
                ticker="KXHIGHNY-26MAR31-T75",
                yes_bid="0.58", yes_ask="0.62",
                volume_fp="200",
                close_time=(
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
            _mock_market(
                ticker="KXHIGHNY-26MAR31-T80",
                yes_bid="0.28", yes_ask="0.32",
                volume_fp="150",
                close_time=(
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        ]

        from src.probability_engine import ProbabilityEngine
        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(return_value=markets)
        kalshi.get_series = AsyncMock(return_value=_mock_series())

        ensemble_map = {
            "KXHIGHNY-26MAR31-T75": _mock_ensemble("KXHIGHNY-26MAR31-T75", probability=0.75),
            "KXHIGHNY-26MAR31-T80": _mock_ensemble("KXHIGHNY-26MAR31-T80", probability=0.45),
        }
        prob_engine = MagicMock(spec=ProbabilityEngine)
        prob_engine.estimate_multiple = AsyncMock(return_value=ensemble_map)

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=prob_engine,
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping=_default_station_mapping(),
            series_tickers=["KXHIGHNY"],
        )
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert summary.evaluated == 2
        assert len(results) == 2
        # estimate_multiple called exactly once (batch)
        prob_engine.estimate_multiple.assert_awaited_once()

    def test_fee_coefficient_fetched_from_series(self):
        m = _mock_market(
            yes_bid="0.58", yes_ask="0.62",
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        from src.probability_engine import ProbabilityEngine
        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(return_value=[m])
        kalshi.get_series = AsyncMock(return_value=_mock_series(fee_multiplier=0.09))

        prob_engine = MagicMock(spec=ProbabilityEngine)
        prob_engine.estimate_multiple = AsyncMock(
            return_value={m.ticker: _mock_ensemble(m.ticker, probability=0.75)}
        )

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=prob_engine,
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping=_default_station_mapping(),
            series_tickers=["KXHIGHNY"],
        )
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert len(results) == 1
        assert results[0].fee_coefficient == pytest.approx(0.09)

    def test_fee_coefficient_defaults_to_007_on_error(self):
        m = _mock_market(
            yes_bid="0.58", yes_ask="0.62",
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        from src.probability_engine import ProbabilityEngine
        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(return_value=[m])
        kalshi.get_series = AsyncMock(side_effect=Exception("API error"))

        prob_engine = MagicMock(spec=ProbabilityEngine)
        prob_engine.estimate_multiple = AsyncMock(
            return_value={m.ticker: _mock_ensemble(m.ticker, probability=0.75)}
        )

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=prob_engine,
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping=_default_station_mapping(),
            series_tickers=["KXHIGHNY"],
        )
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert len(results) == 1
        assert results[0].fee_coefficient == pytest.approx(0.07)

    def test_list_markets_error_returns_empty(self):
        from src.probability_engine import ProbabilityEngine
        kalshi = MagicMock()
        kalshi.list_markets = AsyncMock(side_effect=Exception("network error"))
        kalshi.get_series = AsyncMock(return_value=_mock_series())

        scanner = MarketScanner(
            kalshi_client=kalshi,
            probability_engine=MagicMock(spec=ProbabilityEngine),
            edge_params=EdgeParams(),
            risk_limits=RiskLimits(),
            station_mapping=_default_station_mapping(),
            series_tickers=["KXHIGHNY"],
        )
        rm = self._make_risk_manager()
        results, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert len(results) == 0
        assert summary.total_fetched == 0

    def test_summary_duration_is_positive(self):
        scanner = _make_scanner()
        rm = self._make_risk_manager()
        _, summary = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))
        assert summary.scan_duration_seconds >= 0.0

    def test_below_market_probability_flipped(self):
        """
        For a '<' market, YES wins when temp stays LOW.
        Ensemble gives P(max >= threshold) = 0.80 (very likely hot).
        For a 'below' market that means P(YES) = 1 - 0.80 = 0.20.
        The edge should be computed against 0.20, not 0.80.
        """
        m = _mock_market(
            ticker="KXHIGHNY-26APR01-T72",
            title="Will the **high temp in NYC** be <72\u00b0 on Apr 1, 2026?",
            yes_bid="0.18",
            yes_ask="0.22",   # mid = 0.20, spread = 0.04 (passes)
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        # ensemble says P(max >= 72) = 0.80 → for below market P(YES) = 0.20
        scanner = self._build_scanner_with_markets([m], probability=0.80)
        rm = self._make_risk_manager()
        results, _ = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))

        assert len(results) == 1
        r = results[0]
        assert r.market_direction == "below"
        # Raw ensemble stored unflipped (for calibration)
        assert r.ensemble.probability == pytest.approx(0.80)
        # Edge computed with flipped probability (≈0.20), market mid=0.20 → near-zero edge
        # The direction-adjusted fair value is 0.20, market is 0.20 → NO_TRADE
        assert r.edge.recommendation == "NO_TRADE"

    def test_above_market_direction_stored(self):
        """Above-market result has market_direction == 'above'."""
        m = _mock_market(
            ticker="KXHIGHNY-26APR01-T79",
            title="Will the **high temp in NYC** be >79\u00b0 on Apr 1, 2026?",
            yes_bid="0.58", yes_ask="0.62",
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        scanner = self._build_scanner_with_markets([m], probability=0.75)
        rm = self._make_risk_manager()
        results, _ = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))
        assert len(results) == 1
        assert results[0].market_direction == "above"

    def test_ai_reasoner_none_does_not_crash(self):
        """Scanner with ai_reasoner=None should complete without error."""
        m = _mock_market(
            yes_bid="0.58", yes_ask="0.62",
            volume_fp="200",
            close_time=(
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        scanner = self._build_scanner_with_markets([m], probability=0.75)
        assert scanner.ai_reasoner is None  # default stub
        rm = self._make_risk_manager()
        results, _ = _run(scanner.scan(
            portfolio_balance=1000.0,
            open_orders_count=0,
            risk_manager=rm,
        ))
        assert isinstance(results, list)  # completed without crash
