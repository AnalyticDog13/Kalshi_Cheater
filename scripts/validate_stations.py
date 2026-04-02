"""
validate_stations.py — Verify station_mapping.yaml against live data sources.

For each configured Kalshi series, checks:
  1. NOAA station coordinates match our config (within 0.05 degree tolerance)
  2. Kalshi series settlement_sources text contains the expected station ID

Run this before any live trading and after adding new series to station_mapping.yaml.

Usage:
    python scripts/validate_stations.py [--config-dir ./config]

Exit codes:
    0 — all stations validated
    1 — one or more stations failed validation
"""

from __future__ import annotations

import asyncio
import os
import sys

# Project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

from src.data_feeds.weather_observations import NOAAObservationClient
from src.kalshi_client import KalshiClient

console = Console()
PASS = "[bold green]PASS[/bold green]"
WARN = "[bold yellow]WARN[/bold yellow]"
FAIL = "[bold red]FAIL[/bold red]"


async def validate_all(config_dir: str) -> int:
    """Run all station validations. Returns number of failures."""
    load_dotenv()

    # Load configs
    with open(os.path.join(config_dir, "station_mapping.yaml")) as f:
        data = yaml.safe_load(f)
    station_mapping: dict = data.get("stations", {})

    if not station_mapping:
        console.print("[red]No stations found in station_mapping.yaml[/red]")
        return 1

    noaa = NOAAObservationClient()
    kalshi = KalshiClient.from_env(mode=os.environ.get("TRADING_MODE", "demo"))

    table = Table(
        "Series", "Station ID", "City", "NOAA Coords", "Kalshi Source", "Result",
        box=box.ROUNDED,
        title="Station Mapping Validation",
    )

    failures = 0

    for series_ticker, cfg in station_mapping.items():
        station_id   = cfg.get("station_id", "")
        config_lat   = cfg.get("latitude", 0.0)
        config_lon   = cfg.get("longitude", 0.0)
        city         = cfg.get("city", series_ticker)

        # --- Check 1: NOAA coordinate validation ----------------------------
        try:
            await noaa.get_station_info(station_id)  # populate cache before sync check
            coord_ok, coord_msg = noaa.validate_station_coordinates(
                station_id, config_lat, config_lon, tolerance_degrees=0.05
            )
            noaa_result = PASS if coord_ok else WARN
            noaa_detail = coord_msg
        except Exception as exc:
            coord_ok = False
            noaa_result = FAIL
            noaa_detail = str(exc)[:60]

        # --- Check 2: Kalshi series settlement_sources ----------------------
        try:
            series_obj = await kalshi.get_series(series_ticker)
            sources_raw = getattr(series_obj, "settlement_sources", None) or []

            # settlement_sources is a list of dicts or objects with url/name fields
            sources_text = " ".join(
                str(getattr(s, "url", "") or s.get("url", "") if hasattr(s, "get") else getattr(s, "url", ""))
                + " "
                + str(getattr(s, "name", "") or s.get("name", "") if hasattr(s, "get") else getattr(s, "name", ""))
                for s in sources_raw
            ).upper()

            station_found = station_id.upper() in sources_text
            kalshi_result = PASS if station_found else WARN
            kalshi_detail = "found" if station_found else f"{station_id!r} not in sources"
        except Exception as exc:
            kalshi_result = WARN
            kalshi_detail = f"API err: {str(exc)[:40]}"
            station_found = None

        # --- Overall result -------------------------------------------------
        if noaa_result == FAIL or (noaa_result == WARN and kalshi_result == FAIL):
            overall = FAIL
            failures += 1
        elif noaa_result == WARN or kalshi_result == WARN:
            overall = WARN
        else:
            overall = PASS

        table.add_row(
            series_ticker,
            station_id,
            city,
            f"{noaa_result}: {noaa_detail[:45]}",
            f"{kalshi_result}: {kalshi_detail[:35]}",
            overall,
        )

    console.print(table)

    total = len(station_mapping)
    console.print(
        f"\n[bold]Result:[/bold] {total - failures}/{total} stations validated successfully."
    )

    if failures:
        console.print(
            "[yellow]WARN rows need manual review — coordinates may differ slightly "
            "from NWS station coordinates. FAIL rows must be fixed before trading.[/yellow]"
        )

    return failures


@click.command()
@click.option("--config-dir", default="./config", show_default=True, help="Config directory.")
def main(config_dir: str) -> None:
    """Validate station_mapping.yaml against NOAA and Kalshi APIs."""
    failures = asyncio.run(validate_all(config_dir))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
