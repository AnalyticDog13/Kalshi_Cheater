"""
NOAA Weather API feed.

Two purposes:
  1. Station validation — given a NWS station ID (e.g. "KNYC"), fetch its exact
     lat/lon coordinates so we can verify our station_mapping.yaml is correct.
     A wrong coordinate flowing into the ensemble = wrong probability = losing trades.

  2. Post-resolution ground truth — after a Kalshi market settles, pull the actual
     recorded temperature for the settlement date. This goes into calibration.db
     so we can measure our Brier score and detect model biases.

API: https://api.weather.gov (free, no auth, requires User-Agent header)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from src.data_feeds.base import DataFeed

logger = logging.getLogger(__name__)

# NOAA requires a User-Agent identifying your application.
# Without this, requests get 403'd.
NOAA_USER_AGENT = "KalshiWeatherTrader/1.0 (contact: weather-trader@example.com)"


class NOAAObservationClient(DataFeed):
    """
    Client for the NOAA Weather API (api.weather.gov).

    Station info is cached indefinitely — station coordinates don't change.
    Observations are not cached since we only fetch them once after resolution.
    """

    BASE_URL = "https://api.weather.gov"

    def __init__(self, request_timeout: float = 20.0) -> None:
        self.request_timeout = request_timeout
        self._station_cache: dict[str, dict] = {}
        self._last_status: int | None = None

    # ------------------------------------------------------------------
    # DataFeed interface
    # ------------------------------------------------------------------

    async def fetch(self, station_id: str) -> dict:
        """Fetch station metadata. Thin alias for get_station_info()."""
        return await self.get_station_info(station_id)

    def is_healthy(self) -> bool:
        return self._last_status in (None, 200)

    # ------------------------------------------------------------------
    # Station info (used for coordinate validation)
    # ------------------------------------------------------------------

    async def get_station_info(self, station_id: str) -> dict:
        """
        Get NWS station metadata including exact lat/lon coordinates.

        This is called once on startup per station to validate that our
        station_mapping.yaml coordinates match what NOAA records.

        Returns:
            {
                "station_id": "KNYC",
                "name": "New York, Central Park",
                "latitude": 40.7789,
                "longitude": -73.9692,
                "elevation_m": 42.1,
                "timezone": "America/New_York",
            }
        """
        station_id = station_id.upper()

        if station_id in self._station_cache:
            return self._station_cache[station_id]

        url = f"{self.BASE_URL}/stations/{station_id}"
        headers = {"User-Agent": NOAA_USER_AGENT, "Accept": "application/geo+json"}

        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            resp = await client.get(url, headers=headers)
            self._last_status = resp.status_code
            resp.raise_for_status()
            data = resp.json()

        props = data.get("properties", {})
        geometry = data.get("geometry", {})
        coords = geometry.get("coordinates", [None, None])  # [lon, lat] in GeoJSON

        # GeoJSON coordinate order is [longitude, latitude]
        longitude = coords[0]
        latitude = coords[1]

        elevation_val = props.get("elevation", {})
        elevation_m = elevation_val.get("value") if isinstance(elevation_val, dict) else None

        result = {
            "station_id": station_id,
            "name": props.get("name", ""),
            "latitude": latitude,
            "longitude": longitude,
            "elevation_m": elevation_m,
            "timezone": props.get("timeZone", ""),
            "raw": data,
        }

        self._station_cache[station_id] = result
        logger.info(
            "Station %s: %s (%.4f, %.4f) elev=%.0fm tz=%s",
            station_id, result["name"],
            latitude or 0, longitude or 0,
            elevation_m or 0, result["timezone"],
        )
        return result

    def validate_station_coordinates(
        self,
        station_id: str,
        config_lat: float,
        config_lon: float,
        tolerance_degrees: float = 0.05,  # ~3.5 miles at mid-latitudes
    ) -> tuple[bool, str]:
        """
        Check that our station_mapping.yaml coordinates match NOAA's record.

        Args:
            station_id:        e.g. "KNYC"
            config_lat/lon:    what station_mapping.yaml says
            tolerance_degrees: max acceptable error (~0.05° ≈ 3.5 miles)

        Returns:
            (ok: bool, message: str)

        IMPORTANT: Call get_station_info() first to populate the cache.
        """
        cached = self._station_cache.get(station_id.upper())
        if cached is None:
            return False, f"Station {station_id} not yet fetched — call get_station_info() first"

        noaa_lat = cached["latitude"]
        noaa_lon = cached["longitude"]

        if noaa_lat is None or noaa_lon is None:
            return False, f"NOAA returned null coordinates for {station_id}"

        lat_err = abs(config_lat - noaa_lat)
        lon_err = abs(config_lon - noaa_lon)

        if lat_err <= tolerance_degrees and lon_err <= tolerance_degrees:
            return True, (
                f"OK: config ({config_lat:.4f}, {config_lon:.4f}) vs "
                f"NOAA ({noaa_lat:.4f}, {noaa_lon:.4f}) — "
                f"delta lat={lat_err:.4f} lon={lon_err:.4f}"
            )
        else:
            return False, (
                f"MISMATCH: config ({config_lat:.4f}, {config_lon:.4f}) vs "
                f"NOAA ({noaa_lat:.4f}, {noaa_lon:.4f}) — "
                f"delta lat={lat_err:.4f} lon={lon_err:.4f} (tolerance={tolerance_degrees})"
            )

    # ------------------------------------------------------------------
    # Observations (used for post-resolution calibration)
    # ------------------------------------------------------------------

    async def get_daily_max_temperature(
        self,
        station_id: str,
        target_date: date,
    ) -> dict | None:
        """
        Get the actual recorded daily maximum temperature at a station.

        Called AFTER a Kalshi market resolves to record ground truth for
        calibration and Brier score computation.

        NOAA's observation endpoint returns individual hourly readings.
        We fetch the full day and compute the max ourselves, which is more
        reliable than relying on NOAA's own daily summary (which can be delayed).

        Returns:
            {
                "station_id": "KNYC",
                "date": "2026-04-01",
                "max_temp_f": 78.2,
                "observation_count": 24,
                "source": "hourly_max",
            }
        Or None if no observations are available.
        """
        station_id = station_id.upper()
        date_str = target_date.isoformat()

        # NOAA API wants ISO8601 with timezone
        start = f"{date_str}T00:00:00+00:00"
        end = f"{date_str}T23:59:59+00:00"

        url = f"{self.BASE_URL}/stations/{station_id}/observations"
        params = {"start": start, "end": end, "limit": 500}
        headers = {"User-Agent": NOAA_USER_AGENT, "Accept": "application/geo+json"}

        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            self._last_status = resp.status_code

            if resp.status_code == 404:
                logger.warning("NOAA 404 for station %s observations on %s", station_id, date_str)
                return None

            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        if not features:
            logger.warning(
                "No observations found for station %s on %s",
                station_id, date_str,
            )
            return None

        # Extract temperatures — NOAA returns Celsius, we convert to Fahrenheit
        temps_f: list[float] = []
        for obs in features:
            props = obs.get("properties", {})
            temp_obj = props.get("temperature", {})
            temp_c = temp_obj.get("value") if isinstance(temp_obj, dict) else None
            if temp_c is not None:
                temps_f.append(temp_c * 9 / 5 + 32)

        if not temps_f:
            # Some stations report temperature differently — try maxTemperatureLast24Hours
            for obs in features:
                props = obs.get("properties", {})
                max_obj = props.get("maxTemperatureLast24Hours", {})
                max_c = max_obj.get("value") if isinstance(max_obj, dict) else None
                if max_c is not None:
                    temps_f.append(max_c * 9 / 5 + 32)

        if not temps_f:
            logger.warning(
                "Observations found for %s on %s but no temperature values parseable "
                "(%d features examined)",
                station_id, date_str, len(features),
            )
            return None

        max_temp_f = max(temps_f)

        logger.info(
            "Ground truth for %s on %s: max=%.1fF (%d observations)",
            station_id, date_str, max_temp_f, len(temps_f),
        )

        return {
            "station_id": station_id,
            "date": date_str,
            "max_temp_f": max_temp_f,
            "observation_count": len(temps_f),
            "source": "hourly_max",
        }
