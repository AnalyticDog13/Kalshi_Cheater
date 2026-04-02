"""
compute_station_bias.py — Compute per-station, per-month temperature model bias.

Algorithm:
  For each station in station_mapping.yaml:
    1. Fetch NOAA actual daily max temperatures for the last N days
    2. Fetch Open-Meteo historical forecast daily max temperatures for the
       same location and date range (uses the historical-forecast API, which
       returns model hindcast values — the model's best reconstruction of
       what happened, comparable to what would have been available at trading time)
    3. Compute bias = mean(model_max - actual_max) per calendar month
    4. Write to station_bias.db via BiasDatabase.update_bias()

Design note on data sources:
  - NOAA observations: ground truth at the specific NWS station
  - Open-Meteo historical-forecast-api.open-meteo.com: model hindcast values
    for the station's lat/lon. The bias measures how the model systematically
    over- or under-predicts temperatures at this location compared to the
    NWS observation. Applying this correction at trading time improves the
    probability estimate.
  - Minimum 10 data points per station/month before writing a correction.
    Stations with too few observations return 0.0 bias (no correction).

Usage:
    python scripts/compute_station_bias.py \\
        [--config-dir ./config] \\
        [--bias-db ./data/station_bias.db] \\
        [--days-back 365]
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

from src.calibration_engine import BiasDatabase

console = Console()

HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ERA5_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_POINTS_FOR_BIAS = 10  # minimum observations per station/month
REQUEST_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# ERA5 archive fetch — replaces NOAA observations as ground truth source.
#
# Why ERA5 instead of NOAA api.weather.gov observations?
# The NOAA observations API (api.weather.gov/stations/{id}/observations) only
# retains ~30-60 days of data online. Months older than that return 0 features.
# ERA5 is ECMWF's reanalysis dataset: free, no API key, global coverage back to
# 1940, and accurate to ~0.5°F vs station observations at major airports.
# The bias (GFS_hindcast - ERA5_actual) is the correct quantity to correct for
# because it measures how much the live ensemble over/underpredicts reality at
# each station's exact lat/lon.
# ---------------------------------------------------------------------------

async def fetch_era5_monthly_maxes(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    year: int,
    month: int,
) -> dict[date, float]:
    """
    Fetch daily max temperatures from the Open-Meteo ERA5 archive API.

    ERA5 is ECMWF's reanalysis — it fuses model output with actual observations
    to produce the best estimate of what really happened. Returns one value per
    calendar day, already in Fahrenheit.

    Returns dict of {date: max_temp_f}. Missing dates are absent.
    """
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    # ERA5 archive has ~5-day lag; clamp end to 5 days ago
    latest_available = date.today() - timedelta(days=5)
    if month_end > latest_available:
        month_end = latest_available
    if month_start > latest_available:
        return {}

    params = {
        "latitude":         lat,
        "longitude":        lon,
        "start_date":       month_start.isoformat(),
        "end_date":         month_end.isoformat(),
        "daily":            "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone":         "UTC",
    }

    try:
        resp = await client.get(ERA5_ARCHIVE_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        console.print(f"  [yellow]ERA5 ({lat},{lon}) {year}-{month:02d}: {exc}[/yellow]")
        return {}

    dates_raw = payload.get("daily", {}).get("time", [])
    temps_raw = payload.get("daily", {}).get("temperature_2m_max", [])

    result: dict[date, float] = {}
    for d_str, t in zip(dates_raw, temps_raw):
        if t is None:
            continue
        try:
            result[date.fromisoformat(d_str)] = float(t)
        except (ValueError, TypeError):
            continue

    return result


# ---------------------------------------------------------------------------
# Open-Meteo historical forecast batch fetch — one call per station/month
# ---------------------------------------------------------------------------

async def fetch_model_monthly_maxes(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    year: int,
    month: int,
) -> dict[date, float]:
    """
    Fetch daily max temperatures from the Open-Meteo Historical Forecast API
    for a specific location and month.

    Uses GFS seamless as the primary model (same as live trading).
    Returns dict of {date: max_temp_f}.
    """
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    # Clamp end date to yesterday (historical API doesn't serve today)
    yesterday = date.today() - timedelta(days=1)
    if month_end > yesterday:
        month_end = yesterday
    if month_start > yesterday:
        return {}

    params = {
        "latitude":         lat,
        "longitude":        lon,
        "start_date":       month_start.isoformat(),
        "end_date":         month_end.isoformat(),
        "daily":            "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "models":           "gfs_seamless",  # match live trading model
        "timezone":         "UTC",
    }

    try:
        resp = await client.get(HISTORICAL_FORECAST_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        console.print(f"  [yellow]Open-Meteo historical ({lat},{lon}) {year}-{month:02d}: {exc}[/yellow]")
        return {}

    dates_raw  = payload.get("daily", {}).get("time", [])
    temps_raw  = payload.get("daily", {}).get("temperature_2m_max", [])

    result: dict[date, float] = {}
    for d_str, t in zip(dates_raw, temps_raw):
        if t is None:
            continue
        try:
            result[date.fromisoformat(d_str)] = float(t)
        except (ValueError, TypeError):
            continue

    return result


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

async def compute_bias(config_dir: str, bias_db_path: str, days_back: int) -> None:
    load_dotenv()

    with open(os.path.join(config_dir, "station_mapping.yaml")) as f:
        data = yaml.safe_load(f)
    station_mapping: dict = data.get("stations", {})

    bias_db = BiasDatabase(db_path=bias_db_path)

    # Date range: last N days, grouped by (year, month)
    end_date   = date.today() - timedelta(days=1)  # yesterday = last complete day
    start_date = end_date - timedelta(days=days_back)

    # Build list of (year, month) pairs to fetch
    ym_pairs: list[tuple[int, int]] = []
    cur = date(start_date.year, start_date.month, 1)
    while cur <= end_date:
        ym_pairs.append((cur.year, cur.month))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    summary_rows: list[tuple] = []

    async with httpx.AsyncClient() as client:
        for series_ticker, cfg in station_mapping.items():
            station_id = cfg.get("station_id", "")
            lat        = cfg.get("latitude", 0.0)
            lon        = cfg.get("longitude", 0.0)
            city       = cfg.get("city", series_ticker)

            console.print(f"\n[bold]{city}[/bold] ({station_id})  {len(ym_pairs)} months...")

            # Accumulate matched (model_max, actual_max) pairs per month
            monthly_pairs: dict[int, list[tuple[float, float]]] = defaultdict(list)

            for year, month in ym_pairs:
                era5_data  = await fetch_era5_monthly_maxes(client, lat, lon, year, month)
                model_data = await fetch_model_monthly_maxes(client, lat, lon, year, month)

                matched = 0
                for d, actual in era5_data.items():
                    model = model_data.get(d)
                    if model is not None:
                        monthly_pairs[month].append((model, actual))
                        matched += 1

                console.print(
                    f"  {year}-{month:02d}: ERA5={len(era5_data)} model={len(model_data)} matched={matched}"
                )

            # Compute and store bias per month
            stored = 0
            for month in range(1, 13):
                pairs = monthly_pairs.get(month, [])
                if len(pairs) < MIN_POINTS_FOR_BIAS:
                    continue
                bias = sum(m - a for m, a in pairs) / len(pairs)
                bias_db.update_bias(
                    station_id=station_id,
                    month=month,
                    bias_f=round(bias, 3),
                    sample_count=len(pairs),
                )
                stored += 1

            # Summary row
            total_pairs = sum(len(v) for v in monthly_pairs.values())
            months_stored = stored
            all_biases = [
                sum(m - a for m, a in monthly_pairs[mo]) / len(monthly_pairs[mo])
                for mo in monthly_pairs if len(monthly_pairs[mo]) >= MIN_POINTS_FOR_BIAS
            ]
            mean_bias = sum(all_biases) / len(all_biases) if all_biases else float("nan")
            summary_rows.append((city, station_id, total_pairs, months_stored, mean_bias))

    # Print summary table
    table = Table(
        "City", "Station", "Data Points", "Months Written", "Mean Bias (F)",
        box=box.ROUNDED,
        title=f"Station Bias Summary ({days_back}-day window)",
    )
    for city, sid, pts, mths, bias in summary_rows:
        bias_str = f"{bias:+.2f}" if bias == bias else "N/A"  # NaN check
        table.add_row(city, sid, str(pts), str(mths), bias_str)

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]Done.[/bold] Bias corrections written to [cyan]{bias_db_path}[/cyan]. "
        "Run monthly to keep corrections fresh."
    )


@click.command()
@click.option("--config-dir", default="./config", show_default=True)
@click.option("--bias-db",    default="./data/station_bias.db", show_default=True,
              help="Path to station bias SQLite database.")
@click.option("--days-back",  default=365, show_default=True,
              help="Number of historical days to analyze.")
def main(config_dir: str, bias_db: str, days_back: int) -> None:
    """Compute per-station, per-month temperature model bias and write to station_bias.db."""
    asyncio.run(compute_bias(config_dir, bias_db, days_back))


if __name__ == "__main__":
    main()
