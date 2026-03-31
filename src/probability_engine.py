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
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.data_feeds.weather_ensemble import EnsembleResult, OpenMeteoEnsembleClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bias database
# ---------------------------------------------------------------------------

class BiasDatabase:
    """
    Reads per-station, per-month bias corrections from station_bias.db.

    Schema (created by scripts/compute_station_bias.py):
        CREATE TABLE station_bias (
            station_id TEXT,
            month INTEGER,         -- 1-12
            bias_f REAL,           -- °F to subtract from model predictions
            sample_count INTEGER,  -- how many resolved markets this is based on
            updated_at TEXT,       -- ISO8601
            PRIMARY KEY (station_id, month)
        )

    If the database doesn't exist yet, all lookups return 0.0 (no correction).
    This is safe — it just means we're running without bias correction, which
    is conservative (wider effective uncertainty).
    """

    MIN_SAMPLE_COUNT = 30  # don't apply corrections based on fewer than this

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._cache: dict[tuple[str, int], float] = {}
        self._loaded = False

    def _load(self) -> None:
        """Load all bias corrections into memory. Called lazily on first lookup."""
        if self._loaded:
            return

        if not self.db_path.exists():
            logger.warning(
                "station_bias.db not found at %s. "
                "Running without bias corrections. "
                "Run scripts/compute_station_bias.py after accumulating 30+ resolved markets "
                "per station to enable corrections.",
                self.db_path,
            )
            self._loaded = True
            return

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT station_id, month, bias_f, sample_count FROM station_bias"
            ).fetchall()
            conn.close()

            loaded = 0
            skipped = 0
            for row in rows:
                if row["sample_count"] >= self.MIN_SAMPLE_COUNT:
                    self._cache[(row["station_id"].upper(), row["month"])] = row["bias_f"]
                    loaded += 1
                else:
                    logger.debug(
                        "Skipping bias for %s month=%d: only %d samples (need %d)",
                        row["station_id"], row["month"], row["sample_count"], self.MIN_SAMPLE_COUNT,
                    )
                    skipped += 1

            logger.info(
                "Loaded %d bias corrections (%d skipped, insufficient samples)",
                loaded, skipped,
            )
        except Exception as exc:
            logger.error("Failed to load station_bias.db: %s — proceeding without corrections", exc)

        self._loaded = True

    def get_bias(self, station_id: str, month: int) -> float:
        """
        Return the bias correction for a station in a given month.

        Returns 0.0 if:
          - DB doesn't exist yet
          - Insufficient sample count for this station/month
          - Station not in DB
        """
        self._load()
        return self._cache.get((station_id.upper(), month), 0.0)


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
