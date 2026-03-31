"""
Tests for:
  - src/data_feeds/weather_ensemble.py (EnsembleResult, OpenMeteoEnsembleClient)
  - src/data_feeds/weather_observations.py (NOAAObservationClient)
  - src/probability_engine.py (BiasDatabase, ProbabilityEngine)

All tests use mocked/synthetic data — no real API calls.
Run with: pytest tests/test_probability_engine.py -v
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data_feeds.weather_ensemble import EnsembleResult, OpenMeteoEnsembleClient
from src.data_feeds.weather_observations import NOAAObservationClient
from src.probability_engine import BiasDatabase, MarketInfo, ProbabilityEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ensemble_data(
    target_date: date,
    member_temps: list[float],
    model_suffix: str = "ecmwf_ifs025_ensemble",
) -> dict:
    """
    Build a synthetic Open-Meteo ensemble response with controlled member temperatures.

    Generates 24 hourly time slots for target_date where:
      - Each member has its temperature constant throughout the day
      - The daily max for member N = member_temps[N]
    """
    date_str = target_date.isoformat()
    times = [f"{date_str}T{h:02d}:00" for h in range(24)]

    hourly: dict = {"time": times}
    for i, temp in enumerate(member_temps):
        key = f"temperature_2m_member{i+1:02d}_{model_suffix}"
        hourly[key] = [temp] * 24  # constant temp throughout day

    return {"hourly": hourly}


def _make_multi_day_ensemble_data(
    dates: list[date],
    member_temps_by_date: dict[str, list[float]],
    model_suffix: str = "ecmwf_ifs025_ensemble",
) -> dict:
    """Build ensemble data spanning multiple dates."""
    times = []
    for d in dates:
        date_str = d.isoformat()
        times.extend([f"{date_str}T{h:02d}:00" for h in range(24)])

    n_members = max(len(temps) for temps in member_temps_by_date.values())
    hourly: dict = {"time": times}

    for i in range(n_members):
        key = f"temperature_2m_member{i+1:02d}_{model_suffix}"
        values = []
        for d in dates:
            date_str = d.isoformat()
            temps_for_date = member_temps_by_date.get(date_str, [])
            temp = temps_for_date[i] if i < len(temps_for_date) else None
            values.extend([temp] * 24)
        hourly[key] = values

    return {"hourly": hourly}


# ---------------------------------------------------------------------------
# EnsembleResult tests
# ---------------------------------------------------------------------------

class TestEnsembleResult:
    def _make(self, spread: float, total: int = 50) -> EnsembleResult:
        return EnsembleResult(
            market_ticker="TEST-TICKER",
            target_date=date(2026, 7, 1),
            threshold_f=85.0,
            station_lat=40.77,
            station_lon=-73.96,
            probability=0.6,
            total_members=total,
            members_exceeding=30,
            ensemble_mean=84.0,
            ensemble_spread=spread,
            ensemble_std=2.0,
            confidence_interval_90=(78.0, 90.0),
            models_used=["ecmwf_ifs025"],
            member_keys_found=50,
            freshness_minutes=0.0,
            bias_correction_applied=0.0,
        )

    def test_confidence_high(self):
        assert self._make(spread=5.0).confidence == "high"

    def test_confidence_medium(self):
        assert self._make(spread=8.0).confidence == "medium"

    def test_confidence_low(self):
        assert self._make(spread=15.0).confidence == "low"

    def test_is_usable_sufficient_members(self):
        assert self._make(spread=5.0, total=50).is_usable is True

    def test_is_usable_insufficient_members(self):
        assert self._make(spread=5.0, total=10).is_usable is False

    def test_is_usable_exactly_30(self):
        assert self._make(spread=5.0, total=30).is_usable is True

    def test_str_repr_contains_key_info(self):
        r = self._make(spread=5.0)
        s = str(r)
        assert "TEST-TICKER" in s
        assert "60.0%" in s


# ---------------------------------------------------------------------------
# OpenMeteoEnsembleClient.compute_threshold_probability tests
# ---------------------------------------------------------------------------

class TestComputeThresholdProbability:
    def setup_method(self):
        self.client = OpenMeteoEnsembleClient()
        self.target = date(2026, 7, 4)

    def test_all_above_threshold(self):
        # 10 members all at 90°F, threshold 85°F → P=1.0
        data = _make_ensemble_data(self.target, [90.0] * 10)
        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.probability == pytest.approx(1.0)
        assert result.members_exceeding == 10
        assert result.total_members == 10

    def test_all_below_threshold(self):
        # 10 members all at 70°F, threshold 85°F → P=0.0
        data = _make_ensemble_data(self.target, [70.0] * 10)
        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.probability == pytest.approx(0.0)
        assert result.members_exceeding == 0

    def test_half_above_threshold(self):
        # 20 members: 10 at 90°F, 10 at 80°F, threshold 85°F → P=0.5
        temps = [90.0] * 10 + [80.0] * 10
        data = _make_ensemble_data(self.target, temps)
        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.probability == pytest.approx(0.5)

    def test_exactly_at_threshold_counts_as_exceeding(self):
        # 85.0 >= 85.0 should count
        data = _make_ensemble_data(self.target, [85.0] * 10)
        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.probability == pytest.approx(1.0)

    def test_bias_correction_applied_correctly(self):
        # 10 members at 87°F with bias_correction=+2°F (model runs warm)
        # corrected temp = 87 - 2 = 85°F, which just meets the threshold
        data = _make_ensemble_data(self.target, [87.0] * 10)
        result = self.client.compute_threshold_probability(
            data, self.target, 85.0, bias_correction=2.0
        )
        assert result.probability == pytest.approx(1.0)
        assert result.bias_correction_applied == pytest.approx(2.0)

    def test_bias_correction_pushes_below_threshold(self):
        # 10 members at 86°F, threshold 85°F, bias +2°F → corrected = 84°F < 85°F → P=0
        data = _make_ensemble_data(self.target, [86.0] * 10)
        result = self.client.compute_threshold_probability(
            data, self.target, 85.0, bias_correction=2.0
        )
        assert result.probability == pytest.approx(0.0)

    def test_ensemble_statistics_computed_correctly(self):
        temps = [70.0, 80.0, 90.0, 100.0]  # mean=85, spread=30
        data = _make_ensemble_data(self.target, temps)
        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.ensemble_mean == pytest.approx(85.0)
        assert result.ensemble_spread == pytest.approx(30.0)
        assert result.total_members == 4

    def test_wrong_date_returns_empty_result(self):
        data = _make_ensemble_data(self.target, [90.0] * 10)
        wrong_date = date(2030, 1, 1)  # not in the data
        result = self.client.compute_threshold_probability(data, wrong_date, 85.0)
        assert result.total_members == 0
        assert result.is_usable is False
        assert result.probability == pytest.approx(0.5)  # neutral, not tradeable

    def test_daily_max_used_not_hourly_average(self):
        # Build a day where temp peaks at hour 14 at 90°F, rest of day is 65°F
        date_str = self.target.isoformat()
        times = [f"{date_str}T{h:02d}:00" for h in range(24)]
        temps = [65.0] * 24
        temps[14] = 90.0  # peak at 2pm

        hourly = {
            "time": times,
            "temperature_2m_member01_ecmwf_ifs025_ensemble": temps,
        }
        data = {"hourly": hourly}

        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        assert result.members_exceeding == 1  # the 90°F peak should be caught
        assert result.ensemble_mean == pytest.approx(90.0)  # daily MAX, not hourly avg

    def test_none_values_in_hourly_data_skipped(self):
        # Some ensemble members may have null/None for certain hours
        date_str = self.target.isoformat()
        times = [f"{date_str}T{h:02d}:00" for h in range(24)]

        hourly = {
            "time": times,
            "temperature_2m_member01_ecmwf_ifs025_ensemble": [None] * 24,   # all null
            "temperature_2m_member02_ecmwf_ifs025_ensemble": [90.0] * 24,  # valid
        }
        data = {"hourly": hourly}

        result = self.client.compute_threshold_probability(data, self.target, 85.0)
        # Only member02 should contribute
        assert result.total_members == 1
        assert result.members_exceeding == 1

    def test_confidence_interval_bounds(self):
        # 100 members spanning 60-100°F, threshold 80°F
        temps = [60.0 + i * 0.4 for i in range(100)]  # 60.0 to 99.6
        data = _make_ensemble_data(self.target, temps)
        result = self.client.compute_threshold_probability(data, self.target, 80.0)
        lo, hi = result.confidence_interval_90
        assert lo < hi
        assert lo >= 60.0
        assert hi <= 100.0

    def test_ticker_and_coords_passed_through(self):
        data = _make_ensemble_data(self.target, [85.0] * 5)
        result = self.client.compute_threshold_probability(
            data, self.target, 85.0,
            market_ticker="KXHIGHNY-26APR01-T85",
            station_lat=40.7789, station_lon=-73.9692,
        )
        assert result.market_ticker == "KXHIGHNY-26APR01-T85"
        assert result.station_lat == pytest.approx(40.7789)


# ---------------------------------------------------------------------------
# OpenMeteoEnsembleClient caching tests
# ---------------------------------------------------------------------------

class TestEnsembleClientCaching:
    @pytest.mark.asyncio
    async def test_second_fetch_uses_cache(self):
        client = OpenMeteoEnsembleClient(cache_ttl_minutes=15)
        target = date(2026, 7, 1)
        fake_data = _make_ensemble_data(target, [85.0] * 30)

        with patch.object(client, "_cache", {}):
            with patch("httpx.AsyncClient") as mock_http:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = fake_data
                mock_resp.raise_for_status = MagicMock()
                mock_http.return_value.__aenter__.return_value.get = AsyncMock(
                    return_value=mock_resp
                )

                # First call — hits HTTP
                await client.fetch(lat=40.77, lon=-73.96)
                # Second call — should be cached (no new HTTP call)
                await client.fetch(lat=40.77, lon=-73.96)

                assert mock_http.return_value.__aenter__.return_value.get.call_count == 1

    def test_compute_from_cache_returns_none_on_miss(self):
        client = OpenMeteoEnsembleClient()
        result = client.compute_threshold_probability_from_cache(
            lat=40.77, lon=-73.96,
            target_date=date(2026, 7, 1),
            threshold_f=85.0,
        )
        assert result is None

    def test_empty_result_is_not_usable(self):
        client = OpenMeteoEnsembleClient()
        result = client._empty_result("TICKER", date(2026, 7, 1), 85.0, 40.77, -73.96)
        assert result.is_usable is False
        assert result.probability == pytest.approx(0.5)
        assert result.confidence == "low"


# ---------------------------------------------------------------------------
# NOAAObservationClient tests
# ---------------------------------------------------------------------------

class TestNOAAObservationClient:
    @pytest.mark.asyncio
    async def test_get_station_info_parses_geojson(self):
        """Verify we correctly extract lat/lon from GeoJSON (coordinates are [lon, lat])."""
        client = NOAAObservationClient()

        fake_response = {
            "properties": {
                "name": "New York, Central Park",
                "timeZone": "America/New_York",
                "elevation": {"value": 42.1, "unitCode": "wmoUnit:m"},
            },
            "geometry": {
                "type": "Point",
                "coordinates": [-73.9692, 40.7789],  # GeoJSON: [lon, lat]
            },
        }

        with patch("httpx.AsyncClient") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = fake_response
            mock_resp.raise_for_status = MagicMock()
            mock_http.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )

            info = await client.get_station_info("KNYC")

        assert info["station_id"] == "KNYC"
        assert info["latitude"] == pytest.approx(40.7789)
        assert info["longitude"] == pytest.approx(-73.9692)
        assert info["name"] == "New York, Central Park"
        assert info["timezone"] == "America/New_York"

    @pytest.mark.asyncio
    async def test_get_station_info_cached(self):
        client = NOAAObservationClient()

        fake_response = {
            "properties": {"name": "Test", "timeZone": "UTC", "elevation": {"value": 0}},
            "geometry": {"coordinates": [-73.0, 41.0]},
        }

        with patch("httpx.AsyncClient") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = fake_response
            mock_resp.raise_for_status = MagicMock()
            mock_http.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )

            await client.get_station_info("KNYC")
            await client.get_station_info("KNYC")  # second call

            assert mock_http.return_value.__aenter__.return_value.get.call_count == 1

    def test_validate_coordinates_pass(self):
        client = NOAAObservationClient()
        # Manually populate cache
        client._station_cache["KNYC"] = {
            "latitude": 40.7789, "longitude": -73.9692,
        }
        ok, msg = client.validate_station_coordinates("KNYC", 40.7789, -73.9692)
        assert ok is True
        assert "OK" in msg

    def test_validate_coordinates_fail_large_error(self):
        client = NOAAObservationClient()
        client._station_cache["KNYC"] = {
            "latitude": 40.7789, "longitude": -73.9692,
        }
        # 10 miles off in latitude
        ok, msg = client.validate_station_coordinates("KNYC", 40.65, -73.9692)
        assert ok is False
        assert "MISMATCH" in msg

    def test_validate_coordinates_not_fetched(self):
        client = NOAAObservationClient()
        ok, msg = client.validate_station_coordinates("KNYC", 40.77, -73.96)
        assert ok is False
        assert "not yet fetched" in msg

    @pytest.mark.asyncio
    async def test_get_daily_max_celsius_to_fahrenheit(self):
        """Temperature observations come in Celsius, must be converted."""
        client = NOAAObservationClient()

        # 25°C = 77°F
        fake_observations = {
            "features": [
                {
                    "properties": {
                        "temperature": {"value": 25.0, "unitCode": "wmoUnit:degC"},
                        "timestamp": "2026-07-01T14:00:00+00:00",
                    }
                },
                {
                    "properties": {
                        "temperature": {"value": 20.0, "unitCode": "wmoUnit:degC"},
                        "timestamp": "2026-07-01T08:00:00+00:00",
                    }
                },
            ]
        }

        with patch("httpx.AsyncClient") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = fake_observations
            mock_resp.raise_for_status = MagicMock()
            mock_http.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )

            result = await client.get_daily_max_temperature("KNYC", date(2026, 7, 1))

        assert result is not None
        assert result["max_temp_f"] == pytest.approx(77.0)  # max of 25°C and 20°C → 25°C → 77°F
        assert result["observation_count"] == 2

    @pytest.mark.asyncio
    async def test_get_daily_max_returns_none_on_404(self):
        client = NOAAObservationClient()

        with patch("httpx.AsyncClient") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.raise_for_status = MagicMock(side_effect=Exception("404"))
            mock_http.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_resp
            )

            result = await client.get_daily_max_temperature("KNYC", date(2026, 7, 1))

        assert result is None


# ---------------------------------------------------------------------------
# BiasDatabase tests
# ---------------------------------------------------------------------------

class TestBiasDatabase:
    def test_missing_db_returns_zero(self, tmp_path):
        db = BiasDatabase(tmp_path / "nonexistent.db")
        bias = db.get_bias("KNYC", month=7)
        assert bias == pytest.approx(0.0)

    def test_loads_corrections_above_min_samples(self, tmp_path):
        db_path = tmp_path / "station_bias.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE station_bias (
                station_id TEXT, month INTEGER, bias_f REAL,
                sample_count INTEGER, updated_at TEXT,
                PRIMARY KEY (station_id, month)
            )
        """)
        conn.execute(
            "INSERT INTO station_bias VALUES (?, ?, ?, ?, ?)",
            ("KNYC", 7, 1.8, 45, "2026-03-01"),  # 45 samples → above min
        )
        conn.commit()
        conn.close()

        db = BiasDatabase(db_path)
        assert db.get_bias("KNYC", 7) == pytest.approx(1.8)

    def test_skips_corrections_below_min_samples(self, tmp_path):
        db_path = tmp_path / "station_bias.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE station_bias (
                station_id TEXT, month INTEGER, bias_f REAL,
                sample_count INTEGER, updated_at TEXT,
                PRIMARY KEY (station_id, month)
            )
        """)
        conn.execute(
            "INSERT INTO station_bias VALUES (?, ?, ?, ?, ?)",
            ("KNYC", 7, 2.5, 10, "2026-03-01"),  # only 10 samples → skipped
        )
        conn.commit()
        conn.close()

        db = BiasDatabase(db_path)
        assert db.get_bias("KNYC", 7) == pytest.approx(0.0)  # not applied

    def test_case_insensitive_station_id(self, tmp_path):
        db_path = tmp_path / "station_bias.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE station_bias (
                station_id TEXT, month INTEGER, bias_f REAL,
                sample_count INTEGER, updated_at TEXT,
                PRIMARY KEY (station_id, month)
            )
        """)
        conn.execute("INSERT INTO station_bias VALUES (?, ?, ?, ?, ?)", ("KNYC", 7, 1.5, 50, ""))
        conn.commit()
        conn.close()

        db = BiasDatabase(db_path)
        assert db.get_bias("knyc", 7) == pytest.approx(1.5)  # lowercase should work
        assert db.get_bias("KNYC", 7) == pytest.approx(1.5)

    def test_missing_station_returns_zero(self, tmp_path):
        db_path = tmp_path / "station_bias.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE station_bias (
                station_id TEXT, month INTEGER, bias_f REAL,
                sample_count INTEGER, updated_at TEXT, PRIMARY KEY (station_id, month)
            )
        """)
        conn.execute("INSERT INTO station_bias VALUES (?, ?, ?, ?, ?)", ("KORD", 7, 0.5, 50, ""))
        conn.commit()
        conn.close()

        db = BiasDatabase(db_path)
        assert db.get_bias("KNYC", 7) == pytest.approx(0.0)  # different station


# ---------------------------------------------------------------------------
# ProbabilityEngine integration tests
# ---------------------------------------------------------------------------

class TestProbabilityEngine:
    def _setup(self, member_temps: list[float], bias: float = 0.0):
        target = date(2026, 7, 4)
        ensemble_client = OpenMeteoEnsembleClient()
        ensemble_data = _make_ensemble_data(target, member_temps)
        # Pre-populate cache
        cache_key = (round(40.7789, 4), round(-73.9692, 4), tuple(ensemble_client.models))
        import time
        ensemble_client._cache[cache_key] = (ensemble_data, time.monotonic())

        bias_db = MagicMock(spec=BiasDatabase)
        bias_db.get_bias.return_value = bias

        engine = ProbabilityEngine(ensemble_client=ensemble_client, bias_db=bias_db)
        station_config = {
            "station_id": "KNYC",
            "latitude": 40.7789,
            "longitude": -73.9692,
        }
        market = MarketInfo(
            ticker="KXHIGHNY-26JUL04-T85",
            resolution_date=target,
            threshold_value=85.0,
            series_ticker="KXHIGHNY",
        )
        return engine, market, station_config

    @pytest.mark.asyncio
    async def test_estimate_returns_correct_probability(self):
        # 40 members at 90°F (above 85), 10 at 80°F (below 85) → P=0.8
        temps = [90.0] * 40 + [80.0] * 10
        engine, market, station = self._setup(temps)
        result = await engine.estimate(market, station)
        assert result.probability == pytest.approx(0.8)
        assert result.total_members == 50

    @pytest.mark.asyncio
    async def test_estimate_applies_bias_from_db(self):
        # All members at 87°F, bias=2°F (model runs warm)
        # corrected = 85°F, which meets threshold → P=1.0
        engine, market, station = self._setup([87.0] * 30, bias=2.0)
        result = await engine.estimate(market, station)
        assert result.probability == pytest.approx(1.0)
        assert result.bias_correction_applied == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_estimate_multiple_single_fetch(self):
        """estimate_multiple should fetch ensemble once for all markets at same station."""
        target = date(2026, 7, 4)
        ensemble_client = OpenMeteoEnsembleClient()
        bias_db = MagicMock(spec=BiasDatabase)
        bias_db.get_bias.return_value = 0.0
        engine = ProbabilityEngine(ensemble_client=ensemble_client, bias_db=bias_db)

        station = {"station_id": "KNYC", "latitude": 40.7789, "longitude": -73.9692}
        markets = [
            MarketInfo("TICKER-T80", target, 80.0, "KXHIGHNY"),
            MarketInfo("TICKER-T85", target, 85.0, "KXHIGHNY"),
            MarketInfo("TICKER-T90", target, 90.0, "KXHIGHNY"),
        ]

        fetch_count = 0
        original_fetch = ensemble_client.fetch

        async def counting_fetch(*args, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            return _make_ensemble_data(target, [85.0] * 30)

        ensemble_client.fetch = counting_fetch

        results = await engine.estimate_multiple(markets, station)

        assert fetch_count == 1  # only one HTTP call for three markets
        assert len(results) == 3
        assert "TICKER-T80" in results
        assert "TICKER-T85" in results
        assert "TICKER-T90" in results

    @pytest.mark.asyncio
    async def test_estimate_handles_fetch_failure(self):
        """Engine should return empty (not crash) if ensemble fetch fails."""
        ensemble_client = OpenMeteoEnsembleClient()
        bias_db = MagicMock(spec=BiasDatabase)
        bias_db.get_bias.return_value = 0.0
        engine = ProbabilityEngine(ensemble_client=ensemble_client, bias_db=bias_db)

        async def failing_fetch(*args, **kwargs):
            raise ConnectionError("Open-Meteo unreachable")

        ensemble_client.fetch = failing_fetch

        station = {"station_id": "KNYC", "latitude": 40.7789, "longitude": -73.9692}
        market = MarketInfo("TICKER", date(2026, 7, 4), 85.0, "KXHIGHNY")

        result = await engine.estimate(market, station)

        assert result.is_usable is False
        assert result.probability == pytest.approx(0.5)
