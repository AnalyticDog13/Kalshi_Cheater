"""
Tests for src/health_monitor.py.

Uses a short cooldown (0.1s) so tests don't need to sleep 5 minutes.
"""

from __future__ import annotations

import time
import pytest
from datetime import datetime, timezone, timedelta

from src.health_monitor import (
    HealthMonitor,
    BreakerState,
    BreakerStatus,
    FAILURE_THRESHOLD,
    COOLDOWN_SECONDS,
    KNOWN_SERVICES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monitor(threshold: int = 3, cooldown: float = 60.0) -> HealthMonitor:
    return HealthMonitor(failure_threshold=threshold, cooldown_seconds=cooldown)


def _fast_monitor(threshold: int = 3) -> HealthMonitor:
    """Monitor with a very short cooldown so HALF-OPEN transitions are testable."""
    return HealthMonitor(failure_threshold=threshold, cooldown_seconds=0.05)


def _trip(monitor: HealthMonitor, service: str, n: int = None) -> bool:
    """Fire n failures (default: threshold). Returns result of last record_failure."""
    n = n or monitor._threshold
    result = False
    for i in range(n):
        result = monitor.record_failure(service, reason=f"err-{i}")
    return result


# ---------------------------------------------------------------------------
# Constants and init
# ---------------------------------------------------------------------------

class TestInit:
    def test_all_known_services_present(self):
        m = _monitor()
        assert set(m._breakers.keys()) == KNOWN_SERVICES

    def test_all_breakers_start_closed(self):
        m = _monitor()
        for svc in KNOWN_SERVICES:
            assert not m.is_open(svc)

    def test_all_healthy_at_start(self):
        assert _monitor().all_healthy()

    def test_known_services_contains_required(self):
        assert "kalshi_api" in KNOWN_SERVICES
        assert "open_meteo" in KNOWN_SERVICES
        assert "noaa"       in KNOWN_SERVICES

    def test_default_threshold(self):
        m = HealthMonitor()
        assert m._threshold == FAILURE_THRESHOLD

    def test_default_cooldown(self):
        m = HealthMonitor()
        assert m._cooldown == COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# record_failure / is_open — basic tripping
# ---------------------------------------------------------------------------

class TestRecordFailure:
    def test_single_failure_does_not_trip(self):
        m = _monitor(threshold=3)
        tripped = m.record_failure("kalshi_api", "timeout")
        assert tripped is False
        assert not m.is_open("kalshi_api")

    def test_two_failures_do_not_trip(self):
        m = _monitor(threshold=3)
        m.record_failure("kalshi_api", "err")
        tripped = m.record_failure("kalshi_api", "err")
        assert tripped is False
        assert not m.is_open("kalshi_api")

    def test_threshold_failures_trip(self):
        m = _monitor(threshold=3)
        tripped = _trip(m, "kalshi_api", 3)
        assert tripped is True
        assert m.is_open("kalshi_api")

    def test_returns_true_only_on_exact_trip(self):
        m = _monitor(threshold=3)
        results = [m.record_failure("kalshi_api", f"err-{i}") for i in range(5)]
        # Only the 3rd failure should return True
        assert results == [False, False, True, False, False]

    def test_different_services_independent(self):
        m = _monitor(threshold=3)
        _trip(m, "open_meteo", 3)
        assert m.is_open("open_meteo")
        assert not m.is_open("kalshi_api")
        assert not m.is_open("noaa")

    def test_unknown_service_returns_false(self):
        m = _monitor()
        result = m.record_failure("bitcoin_oracle", "err")
        assert result is False

    def test_unknown_service_is_open_returns_false(self):
        m = _monitor()
        assert m.is_open("bitcoin_oracle") is False

    def test_failure_stores_reason(self):
        m = _monitor(threshold=3)
        _trip(m, "noaa", 3)
        status = m.get_status()["noaa"]
        assert "err-2" in (status.last_failure_reason or "")

    def test_failure_increments_total_trips(self):
        m = _monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        assert m.get_status()["kalshi_api"].total_trips == 1

    def test_consecutive_failures_tracked(self):
        m = _monitor(threshold=5)
        for i in range(3):
            m.record_failure("open_meteo", "err")
        assert m.get_status()["open_meteo"].consecutive_failures == 3


# ---------------------------------------------------------------------------
# record_success — reset behavior
# ---------------------------------------------------------------------------

class TestRecordSuccess:
    def test_success_before_trip_resets_counter(self):
        m = _monitor(threshold=3)
        m.record_failure("kalshi_api", "err")
        m.record_failure("kalshi_api", "err")
        m.record_success("kalshi_api")
        # Counter reset: need 3 more failures to trip again
        m.record_failure("kalshi_api", "err")
        m.record_failure("kalshi_api", "err")
        assert not m.is_open("kalshi_api")

    def test_success_after_trip_closes_breaker(self):
        m = _monitor(threshold=3)
        _trip(m, "kalshi_api")
        assert m.is_open("kalshi_api")
        m.record_success("kalshi_api")
        assert not m.is_open("kalshi_api")

    def test_success_resets_failure_count(self):
        m = _monitor(threshold=3)
        m.record_failure("open_meteo", "err")
        m.record_success("open_meteo")
        assert m.get_status()["open_meteo"].consecutive_failures == 0

    def test_success_on_closed_breaker_is_noop(self):
        m = _monitor()
        m.record_success("noaa")  # must not raise
        assert not m.is_open("noaa")

    def test_success_unknown_service_is_noop(self):
        m = _monitor()
        m.record_success("unknown_service")  # must not raise

    def test_total_trips_not_decremented_by_success(self):
        m = _monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        m.record_success("kalshi_api")
        assert m.get_status()["kalshi_api"].total_trips == 1

    def test_second_trip_increments_total_trips(self):
        m = _monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        m.record_success("kalshi_api")
        _trip(m, "kalshi_api", 2)
        assert m.get_status()["kalshi_api"].total_trips == 2


# ---------------------------------------------------------------------------
# HALF-OPEN / cooldown transitions
# ---------------------------------------------------------------------------

class TestCooldownAndHalfOpen:
    def test_breaker_stays_open_during_cooldown(self):
        m = _monitor(threshold=2, cooldown=60.0)
        _trip(m, "open_meteo", 2)
        assert m.is_open("open_meteo")

    def test_breaker_allows_call_after_cooldown(self):
        m = _fast_monitor(threshold=2)
        _trip(m, "open_meteo", 2)
        time.sleep(0.1)  # fast cooldown elapsed
        assert not m.is_open("open_meteo")

    def test_state_is_half_open_after_cooldown(self):
        m = _fast_monitor(threshold=2)
        _trip(m, "open_meteo", 2)
        time.sleep(0.1)
        m.is_open("open_meteo")  # trigger transition
        status = m.get_status()["open_meteo"]
        assert status.state == BreakerState.HALF_OPEN

    def test_success_in_half_open_closes_breaker(self):
        m = _fast_monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        time.sleep(0.1)
        assert not m.is_open("kalshi_api")  # HALF-OPEN
        m.record_success("kalshi_api")
        assert not m.is_open("kalshi_api")
        assert m.get_status()["kalshi_api"].state == BreakerState.CLOSED

    def test_failure_in_half_open_reopens_breaker(self):
        m = _fast_monitor(threshold=2)
        _trip(m, "noaa", 2)
        time.sleep(0.1)
        assert not m.is_open("noaa")  # HALF-OPEN: test call allowed
        m.record_failure("noaa", "still down")
        assert m.is_open("noaa")  # back to OPEN

    def test_failure_in_half_open_refreshes_cooldown(self):
        m = _fast_monitor(threshold=2)
        _trip(m, "noaa", 2)
        time.sleep(0.1)
        m.is_open("noaa")  # trigger HALF-OPEN
        m.record_failure("noaa", "still down")
        # Immediately after re-trip, should still be open (fresh cooldown)
        assert m.is_open("noaa")


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_returns_all_services(self):
        m = _monitor()
        status = m.get_status()
        assert set(status.keys()) == KNOWN_SERVICES

    def test_returns_breaker_status_objects(self):
        m = _monitor()
        for svc, st in m.get_status().items():
            assert isinstance(st, BreakerStatus)

    def test_closed_status_fields(self):
        m = _monitor()
        st = m.get_status()["kalshi_api"]
        assert st.state == BreakerState.CLOSED
        assert st.consecutive_failures == 0
        assert st.tripped_at is None
        assert st.total_trips == 0

    def test_open_status_has_tripped_at(self):
        m = _monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        st = m.get_status()["kalshi_api"]
        assert st.state == BreakerState.OPEN
        assert st.tripped_at is not None
        assert isinstance(st.tripped_at, datetime)

    def test_status_service_name_matches(self):
        m = _monitor()
        for svc, st in m.get_status().items():
            assert st.service == svc


# ---------------------------------------------------------------------------
# reset (manual)
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_closes_open_breaker(self):
        m = _monitor(threshold=2)
        _trip(m, "noaa", 2)
        assert m.is_open("noaa")
        m.reset("noaa")
        assert not m.is_open("noaa")

    def test_reset_unknown_service_is_noop(self):
        m = _monitor()
        m.reset("nonexistent")  # must not raise

    def test_reset_allows_retrip_after_fresh_failures(self):
        m = _monitor(threshold=2)
        _trip(m, "kalshi_api", 2)
        m.reset("kalshi_api")
        tripped = _trip(m, "kalshi_api", 2)
        assert tripped is True


# ---------------------------------------------------------------------------
# all_healthy
# ---------------------------------------------------------------------------

class TestAllHealthy:
    def test_true_when_all_closed(self):
        assert _monitor().all_healthy()

    def test_false_when_one_open(self):
        m = _monitor(threshold=2)
        _trip(m, "open_meteo", 2)
        assert not m.all_healthy()

    def test_true_after_recovery(self):
        m = _monitor(threshold=2)
        _trip(m, "noaa", 2)
        m.record_success("noaa")
        assert m.all_healthy()
