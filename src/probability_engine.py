"""
Probability Engine.

Orchestrates the full pipeline from market → ensemble fetch → probability estimate.
Does NOT call Claude. Does NOT make trading decisions. Just: given a market and a
station, what is P(max temp >= threshold) on the settlement date?

The engine also manages bias corrections. On first run, station_bias.db won't exist
and no corrections are applied (logged as a warning). After ~30 resolved markets per
station per season, compute_station_bias.py should be run to populate corrections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.calibration_engine import BiasDatabase  # single canonical implementation
from src.data_feeds.weather_ensemble import EnsembleResult, OpenMeteoEnsembleClient

logger = logging.getLogger(__name__)


# BiasDatabase is re-exported from here so existing imports like
# `from src.probability_engine import BiasDatabase` continue to work.
__all__ = ["BiasDatabase", "MarketInfo", "ProbabilityEngine"]


# ---------------------------------------------------------------------------
# Market data object (lightweight — passed in by scanner)
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    """
    The fields the probability engine needs from a Kalshi market object.
    Scanner extracts these from the SDK response to keep the engine decoupled.
    """
    ticker: str
    resolution_date: date
    threshold_value: float   # the °F threshold from the market title
    series_ticker: str       # e.g. "KXHIGHNY"


# ---------------------------------------------------------------------------
# Probability Engine
# ---------------------------------------------------------------------------

class ProbabilityEngine:
    """
    Given a MarketInfo and a station config, computes P(max temp >= threshold).

    This is the only entry point the scanner should use for weather probability
    estimation. It handles:
      - Ensemble fetch (with caching)
      - Bias correction lookup
      - Logging every estimate for later calibration analysis
    """

    def __init__(
        self,
        ensemble_client: OpenMeteoEnsembleClient,
        bias_db: BiasDatabase,
    ) -> None:
        self.ensemble = ensemble_client
        self.bias_db = bias_db

    async def estimate(
        self,
        market: MarketInfo,
        station_config: dict,
    ) -> EnsembleResult:
        """
        Full probability estimation pipeline.

        Args:
            market:         MarketInfo (ticker, resolution_date, threshold_value)
            station_config: dict from station_mapping.yaml for the market's series
                            must have: latitude, longitude, station_id

        Returns:
            EnsembleResult — always returns something, caller must check .is_usable
        """
        lat = station_config["latitude"]
        lon = station_config["longitude"]
        station_id = station_config["station_id"]

        # 1. Fetch ensemble data (returns cached response if < 15 min old)
        try:
            raw_data = await self.ensemble.fetch(lat=lat, lon=lon)
        except Exception as exc:
            logger.error(
                "Ensemble fetch failed for %s (station=%s): %s",
                market.ticker, station_id, exc,
            )
            return self.ensemble._empty_result(
                market.ticker, market.resolution_date,
                market.threshold_value, lat, lon,
                reason=f"fetch error: {exc}",
            )

        # 2. Look up bias correction for this station × month
        month = market.resolution_date.month
        bias = self.bias_db.get_bias(station_id=station_id, month=month)

        if bias != 0.0:
            logger.debug(
                "Applying bias correction of %+.2fF for %s in month %d",
                bias, station_id, month,
            )

        # 3. Compute probability
        result = self.ensemble.compute_threshold_probability(
            ensemble_data=raw_data,
            target_date=market.resolution_date,
            threshold_f=market.threshold_value,
            bias_correction=bias,
            market_ticker=market.ticker,
            station_lat=lat,
            station_lon=lon,
        )

        # 4. Guard: warn loudly if result is unusable
        if not result.is_usable:
            logger.warning(
                "Probability estimate for %s has only %d members (need 30). "
                "This trade will be skipped by the risk manager.",
                market.ticker, result.total_members,
            )

        return result

    async def estimate_multiple(
        self,
        markets: list[MarketInfo],
        station_config: dict,
    ) -> dict[str, EnsembleResult]:
        """
        Estimate probabilities for multiple markets at the SAME station.

        Efficiency note: the ensemble data is fetched ONCE and cached.
        All subsequent estimates for the same station reuse the cached data.
        For a city with 10 open threshold markets (e.g. NYC: 70F, 75F, 80F, ...),
        this means 1 API call instead of 10.

        Returns:
            dict mapping market.ticker → EnsembleResult
        """
        results: dict[str, EnsembleResult] = {}

        # Fetch once (or get from cache)
        lat = station_config["latitude"]
        lon = station_config["longitude"]

        try:
            raw_data = await self.ensemble.fetch(lat=lat, lon=lon)
        except Exception as exc:
            logger.error("Ensemble fetch failed for station batch: %s", exc)
            for market in markets:
                results[market.ticker] = self.ensemble._empty_result(
                    market.ticker, market.resolution_date,
                    market.threshold_value, lat, lon,
                    reason=f"fetch error: {exc}",
                )
            return results

        # Compute probability for each market using the raw data we just fetched.
        # We pass raw_data directly rather than going through the cache lookup,
        # so this works correctly even in tests that mock fetch().
        station_id = station_config["station_id"]
        for market in markets:
            month = market.resolution_date.month
            bias = self.bias_db.get_bias(station_id=station_id, month=month)

            result = self.ensemble.compute_threshold_probability(
                ensemble_data=raw_data,
                target_date=market.resolution_date,
                threshold_f=market.threshold_value,
                bias_correction=bias,
                market_ticker=market.ticker,
                station_lat=lat,
                station_lon=lon,
            )
            results[market.ticker] = result

        return results
