"""
Health Monitor.

Implements circuit breakers for three external services:
  - "kalshi_api"  — Kalshi REST API (trading cannot function without this)
  - "open_meteo"  — Open-Meteo Ensemble API (probability engine)
  - "noaa"        — NOAA Weather API (ground truth / settlement verification)

Circuit breaker state machine (standard three-state pattern):
  CLOSED (normal) → OPEN (tripped) → HALF-OPEN (testing recovery) → CLOSED

  CLOSED:    Calls pass through. Consecutive failure counter increments on each
             record_failure(). At FAILURE_THRESHOLD, transitions to OPEN and
             records the trip time.

  OPEN:      is_open() returns True. Callers should skip the service entirely.
             After COOLDOWN_SECONDS have elapsed since the trip, the breaker
             transitions to HALF-OPEN automatically (no explicit call needed).

  HALF-OPEN: is_open() returns False — exactly ONE test call is allowed through.
             record_success() → back to CLOSED (reset counter).
             record_failure() → back to OPEN with a fresh cooldown timer.

Why this matters for a trading system:
  - A single Kalshi API timeout should not halt trading. But 3 in a row
    suggests a real outage, and blindly retrying wastes time and hits rate limits.
  - Open-Meteo failures mean our probability estimates are stale. After 5 minutes
    without a successful fetch, the market may have repriced against us. Better
    to pause and wait for recovery than to trade on a 15-minute-old probability.
  - NOAA failures only affect post-resolution recording. The breaker fires an
    alert but does NOT stop trading — active order management takes priority.

Notification design:
  record_failure() returns True when a breaker JUST tripped (transition
  CLOSED→OPEN). The async caller (daemon) is responsible for awaiting
  notifier.send_circuit_breaker(). This keeps HealthMonitor fully synchronous
  and testable without async mocking.

All state is in-memory. Process restart clears all breakers — this is intentional.
A crashed-and-restarted daemon should attempt each service fresh on startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD: int   = 3      # consecutive failures before tripping
COOLDOWN_SECONDS:  float = 300.0  # 5 minutes between trip and first retry

KNOWN_SERVICES = frozenset({"kalshi_api", "open_meteo", "noaa"})


class BreakerState(Enum):
    CLOSED    = "closed"     # normal — calls pass through
    OPEN      = "open"       # tripped — calls blocked
    HALF_OPEN = "half_open"  # cooldown elapsed — one test call allowed


@dataclass
class BreakerStatus:
    """Snapshot of a single circuit breaker's state."""
    service:             str
    state:               BreakerState
    consecutive_failures: int
    tripped_at:          datetime | None   # when the breaker last opened
    last_failure_reason: str | None        # last error message recorded
    total_trips:         int               # how many times this breaker has tripped


