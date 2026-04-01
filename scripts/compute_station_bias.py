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
NOAA_BASE = "https://api.weather.gov"
NOAA_HEADERS = {"User-Agent": "KalshiWeatherTrader/1.0 (bias-correction-script)"}
MIN_POINTS_FOR_BIAS = 10  # minimum observations per station/month
REQUEST_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# NOAA batch fetch — one call per month for efficiency
# ---------------------------------------------------------------------------

async def fetch_noaa_monthly_maxes(
    client: httpx.AsyncClient,
    station_id: str,
    year: int,
    month: int,
) -> dict[date, float]:
    """
    Fetch all daily max temperatures for a station/month from NOAA.

    Returns dict of {date: max_temp_f}. Missing dates are absent from the dict.
    Temperatures are converted from Celsius to Fahrenheit.
    """
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    url = f"{NOAA_BASE}/stations/{station_id}/observations"
    params = {
        "start": f"{month_start.isoformat()}T00:00:00Z",
        "end":   f"{month_end.isoformat()}T23:59:59Z",
    }

    try:
        resp = await client.get(url, params=params, headers=NOAA_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception as exc:
        console.print(f"  [yellow]NOAA {station_id} {year}-{month:02d}: {exc}[/yellow]")
        return {}

    # Build daily max temps: accumulate hourly observations per day
    daily_readings: dict[date, list[float]] = defaultdict(list)
    for obs in features:
        props = obs.get("properties", {})
        ts_raw = props.get("timestamp")
        if not ts_raw:
            continue
        try:
            obs_date = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue

        # maxTemperatureLast24Hours is the authoritative field
        max_24h = props.get("maxTemperatureLast24Hours", {})
        temp_c = max_24h.get("value") if isinstance(max_24h, dict) else None

        # Fall back to instantaneous temperature
        if temp_c is None:
            temp_obj = props.get("temperature", {})
            temp_c = temp_obj.get("value") if isinstance(temp_obj, dict) else None

        if temp_c is not None:
            temp_f = temp_c * 9.0 / 5.0 + 32.0
            daily_readings[obs_date].append(temp_f)

    return {d: max(temps) for d, temps in daily_readings.items() if temps}


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
                noaa_data  = await fetch_noaa_monthly_maxes(client, station_id, year, month)
                model_data = await fetch_model_monthly_maxes(client, lat, lon, year, month)

                matched = 0
                for d, actual in noaa_data.items():
                    model = model_data.get(d)
                    if model is not None:
                        monthly_pairs[month].append((model, actual))
                        matched += 1

                console.print(
                    f"  {year}-{month:02d}: NOAA={len(noaa_data)} model={len(model_data)} matched={matched}"
                )

            # Compute and store bias per month
            stored = 0
            for month in range(1, 13):
                pairs = monthly_pairs.get(month, [])
                if len(pairs) < MIN_POINTS_FOR_BIAS:
                    continue
                bias = sum(m - a for m, a in pairs) / len(pairs)
                bias_db.update_bias(station_id=station_id, month=month, bias_f=round(bias, 3))
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
