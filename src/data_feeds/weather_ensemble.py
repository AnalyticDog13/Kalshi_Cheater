"""
Open-Meteo Ensemble API feed.

This is the PRIMARY probability engine data source. It fetches 139 individual
weather model member forecasts across GFS (30), ECMWF (50), ICON (39), and GEM (20).

Key design notes:
  - Base URL is ensemble-api.open-meteo.com, NOT api.open-meteo.com (spec had wrong URL).
  - Member field names are: temperature_2m_member{NN}_{model_suffix}
    e.g. temperature_2m_member01_ecmwf_ifs025_ensemble
  - Time strings have no seconds: "2026-03-31T00:00" (not "T00:00:00")
  - Cache responses for 15 minutes — model data updates every ~6 hours, polling faster wastes quota.
  - On cache miss, fetches all 4 models in a single API call (more efficient than 4 calls).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

from src.data_feeds.base import DataFeed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class EnsembleResult:
    """
    Result of ensemble probability computation for a single market.
    All fields are populated by OpenMeteoEnsembleClient.compute_threshold_probability().
    """
    market_ticker: str
    target_date: date
    threshold_f: float
    station_lat: float
    station_lon: float

    # Core probability
    probability: float              # P(daily max >= threshold_f) after bias correction
    total_members: int              # how many ensemble members contributed
    members_exceeding: int          # how many predicted >= threshold

    # Spread / uncertainty
    ensemble_mean: float            # mean daily max across all members (°F)
    ensemble_spread: float          # max - min across members (°F)
    ensemble_std: float             # standard deviation (°F)
    confidence_interval_90: tuple[float, float]  # (5th pct, 95th pct)

    # Provenance
    models_used: list[str]          # which model names were requested
    member_keys_found: int          # how many member fields were actually in the response
    freshness_minutes: float        # age of response at computation time (0 if just fetched)
    bias_correction_applied: float  # °F subtracted from every member before counting
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def confidence(self) -> str:
        """
        Heuristic confidence based on ensemble spread.
        Thresholds from settings.yaml: high < 6°F, medium < 12°F, low >= 12°F.
        Wide spread = high uncertainty = we need more edge before trading.
        """
        if self.ensemble_spread < 6.0:
            return "high"
        elif self.ensemble_spread < 12.0:
            return "medium"
        else:
            return "low"

    @property
    def is_usable(self) -> bool:
        """False if too few members — don't trade on thin ensemble data."""
        return self.total_members >= 30

    def __str__(self) -> str:
        return (
            f"EnsembleResult({self.market_ticker}: "
            f"P={self.probability:.1%} [{self.members_exceeding}/{self.total_members} members], "
            f"mean={self.ensemble_mean:.1f}F, spread={self.ensemble_spread:.1f}F, "
            f"conf={self.confidence}, bias={self.bias_correction_applied:+.1f}F)"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpenMeteoEnsembleClient(DataFeed):
    """
    Fetches ensemble forecasts from Open-Meteo and computes threshold probabilities.

    Usage:
        client = OpenMeteoEnsembleClient()
        raw = await client.fetch(lat=40.7789, lon=-73.9692)
        result = client.compute_threshold_probability(raw, date(2026, 4, 1), 75.0)
    """

    BASE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

    # Confirmed valid model names (verified 2026-03-31 against live API).
    # Member counts: GFS=30, ECMWF=50, ICON=39, GEM=20 → 139 total.
    DEFAULT_MODELS = [
        "gfs_seamless",     # NOAA GFS: 30 members
        "ecmwf_ifs025",     # ECMWF IFS: 50 members — best global model
        "icon_seamless",    # DWD ICON: 39 members — good for US East Coast
        "gem_global",       # Canadian GEM: 20 members
    ]

    def __init__(
        self,
        models: list[str] | None = None,
        cache_ttl_minutes: float = 15.0,
        request_timeout: float = 30.0,
    ) -> None:
        self.models = models or self.DEFAULT_MODELS
        self.cache_ttl_seconds = cache_ttl_minutes * 60
        self.request_timeout = request_timeout

        # Simple in-memory cache keyed by (lat, lon, models_tuple)
        self._cache: dict[tuple, tuple[dict, float]] = {}  # key -> (data, fetched_at_monotonic)
        self._last_fetch_status: int | None = None

    # ------------------------------------------------------------------
    # DataFeed interface
    # ------------------------------------------------------------------

    async def fetch(self, lat: float, lon: float, models: list[str] | None = None) -> dict:
        """
        Fetch ensemble forecast for a location, with 15-minute cache.

        Args:
            lat: station latitude
            lon: station longitude
            models: override model list (uses DEFAULT_MODELS if None)

        Returns:
            Raw Open-Meteo ensemble API response dict.
        """
        model_list = models or self.models
        cache_key = (round(lat, 4), round(lon, 4), tuple(model_list))

        # Check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            data, fetched_at = cached
            age = time.monotonic() - fetched_at
            if age < self.cache_ttl_seconds:
                logger.debug(
                    "Ensemble cache hit for (%.4f, %.4f), age=%.0fs",
                    lat, lon, age,
                )
                return data

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "models": ",".join(model_list),
            "forecast_days": 7,
        }

        logger.info("Fetching ensemble forecast: lat=%.4f lon=%.4f models=%s", lat, lon, model_list)

        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await client.get(self.BASE_URL, params=params)
            self._last_fetch_status = response.status_code
            response.raise_for_status()
            data = response.json()

        # Validate we got actual member data
        hourly = data.get("hourly", {})
        member_keys = [k for k in hourly if "member" in k and "temperature" in k]
        if not member_keys:
            logger.error(
                "Open-Meteo returned no member fields for models=%s. "
                "Check model names and API structure.",
                model_list,
            )
        else:
            logger.info(
                "Ensemble fetch OK: %d member fields returned for (%.4f, %.4f)",
                len(member_keys), lat, lon,
            )

        self._cache[cache_key] = (data, time.monotonic())
        return data

    def is_healthy(self) -> bool:
        return self._last_fetch_status in (None, 200)

    # ------------------------------------------------------------------
    # Probability computation
    # ------------------------------------------------------------------

    def compute_threshold_probability(
        self,
        ensemble_data: dict,
        target_date: date,
        threshold_f: float,
        bias_correction: float = 0.0,
        market_ticker: str = "",
        station_lat: float = 0.0,
        station_lon: float = 0.0,
    ) -> EnsembleResult:
        """
        THE CORE PROBABILITY ENGINE.

        Count ensemble members whose daily max temperature on target_date
        meets or exceeds threshold_f (after bias correction).

        P(max >= threshold) = members_exceeding / total_members

        This is deterministic math on physics model output — no AI, no opinions.

        Args:
            ensemble_data:    raw dict from fetch()
            target_date:      the date the Kalshi market settles
            threshold_f:      temperature threshold in °F (from market title)
            bias_correction:  °F to subtract from every member (positive = model runs warm)
                              e.g. bias=+1.8 means GFS overpredicts by 1.8°F at this station
            market_ticker:    filled in for logging/tracking
            station_lat/lon:  filled in for the result record

        Returns:
            EnsembleResult with probability and metadata.
        """
        hourly = ensemble_data.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        target_prefix = target_date.isoformat()  # "2026-04-01"

        # Identify which time indices fall on target_date
        target_indices = [i for i, t in enumerate(times) if t.startswith(target_prefix)]

        if not target_indices:
            logger.warning(
                "No time slots found for target_date=%s in ensemble data. "
                "Available dates: %s to %s",
                target_date,
                times[0] if times else "NONE",
                times[-1] if times else "NONE",
            )
            return self._empty_result(
                market_ticker, target_date, threshold_f, station_lat, station_lon,
                reason="target date not in forecast window",
            )

        # Extract per-member daily maxes
        member_daily_maxes: list[float] = []
        member_keys_found = 0
        model_suffixes_seen: set[str] = set()

        for key, values in hourly.items():
            if "member" not in key or "temperature" not in key:
                continue

            member_keys_found += 1

            # Track which models contributed (suffix after the member number)
            # e.g. "temperature_2m_member01_ecmwf_ifs025_ensemble" -> "ecmwf_ifs025_ensemble"
            parts = key.split("_member")
            if len(parts) == 2:
                suffix_parts = parts[1].split("_", 1)
                if len(suffix_parts) == 2:
                    model_suffixes_seen.add(suffix_parts[1])

            # Find daily max for this member on target_date
            daily_max: float | None = None
            for i in target_indices:
                val = values[i] if i < len(values) else None
                if val is not None:
                    if daily_max is None or val > daily_max:
                        daily_max = float(val)

            if daily_max is not None:
                # Apply bias correction: subtract if model runs warm, add if model runs cold
                corrected = daily_max - bias_correction
                member_daily_maxes.append(corrected)

        if not member_daily_maxes:
            logger.error(
                "Zero members produced valid daily maxes for %s on %s. "
                "Keys found: %d, target_indices: %s",
                market_ticker, target_date, member_keys_found, target_indices[:3],
            )
            return self._empty_result(
                market_ticker, target_date, threshold_f, station_lat, station_lon,
                reason="zero valid member maxes",
            )

        total = len(member_daily_maxes)
        exceeding = sum(1 for t in member_daily_maxes if t >= threshold_f)
        probability = exceeding / total

        sorted_maxes = sorted(member_daily_maxes)
        p5_idx = max(0, int(0.05 * total) - 1)
        p95_idx = min(total - 1, int(0.95 * total))
        mean = sum(member_daily_maxes) / total
        spread = max(member_daily_maxes) - min(member_daily_maxes)

        # Standard deviation
        variance = sum((t - mean) ** 2 for t in member_daily_maxes) / total
        std = variance ** 0.5

        result = EnsembleResult(
            market_ticker=market_ticker,
            target_date=target_date,
            threshold_f=threshold_f,
            station_lat=station_lat,
            station_lon=station_lon,
            probability=probability,
            total_members=total,
            members_exceeding=exceeding,
            ensemble_mean=mean,
            ensemble_spread=spread,
            ensemble_std=std,
            confidence_interval_90=(sorted_maxes[p5_idx], sorted_maxes[p95_idx]),
            models_used=self.models,
            member_keys_found=member_keys_found,
            freshness_minutes=0.0,
            bias_correction_applied=bias_correction,
        )

        logger.info(
            "Probability computed: %s | P=%.1f%% (%d/%d members) | "
            "mean=%.1fF spread=%.1fF conf=%s | bias=%+.1fF",
            market_ticker or f"lat={station_lat}",
            probability * 100, exceeding, total,
            mean, spread, result.confidence, bias_correction,
        )

        return result

    def compute_threshold_probability_from_cache(
        self,
        lat: float,
        lon: float,
        target_date: date,
        threshold_f: float,
        bias_correction: float = 0.0,
        market_ticker: str = "",
    ) -> EnsembleResult | None:
        """
        Compute probability using cached data only — no network call.
        Returns None if no cached data exists for this location.

        Used by the scanner to evaluate many markets without re-fetching
        when they share a station (e.g. multiple NYC threshold levels).
        """
        model_list = self.models
        cache_key = (round(lat, 4), round(lon, 4), tuple(model_list))
        cached = self._cache.get(cache_key)
        if cached is None:
            return None

        data, fetched_at = cached
        age_minutes = (time.monotonic() - fetched_at) / 60

        result = self.compute_threshold_probability(
            ensemble_data=data,
            target_date=target_date,
            threshold_f=threshold_f,
            bias_correction=bias_correction,
            market_ticker=market_ticker,
            station_lat=lat,
            station_lon=lon,
        )
        result.freshness_minutes = age_minutes
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _empty_result(
        self,
        market_ticker: str,
        target_date: date,
        threshold_f: float,
        station_lat: float,
        station_lon: float,
        reason: str = "unknown",
    ) -> EnsembleResult:
        """Return a zero-member result that will fail the is_usable check."""
        logger.warning("Returning empty EnsembleResult: reason=%s", reason)
        return EnsembleResult(
            market_ticker=market_ticker,
            target_date=target_date,
            threshold_f=threshold_f,
            station_lat=station_lat,
            station_lon=station_lon,
            probability=0.5,    # neutral — do NOT trade on this
            total_members=0,
            members_exceeding=0,
            ensemble_mean=0.0,
            ensemble_spread=999.0,  # max spread ensures "low" confidence
            ensemble_std=0.0,
            confidence_interval_90=(0.0, 0.0),
            models_used=self.models,
            member_keys_found=0,
            freshness_minutes=0.0,
            bias_correction_applied=0.0,
        )

    def clear_cache(self) -> None:
        self._cache.clear()