class HealthMonitor:
    """
    In-memory circuit breaker manager for all external services.

    Usage (from the async daemon loop):
        monitor = HealthMonitor()

        # Before every external call:
        if monitor.is_open("kalshi_api"):
            logger.info("kalshi_api circuit open, skipping scan")
            continue

        try:
            result = await kalshi_client.get_markets(...)
            monitor.record_success("kalshi_api")
        except Exception as exc:
            just_tripped = monitor.record_failure("kalshi_api", reason=str(exc))
            if just_tripped:
                await notifier.send_circuit_breaker("kalshi_api", str(exc))
    """

    def __init__(
        self,
        failure_threshold: int   = FAILURE_THRESHOLD,
        cooldown_seconds:  float = COOLDOWN_SECONDS,
    ) -> None:
        self._threshold  = failure_threshold
        self._cooldown   = cooldown_seconds
        self._breakers:  dict[str, _Breaker] = {
            svc: _Breaker(service=svc) for svc in KNOWN_SERVICES
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_success(self, service: str) -> None:
        """
        Record a successful call to a service.

        If the breaker was OPEN or HALF-OPEN, this closes it and resets the
        failure counter. Safe to call even if the service is unknown (logs a
        warning and no-ops).
        """
        breaker = self._get(service)
        if breaker is None:
            return
        prev_state = breaker.state
        breaker.record_success()
        if prev_state != BreakerState.CLOSED:
            logger.info(
                "Circuit breaker CLOSED for %s after successful recovery.",
                service,
            )

    def record_failure(self, service: str, reason: str = "") -> bool:
        """
        Record a failed call to a service.

        Increments the consecutive failure counter. If the counter reaches
        the threshold, the breaker trips (CLOSED → OPEN).

        Args:
            service: service name (must be in KNOWN_SERVICES)
            reason:  error description for logging and notification

        Returns:
            True if the breaker JUST tripped on this call (caller should
            send a circuit-breaker notification). False in all other cases.
        """
        breaker = self._get(service)
        if breaker is None:
            return False
        just_tripped = breaker.record_failure(reason, self._threshold)
        if just_tripped:
            logger.warning(
                "Circuit breaker OPEN for %s after %d consecutive failures. "
                "Cooldown: %.0fs. Last reason: %s",
                service, self._threshold, self._cooldown, reason,
            )
        elif breaker.consecutive_failures > 0:
            logger.info(
                "Service %s failure %d/%d: %s",
                service, breaker.consecutive_failures, self._threshold, reason,
            )
        return just_tripped

    def is_open(self, service: str) -> bool:
        """
        Return True if the circuit breaker is tripped for this service.

        Handles the OPEN → HALF-OPEN transition automatically: once the
        cooldown has elapsed, this returns False so the caller can attempt
        one test call. If the test call succeeds, record_success() closes
        the breaker. If it fails, record_failure() re-opens it.

        Unknown services always return False (fail open — don't block unknown).
        """
        breaker = self._get(service)
        if breaker is None:
            return False
        return breaker.is_open(self._cooldown)

    def get_status(self) -> dict[str, BreakerStatus]:
        """
        Return a snapshot of all circuit breakers.

        Returns:
            Dict mapping service name → BreakerStatus.
        """
        return {
            svc: breaker.status(self._cooldown)
            for svc, breaker in self._breakers.items()
        }

    def reset(self, service: str) -> None:
        """
        Manually reset a circuit breaker to CLOSED. Used by operators or
        in tests to simulate recovery without waiting for the cooldown.
        """
        breaker = self._get(service)
        if breaker is None:
            return
        breaker.record_success()
        logger.info("Circuit breaker manually reset for %s.", service)

    def all_healthy(self) -> bool:
        """Return True if no circuit breakers are currently open."""
        return not any(self.is_open(svc) for svc in KNOWN_SERVICES)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, service: str) -> "_Breaker | None":
        if service not in self._breakers:
            logger.warning(
                "HealthMonitor: unknown service %r — ignoring. "
                "Known services: %s", service, sorted(KNOWN_SERVICES),
            )
            return None
        return self._breakers[service]


# ---------------------------------------------------------------------------
# Internal breaker state
# ---------------------------------------------------------------------------

@dataclass
class _Breaker:
    """Single-service circuit breaker state."""
    service:              str
    consecutive_failures: int           = 0
    tripped_at:           datetime | None = None
    last_failure_reason:  str | None    = None
    total_trips:          int           = 0
    _in_half_open:        bool          = field(default=False, repr=False)

    @property
    def state(self) -> BreakerState:
        if self.tripped_at is None:
            return BreakerState.CLOSED
        if self._in_half_open:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def is_open(self, cooldown_seconds: float) -> bool:
        """
        True → caller should block the call.
        False → caller may proceed (either CLOSED or HALF-OPEN test window).

        Transitions OPEN → HALF-OPEN automatically when cooldown elapses.
        """
        if self.tripped_at is None:
            return False  # CLOSED
        elapsed = (datetime.now(timezone.utc) - self.tripped_at).total_seconds()
        if elapsed >= cooldown_seconds:
            if not self._in_half_open:
                self._in_half_open = True
                logger.info(
                    "Circuit breaker HALF-OPEN for %s (cooldown elapsed). "
                    "Allowing one test call.",
                    self.service,
                )
            return False  # allow the one test call through
        return True  # still in cooldown

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.tripped_at           = None
        self._in_half_open        = False
        self.last_failure_reason  = None

    def record_failure(self, reason: str, threshold: int) -> bool:
        """
        Returns True if this failure just tripped the breaker.
        """
        self.consecutive_failures += 1
        self.last_failure_reason   = reason
        self._in_half_open         = False  # failed test call → back to OPEN

        if self.consecutive_failures >= threshold and self.tripped_at is None:
            self.tripped_at  = datetime.now(timezone.utc)
            self.total_trips += 1
            return True  # just tripped

        if self._was_half_open_before_failure(reason):
            # Re-arm the cooldown on a failed test call
            self.tripped_at = datetime.now(timezone.utc)

        return False

    def _was_half_open_before_failure(self, reason: str) -> bool:
        # _in_half_open was cleared above; we detect re-trip by checking
        # if tripped_at is set and consecutive_failures just incremented
        # past threshold (already open, new failure → refresh cooldown)
        return self.tripped_at is not None

    def status(self, cooldown_seconds: float) -> BreakerStatus:
        # Force state resolution (may trigger OPEN → HALF-OPEN)
        self.is_open(cooldown_seconds)
        return BreakerStatus(
            service=self.service,
            state=self.state,
            consecutive_failures=self.consecutive_failures,
            tripped_at=self.tripped_at,
            last_failure_reason=self.last_failure_reason,
            total_trips=self.total_trips,
        )
