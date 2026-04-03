"""
Market Scanner.

Discovers open weather markets on Kalshi, filters them by liquidity and
time-to-resolution, computes ensemble probability estimates, and produces
full edge + risk assessments for every candidate.

Pipeline per scan cycle:
  1. For each series ticker in config: list all open markets
  2. Filter: volume_fp >= min_volume_fp AND spread <= max_spread_dollars
  3. Filter: min_resolution_hours <= hours_to_resolution <= max_resolution_days * 24
  4. Batch-estimate ensemble probabilities per station (one API call per city)
  5. Compute EdgeAssessment via edge_calculator.compute_edge()
  6. Run RiskManager.check_trade() for each market
  7. (Stub) If edge.recommendation == "SANITY_CHECK" and ai_reasoner is set, call it

Returns ALL scan results — approved AND rejected. The caller (daemon) filters
on result.risk.approved and logs both for the dual-arm experimentation framework.

Design notes (verified against live Kalshi API 2026-03-31):
  - Kalshi ticker format: KXHIGHNY-26APR01-T79 (date=Apr 1 2026, threshold=79F)
  - Kalshi title format:  "Will the **high temp in NYC** be >79° on Apr 1, 2026?"
                         "Will the **high temp in NYC** be <72° on Apr 1, 2026?"
  - Kalshi close_time:   "2026-04-02 04:59:00+00:00" (day AFTER the measurement date)

  CRITICAL: MarketInfo.resolution_date = measurement date (from ticker), NOT close_time.date().
  close_time is the day AFTER the measurement. Using close_time.date() would cause the
  probability engine to forecast the wrong day's temperature.

  - Market direction: "above" (>°) or "below" (<°) detected from title. For "below"
    markets, the YES probability = 1 - P(max >= threshold). The scanner flips the
    ensemble probability before passing to compute_edge().

  - Fee coefficient fetched from series.fee_multiplier; defaults to 0.07.
  - ai_reasoner is injected as None (stub). Wire it up in Phase 2 step 4.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.kalshi_client import KalshiClient, market_mid_price, parse_price, parse_quantity
from src.data_feeds.weather_ensemble import EnsembleResult
from src.edge_calculator import EdgeAssessment, EdgeParams, compute_edge
from src.probability_engine import MarketInfo, ProbabilityEngine
from src.risk_manager import RiskCheckOutcome, RiskLimits, RiskManager
from src.ai_reasoner import AIReasoner  # noqa: E402 — imported after core modules

logger = logging.getLogger(__name__)

# Month abbreviation lookup for ticker date parsing
_MONTH_ABBR: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """
    Full assessment of one market from a single scan cycle.

    Contains everything the daemon needs to decide whether to trade:
      - The raw Kalshi market object (for order executor)
      - The parsed MarketInfo (for logging/tracking)
      - The EnsembleResult (raw, always P(max >= threshold))
      - The EdgeAssessment (recommended size, direction, fees)
      - The RiskCheckOutcome (approved/rejected with reasons)
      - market_direction: "above" or "below" — whether YES wins on high or low temp

    The daemon should check result.risk.approved before executing.
    Non-approved results are returned for the exploratory-arm log.
    """
    market_ticker: str
    series_ticker: str
    city: str
    hours_to_resolution: float
    volume_fp: float
    spread_dollars: float
    market_price: float           # YES mid-price at scan time
    fee_coefficient: float
    market_direction: str         # "above" (>°) or "below" (<°)
    market_info: MarketInfo
    ensemble: EnsembleResult      # raw ensemble — P(max >= threshold) before any flip
    edge: EdgeAssessment          # computed with direction-adjusted probability
    risk: RiskCheckOutcome
    market: Any                   # raw Kalshi SDK market object (for order executor)


@dataclass
class ScanSummary:
    """Aggregate stats for one full scan cycle — for logging and health monitoring."""
    total_fetched: int = 0
    filtered_liquidity: int = 0   # failed volume or spread check
    filtered_time: int = 0        # outside resolution time window
    filtered_no_station: int = 0  # series has no station mapping
    filtered_parse_error: int = 0 # could not parse threshold/date from ticker/title
    evaluated: int = 0            # reached edge + risk calculation
    approved: int = 0             # passed risk check (risk.approved == True)
    scan_duration_seconds: float = 0.0

    def __str__(self) -> str:
        return (
            f"ScanSummary: fetched={self.total_fetched} "
            f"liq_filtered={self.filtered_liquidity} "
            f"time_filtered={self.filtered_time} "
            f"no_station={self.filtered_no_station} "
            f"parse_err={self.filtered_parse_error} "
            f"evaluated={self.evaluated} "
            f"approved={self.approved} "
            f"({self.scan_duration_seconds:.1f}s)"
        )


# ---------------------------------------------------------------------------
# Parsing helpers (module-level so tests can import them directly)
# ---------------------------------------------------------------------------

def parse_threshold_from_ticker(ticker: str) -> float | None:
    """
    Extract temperature threshold from a Kalshi market ticker.

    Verified format: KXHIGHNY-26APR01-T79 → 79.0

    Returns None if the pattern is not found. Caller falls back to title.
    """
    # Primary pattern: separator + T + digits (end of string or another separator)
    match = re.search(r'[_\-]T(\d+)(?:[_\-]|$)', ticker, re.IGNORECASE)
    if match:
        return float(match.group(1))
    # Fallback: T + digits at end of string (no separator before it)
    match = re.search(r'T(\d+)$', ticker, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def parse_measurement_date_from_ticker(ticker: str) -> date | None:
    """
    Extract the MEASUREMENT DATE from a Kalshi market ticker.

    Verified format: KXHIGHNY-26APR01-T79 → date(2026, 4, 1)

    IMPORTANT: This is NOT the same as close_time.date(). Kalshi's close_time
    is the day AFTER the measurement date (markets close at ~05:00 UTC the
    following day after observations are recorded). Using close_time.date()
    would cause the probability engine to forecast the wrong day.

    Returns None if the date segment cannot be parsed. Caller falls back to
    parsing from the title, and skips the market if both fail.
    """
    # Pattern: -YYMONDD followed by separator or end of string
    # e.g. -26APR01- (middle of ticker) or -26MAR31 (end of ticker)
    match = re.search(
        r'[_\-](\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?:[_\-]|$)',
        ticker, re.IGNORECASE,
    )
    if match:
        year = 2000 + int(match.group(1))
        month = _MONTH_ABBR[match.group(2).upper()]
        day = int(match.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def parse_direction_from_text(text: str) -> str:
    """
    Detect whether a market is an "above" (>) or "below" (<) market.

    Verified Kalshi title formats:
        "Will the **high temp in NYC** be >79° on Apr 1, 2026?"  → "above"
        "Will the **high temp in NYC** be <72° on Apr 1, 2026?"  → "below"

    Defaults to "above" if neither symbol is found (conservative: most
    markets are above-threshold markets; a misclassified "below" market
    would get its edge computed with the wrong probability direction,
    which the risk checks would likely catch and reject anyway).
    """
    if not text:
        return "above"
    # Look for < immediately before a digit (with optional whitespace)
    if re.search(r'<\s*\d+', text):
        return "below"
    return "above"


def parse_threshold_from_text(text: str) -> float | None:
    """
    Extract temperature threshold from market title or subtitle.

    Verified Kalshi title patterns:
        "be >79° on Apr 1"  → 79.0   (> directly before number, no space)
        "be <72° on Apr 1"  → 72.0   (< directly before number, no space)
        "75°F", "75 F", "75 degrees Fahrenheit"

    If Kalshi changes their title wording, fix the regex here.
    This is the ONLY place to update if titles look different than expected.
    """
    if not text:
        return None
    # Match comparative operator directly before number: >79°, <72°, > 79, < 72
    match = re.search(r'[><]\s*(\d+)\s*[°]?', text)
    if match:
        return float(match.group(1))
    # Match explicit degree notation with F: 75°F, 75 F, 75F, 75 degrees Fahrenheit
    match = re.search(r'(\d+)\s*[°]?\s*[Ff](?:ahrenheit)?\b', text)
    if match:
        return float(match.group(1))
    # Match "above 75", "exceed 75", "greater than 75" (no operator symbol)
    match = re.search(
        r'(?:above|exceed[s]?|greater\s+than)\s+(\d+)',
        text, re.IGNORECASE,
    )
    if match:
        return float(match.group(1))
    return None


def parse_close_time(value: Any) -> datetime | None:
    """
    Parse a Kalshi close_time value to a UTC-aware datetime.

    Verified format from SDK: datetime object "2026-04-02 04:59:00+00:00"
    (SDK returns a datetime, not a string.)

    Also handles strings for robustness: "2026-03-31T23:59:59Z",
    "2026-03-31T23:59:59+00:00", "2026-04-02 04:59:00+00:00"

    Returns None if value is None or cannot be parsed. Caller skips the market.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    if isinstance(value, str):
        # Normalise: replace trailing Z, ensure fromisoformat can handle space separator
        normalised = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalised)
        except ValueError:
            pass
        # Explicit format fallbacks (no timezone suffix)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(value.strip().rstrip("Z"), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    logger.warning("Could not parse close_time value: %r", value)
    return None


# ---------------------------------------------------------------------------
# Market Scanner
# ---------------------------------------------------------------------------

class MarketScanner:
    """
    Discovers and evaluates Kalshi weather markets in one scan cycle.

    Instantiate once; call scan() on each polling cycle. The scanner is
    stateless between calls — all state (exposure, P&L) lives in RiskManager.

    Args:
        kalshi_client:      Authenticated KalshiClient wrapper
        probability_engine: ProbabilityEngine with ensemble client + bias DB
        edge_params:        EdgeParams (from settings.yaml via EdgeParams.from_settings)
        risk_limits:        RiskLimits (from settings.yaml via RiskLimits.from_settings)
        station_mapping:    Dict of series_ticker → station config dict.
                            Comes from config/station_mapping.yaml:
                            {"KXHIGHNY": {"latitude": 40.78, "longitude": -73.97,
                                          "station_id": "KNYC", "city": "New York"}}
        series_tickers:     List of Kalshi series tickers to scan (from market_categories.yaml)
        ai_reasoner:        Optional AIReasoner instance. None = stub (skip sanity checks).
                            TODO: Wire up when ai_reasoner.py is built (Phase 2 step 4).
                            See the # TODO: AI SANITY CHECK block in scan().
    """

    def __init__(
        self,
        kalshi_client: KalshiClient,
        probability_engine: ProbabilityEngine,
        edge_params: EdgeParams,
        risk_limits: RiskLimits,
        station_mapping: dict[str, dict],
        series_tickers: list[str],
        ai_reasoner: AIReasoner | None = None,
    ) -> None:
        self.kalshi_client = kalshi_client
        self.probability_engine = probability_engine
        self.edge_params = edge_params
        self.risk_limits = risk_limits
        self.station_mapping = station_mapping
        self.series_tickers = series_tickers
        self.ai_reasoner = ai_reasoner

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def scan(
        self,
        portfolio_balance: float,
        open_orders_count: int,
        risk_manager: RiskManager,
        skip_tickers: set[str] | None = None,
    ) -> tuple[list[ScanResult], ScanSummary]:
        """
        Run one full scan cycle.

        For efficiency, markets at the same station are batched into a single
        Open-Meteo API call (via estimate_multiple). A city with 10 threshold
        markets (70F, 75F, 80F, ...) costs 1 API call, not 10.

        Args:
            portfolio_balance:  Current available balance for Kelly sizing
            open_orders_count:  Number of currently open orders (for risk check 11)
            risk_manager:       Stateful RiskManager (tracks exposure and daily P&L)
            skip_tickers:       Set of market tickers to skip AI sanity checks for.
                                These are tickers the daemon already knows it won't
                                trade (dedup, manual skip, etc.) — no point spending
                                Claude API budget on them.

        Returns:
            (results, summary) — results contains ALL evaluated markets (approved
            AND rejected). Filter on result.risk.approved to get tradeable ones.
        """
        start_time = time.monotonic()
        summary = ScanSummary()
        all_results: list[ScanResult] = []

        for series_ticker in self.series_tickers:
            series_results = await self._scan_series(
                series_ticker=series_ticker,
                portfolio_balance=portfolio_balance,
                open_orders_count=open_orders_count,
                risk_manager=risk_manager,
                summary=summary,
                skip_tickers=skip_tickers,
            )
            all_results.extend(series_results)

        summary.scan_duration_seconds = time.monotonic() - start_time
        summary.approved = sum(1 for r in all_results if r.risk.approved)

        logger.info("%s", summary)
        return all_results, summary

    # ------------------------------------------------------------------
    # Per-series scan
    # ------------------------------------------------------------------

    async def _scan_series(
        self,
        series_ticker: str,
        portfolio_balance: float,
        open_orders_count: int,
        risk_manager: RiskManager,
        summary: ScanSummary,
        skip_tickers: set[str] | None = None,
    ) -> list[ScanResult]:
        """Scan all markets for one series ticker. Returns ScanResults."""
        # 1. Get fee coefficient for this series (cached 10 min in KalshiClient)
        fee_coefficient = await self._get_fee_coefficient(series_ticker)

        # 2. List all open markets
        try:
            markets = await self.kalshi_client.list_markets(
                series_ticker=series_ticker, status="open"
            )
        except Exception as exc:
            logger.error("Failed to list markets for series %s: %s", series_ticker, exc)
            return []

        summary.total_fetched += len(markets)

        # 3. Filter: liquidity (volume + spread)
        liquid: list[Any] = []
        for m in markets:
            if self._passes_liquidity_filter(m):
                liquid.append(m)
            else:
                summary.filtered_liquidity += 1
                logger.debug(
                    "Liquidity filter: skip %s (vol=%.2f spread=%.3f min_vol=%.1f max_spread=%.2f)",
                    getattr(m, "ticker", "?"),
                    parse_quantity(getattr(m, "volume_fp", None)),
                    self._compute_spread(m),
                    self.risk_limits.min_volume_fp,
                    self.risk_limits.max_spread_dollars,
                )

        if not liquid:
            return []

        # 4. Filter: time to resolution
        # hours_to_resolution is computed from close_time (when trading ends).
        # This is correct for the risk window check — it tells us how long until
        # the market closes, which is when we can no longer act.
        timed: list[tuple[Any, float]] = []  # (market, hours_to_resolution)
        for m in liquid:
            hours = self._hours_to_resolution(m)
            if hours is None:
                summary.filtered_time += 1
                continue
            lo = self.risk_limits.min_resolution_hours
            hi = self.risk_limits.max_resolution_days * 24.0
            if lo <= hours <= hi:
                timed.append((m, hours))
            else:
                summary.filtered_time += 1
                logger.debug(
                    "Time filter: skip %s (%.1fh, window=[%.0f, %.0f]h)",
                    getattr(m, "ticker", "?"), hours, lo, hi,
                )

        if not timed:
            return []

        # 5. Check station mapping
        station_config = self.station_mapping.get(series_ticker)
        if not station_config:
            summary.filtered_no_station += len(timed)
            logger.warning(
                "No station mapping for series %s — skipping %d markets. "
                "Add entry to config/station_mapping.yaml.",
                series_ticker, len(timed),
            )
            return []

        # 6. Parse MarketInfo for each market (threshold, measurement date, direction)
        parsed: list[tuple[Any, float, MarketInfo, str]] = []  # (market, hours, info, direction)
        for m, hours in timed:
            info, direction = self._parse_market_info(m, series_ticker)
            if info is None:
                summary.filtered_parse_error += 1
            else:
                parsed.append((m, hours, info, direction))

        if not parsed:
            return []

        summary.evaluated += len(parsed)

        # 7. Batch-estimate ensemble probabilities (one API call per station)
        market_infos = [info for (_, _, info, _) in parsed]
        ensemble_results = await self.probability_engine.estimate_multiple(
            markets=market_infos,
            station_config=station_config,
        )

        # 8. Compute edge + risk for each market
        city = station_config.get("city", series_ticker)
        results: list[ScanResult] = []

        for market_obj, hours, market_info, direction in parsed:
            ensemble = ensemble_results.get(market_info.ticker)
            if ensemble is None:
                logger.error("Missing ensemble result for %s", market_info.ticker)
                continue

            market_price = market_mid_price(market_obj)
            volume_fp = parse_quantity(getattr(market_obj, "volume_fp", None))
            spread_dollars = self._compute_spread(market_obj)
            market_status = getattr(market_obj, "status", "")

            # Direction adjustment: for "below" markets (<°), YES wins when the
            # temperature stays low. P(YES) = 1 - P(max >= threshold).
            # We create a direction-adjusted copy of the ensemble for edge computation.
            # The original ensemble is stored in ScanResult for calibration logging.
            if direction == "below":
                ensemble_for_edge = copy.copy(ensemble)
                ensemble_for_edge.probability = 1.0 - ensemble.probability
                logger.debug(
                    "%s is a 'below' market: flipping probability %.3f → %.3f",
                    market_info.ticker, ensemble.probability, ensemble_for_edge.probability,
                )
            else:
                ensemble_for_edge = ensemble

            edge = compute_edge(
                market_ticker=market_info.ticker,
                market_price=market_price,
                ensemble_result=ensemble_for_edge,
                fee_coefficient=fee_coefficient,
                portfolio_balance=portfolio_balance,
                max_position_dollars=self.risk_limits.max_position_dollars,
                params=self.edge_params,
            )

            risk = risk_manager.check_trade(
                edge=edge,
                ensemble=ensemble_for_edge,
                market_status=market_status,
                volume_fp=volume_fp,
                spread_dollars=spread_dollars,
                hours_to_resolution=hours,
                open_orders_count=open_orders_count,
            )

            # AI SANITY CHECK — only runs when edge is suspiciously large (>15pp),
            # an AIReasoner has been injected, the risk check already passed, AND
            # the ticker is not in the pre-blocked set (dedup / manual skip / order cap).
            # Skip the AI call for any ticker the daemon already knows it won't trade.
            if (edge.recommendation == "SANITY_CHECK"
                    and self.ai_reasoner is not None
                    and risk.approved
                    and (skip_tickers is None or market_info.ticker not in skip_tickers)):
                station_name = station_config.get("city", series_ticker)
                direction_str = "above" if direction == "above" else "below"
                ensemble_details = (
                    f"{ensemble.total_members} members, "
                    f"spread={ensemble.ensemble_spread:.1f}F"
                )
                ai_result = await self.ai_reasoner.sanity_check(
                    market_description=(
                        f"{station_name} high temp {direction_str} "
                        f"{market_info.threshold_value}F on {market_info.resolution_date}"
                    ),
                    ensemble_prob=ensemble_for_edge.probability,
                    market_price=market_price,
                    ensemble_details=ensemble_details,
                )
                # FUTURE IMPROVEMENT: adjusted_probability
                # ai_result.adjusted_probability is logged inside AIReasoner but
                # is NOT used to reprice the edge or position sizing here (Option B:
                # AI is a veto-only gatekeeper). If this system is to be improved,
                # consider re-running compute_edge() with ai_result.adjusted_probability
                # when proceed=True and the adjusted value differs materially.
                # See the FUTURE IMPROVEMENT note at the top of src/ai_reasoner.py.
                if not ai_result.proceed:
                    concern = ai_result.concern or "AI flagged concern"
                    risk = RiskCheckOutcome(
                        approved=False,
                        failed_checks=[f"AI SANITY CHECK FAIL: {concern}"],
                        notes=risk.notes,
                    )

            results.append(ScanResult(
                market_ticker=market_info.ticker,
                series_ticker=series_ticker,
                city=city,
                hours_to_resolution=hours,
                volume_fp=volume_fp,
                spread_dollars=spread_dollars,
                market_price=market_price,
                fee_coefficient=fee_coefficient,
                market_direction=direction,
                market_info=market_info,
                ensemble=ensemble,        # raw, unflipped — for calibration logging
                edge=edge,                # computed with direction-adjusted probability
                risk=risk,
                market=market_obj,
            ))

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_fee_coefficient(self, series_ticker: str) -> float:
        """
        Fetch fee coefficient from series metadata.
        Returns 0.07 (Kalshi default) if unavailable.
        """
        try:
            series = await self.kalshi_client.get_series(series_ticker)
            coeff = getattr(series, "fee_multiplier", None)
            if coeff is not None:
                return float(coeff)
        except Exception as exc:
            logger.warning(
                "Could not fetch fee coefficient for %s: %s — using default 0.07",
                series_ticker, exc,
            )
        return 0.07

    def _compute_spread(self, market: Any) -> float:
        """
        Compute bid-ask spread in dollars from a market object.

        On thin Kalshi weather markets the ask side is often absent (no one
        is actively selling YES contracts).  In that case, the YES ask can be
        approximated as (1 - best NO bid) from the NO side.  If neither side
        has a counteroffer present, we return 0.0 so the spread filter does
        not block an otherwise liquid market — the volume check is the right
        guard in that situation.
        """
        bid = parse_price(getattr(market, "yes_bid_dollars", None), default=0.0)

        yes_ask_raw = getattr(market, "yes_ask_dollars", None)
        yes_ask = parse_price(yes_ask_raw, default=0.0) if yes_ask_raw is not None else 0.0

        if yes_ask > 0.0:
            return max(0.0, yes_ask - bid)

        # YES ask missing — try to infer from NO bid (YES ask ≈ 1 - NO bid)
        no_bid_raw = getattr(market, "no_bid_dollars", None)
        no_bid = parse_price(no_bid_raw, default=0.0) if no_bid_raw is not None else 0.0
        if no_bid > 0.0:
            implied_ask = 1.0 - no_bid
            return max(0.0, implied_ask - bid)

        # Both sides absent — spread is undefined; pass volume filter only
        return 0.0

    def _passes_liquidity_filter(self, market: Any) -> bool:
        """True if market meets minimum volume and maximum spread requirements."""
        volume = parse_quantity(getattr(market, "volume_fp", None))
        spread = self._compute_spread(market)
        return (
            volume >= self.risk_limits.min_volume_fp
            and spread <= self.risk_limits.max_spread_dollars
        )

    def _hours_to_resolution(self, market: Any) -> float | None:
        """
        Compute hours from now until market close (close_time).
        This is used for the risk window check — how long can we still act?
        close_time is the day AFTER the measurement date on Kalshi weather markets.
        Returns None if close_time is missing or unparseable.
        """
        close_time_raw = getattr(market, "close_time", None)
        close_dt = parse_close_time(close_time_raw)
        if close_dt is None:
            return None
        now = datetime.now(timezone.utc)
        return (close_dt - now).total_seconds() / 3600.0

    def _parse_market_info(
        self, market: Any, series_ticker: str
    ) -> tuple[MarketInfo | None, str]:
        """
        Extract MarketInfo and direction from a Kalshi market object.

        Returns (MarketInfo, direction) or (None, "above") if parsing fails.
        The market will be skipped and counted in summary.filtered_parse_error
        if MarketInfo is None.

        Parsing strategy:
          - Threshold: ticker first (T{N} suffix), then title/subtitle
          - Measurement date: ticker first ({YY}{MON}{DD} segment), then title
          - Direction: title always (ticker doesn't encode it)
        """
        ticker = getattr(market, "ticker", None)
        if not ticker:
            return None, "above"

        # Skip "between" range markets (e.g. KXHIGHNY-26APR03-B71.5 → "be 71-72°").
        # These resolve YES only within a narrow band, requiring P(71≤max<72) which
        # the current probability engine doesn't compute. Skip silently.
        if re.search(r'[_\-]B\d', ticker, re.IGNORECASE):
            return None, "above"

        title = getattr(market, "title", "") or ""
        subtitle = getattr(market, "subtitle", "") or ""

        # --- Threshold ---
        threshold = parse_threshold_from_ticker(ticker)
        if threshold is None:
            threshold = parse_threshold_from_text(title + " " + subtitle)
        if threshold is None:
            logger.warning(
                "Could not parse threshold for %s (title=%r). "
                "Fix parse_threshold_from_text() in market_scanner.py if titles differ.",
                ticker, title,
            )
            return None, "above"

        # --- Measurement date (NOT close_time.date() — see module docstring) ---
        resolution_date = parse_measurement_date_from_ticker(ticker)
        if resolution_date is None:
            logger.warning(
                "Could not parse measurement date from ticker %s. "
                "Ensure ticker follows KXHIGHNY-26APR01-T79 format.",
                ticker,
            )
            return None, "above"

        # --- Direction (above/below threshold) ---
        direction = parse_direction_from_text(title)

        return MarketInfo(
            ticker=ticker,
            resolution_date=resolution_date,
            threshold_value=threshold,
            series_ticker=series_ticker,
        ), direction
