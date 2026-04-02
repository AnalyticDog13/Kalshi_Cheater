"""
backtest.py — Simulate the trading pipeline on historical Kalshi weather markets.

Requires market snapshots from collect_historical.py.

Two modes (--mode flag):
  forecast (default): fetches the archived model forecast that would have been
    available at trading time, using the Open-Meteo Previous Runs API
    (previous-runs-api.open-meteo.com). Results are cached in
    data/backtest_cache.db to avoid re-fetching. This is the correct,
    unbiased approach.

  hindcast: uses Open-Meteo Historical Forecast API for the model's best
    reconstruction of what happened. Faster (no cache cold-start) but
    introduces lookahead bias — results will look better than achievable
    in practice. Use only for quick sanity checks.

For each market snapshot in the date range:
  1. Fetch ensemble probability (from appropriate API)
  2. Apply bias correction from station_bias.db
  3. Compute edge (using conservative params from settings.yaml)
  4. Apply risk checks
  5. Simulate paper trade at the recorded market_price
  6. Compute P&L using the stored outcome

Output: P&L, Brier score, beat-market rate, fill rate, per-city breakdown.

Usage:
    python scripts/backtest.py \\
        [--snapshots ./data/market_snapshots.db] \\
        [--bias-db ./data/station_bias.db] \\
        [--config-dir ./config] \\
        [--mode forecast|hindcast] \\
        [--start-date 2025-01-01] \\
        [--end-date 2025-12-31] \\
        [--portfolio-value 1000.0]

Notes:
  - Only markets with known outcomes (outcome IS NOT NULL) are included.
  - The backtest simulates maker fills at market_price (zero fees) and
    taker fills at market_price (with fee). Both are reported separately.
  - forecast mode requires one Previous Runs API call per city-date pair.
    These are cached in data/backtest_cache.db. First run for a year of
    data across 10 cities takes ~15 minutes (rate limits permitting).
"""

from __future__ import annotations

import asyncio
import math
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
from sqlite_utils import Database

from src.calibration_engine import BiasDatabase
from src.edge_calculator import EdgeParams, compute_edge
from src.risk_manager import RiskLimits
from src.market_scanner import parse_direction_from_text

console = Console()

PREV_RUNS_URL       = "https://previous-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_FORE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT     = 30.0

CACHE_TABLE = "forecast_cache"


# ---------------------------------------------------------------------------
# Forecast cache (SQLite)
# ---------------------------------------------------------------------------

def _ensure_cache_schema(db: Database) -> None:
    if CACHE_TABLE not in db.table_names():
        db[CACHE_TABLE].create({
            "cache_key":        str,   # "{lat}_{lon}_{date}"
            "target_date":      str,
            "lat":              float,
            "lon":              float,
            "model_max_f":      float,
            "fetched_at":       str,
        }, pk="cache_key", ignore=True)


def _cache_key(lat: float, lon: float, target_date: date) -> str:
    return f"{lat:.4f}_{lon:.4f}_{target_date.isoformat()}"


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------

async def fetch_forecast_max(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    target_date: date,
    mode: str,
    cache_db: Database,
) -> float | None:
    """
    Fetch the model's predicted daily max temp for a location and date.

    forecast mode: Previous Runs API (point-in-time archived forecast)
    hindcast mode: Historical Forecast API (model reanalysis)

    Returns max temperature in Fahrenheit, or None on failure.
    Caches results to avoid repeated API calls.
    """
    key = _cache_key(lat, lon, target_date)

    # Cache hit
    try:
        row = cache_db[CACHE_TABLE].get(key)
        return row["model_max_f"]
    except Exception:
        pass

    if mode == "forecast":
        # Previous Runs API: fetch the 00Z run from 1 day before target
        # (this is what the system would have seen at trading time)
        run_date = target_date - timedelta(days=1)
        params = {
            "latitude":             lat,
            "longitude":            lon,
            "start_date":           run_date.isoformat(),
            "end_date":             run_date.isoformat(),
            "hourly":               "temperature_2m",
            "temperature_unit":     "fahrenheit",
            "models":               "gfs_seamless",
            "timezone":             "UTC",
            "past_days":            2,  # include target date in the forecast window
        }
        url = PREV_RUNS_URL
    else:
        # Historical Forecast API (hindcast)
        params = {
            "latitude":             lat,
            "longitude":            lon,
            "start_date":           target_date.isoformat(),
            "end_date":             target_date.isoformat(),
            "daily":                "temperature_2m_max",
            "temperature_unit":     "fahrenheit",
            "models":               "gfs_seamless",
            "timezone":             "UTC",
        }
        url = HISTORICAL_FORE_URL

    try:
        resp = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        console.print(f"  [yellow]Forecast fetch error ({lat},{lon} {target_date}): {exc}[/yellow]")
        return None

    model_max: float | None = None

    if mode == "forecast":
        # Parse hourly data, compute daily max for target_date
        times = payload.get("hourly", {}).get("time", [])
        temps = payload.get("hourly", {}).get("temperature_2m", [])
        day_temps = [
            t for ts, t in zip(times, temps)
            if ts and ts.startswith(target_date.isoformat()) and t is not None
        ]
        if day_temps:
            model_max = max(day_temps)
    else:
        # Parse daily max directly
        dates_raw = payload.get("daily", {}).get("time", [])
        temps_raw = payload.get("daily", {}).get("temperature_2m_max", [])
        for d_str, t in zip(dates_raw, temps_raw):
            if d_str == target_date.isoformat() and t is not None:
                model_max = float(t)
                break

    if model_max is not None:
        cache_db[CACHE_TABLE].insert({
            "cache_key":  key,
            "target_date": target_date.isoformat(),
            "lat":         lat,
            "lon":         lon,
            "model_max_f": model_max,
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }, ignore=True)

    return model_max


# ---------------------------------------------------------------------------
# Probability from a single model max (deterministic — no ensemble spread)
# ---------------------------------------------------------------------------

def _single_member_probability(model_max_f: float, threshold_f: float) -> float:
    """
    Convert a single deterministic forecast to a pseudo-probability.

    Since the Previous Runs API returns one deterministic forecast (not
    ensemble members), we use a logistic function centered on the threshold
    with a scale derived from typical GFS daily-max forecast error (~3°F std).

    p = sigmoid((model_max - threshold) / scale)

    This is NOT the same as the live ensemble probability. It is an
    approximation for backtest purposes. The live system uses 139 ensemble
    members; this uses one deterministic run. Use results directionally
    only — the backtest tells you whether the *signal* (model vs market)
    was profitable, not the exact magnitude.
    """
    scale = 3.0  # empirical GFS forecast error ~3°F 1-sigma
    x = (model_max_f - threshold_f) / scale
    return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# Main backtest logic
# ---------------------------------------------------------------------------

async def run_backtest(
    snapshots_path: str,
    bias_db_path: str,
    config_dir: str,
    cache_path: str,
    mode: str,
    start_date: date | None,
    end_date: date | None,
    portfolio_value: float,
) -> int:
    load_dotenv()

    # Validate inputs
    if not os.path.exists(snapshots_path):
        console.print(f"[red]Snapshots DB not found: {snapshots_path}[/red]")
        console.print("Run: python scripts/collect_historical.py")
        return 1

    # Load configs
    settings: dict = {}
    cfg_path = os.path.join(config_dir, "settings.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            settings = yaml.safe_load(f) or {}

    with open(os.path.join(config_dir, "station_mapping.yaml")) as f:
        station_data = yaml.safe_load(f)
    station_mapping: dict = station_data.get("stations", {})

    # Build series → station lookup
    series_to_station: dict[str, dict] = {
        s: cfg for s, cfg in station_mapping.items()
    }

    edge_params  = EdgeParams.from_settings(settings)
    risk_limits  = RiskLimits.from_settings(settings)
    bias_db      = BiasDatabase(db_path=bias_db_path)

    snap_db  = Database(snapshots_path)
    cache_db = Database(cache_path)
    _ensure_cache_schema(cache_db)

    if "market_snapshots" not in snap_db.table_names():
        console.print("[red]No market_snapshots table found. Run collect_historical.py first.[/red]")
        return 1

    # Load snapshots in date range with known outcomes
    where_clauses = ["outcome IS NOT NULL"]
    params: list = []
    if start_date:
        where_clauses.append("measurement_date >= ?")
        params.append(start_date.isoformat())
    if end_date:
        where_clauses.append("measurement_date <= ?")
        params.append(end_date.isoformat())

    snapshots = list(snap_db["market_snapshots"].rows_where(
        " AND ".join(where_clauses), params, order_by="measurement_date ASC"
    ))

    if not snapshots:
        console.print("[yellow]No snapshots found in the specified date range with known outcomes.[/yellow]")
        return 1

    console.print(
        f"\n[bold]Backtest mode:[/bold] {mode}  |  "
        f"[bold]Markets:[/bold] {len(snapshots)}  |  "
        f"[bold]Portfolio:[/bold] ${portfolio_value:.0f}\n"
    )
    if mode == "forecast":
        console.print(
            "[dim]Using Previous Runs API — results cached in "
            f"{cache_path}[/dim]\n"
        )
    else:
        console.print(
            "[yellow]Hindcast mode — results have lookahead bias; "
            "use for sanity checks only.[/yellow]\n"
        )

    # ------------------------------------------------------------------
    # Simulate trades
    # ------------------------------------------------------------------
    results = []  # list of result dicts

    async with httpx.AsyncClient() as http_client:
        for i, snap in enumerate(snapshots):
            ticker       = snap["ticker"]
            series       = snap["series_ticker"]
            city         = snap.get("city", series)
            threshold_f  = snap["threshold_f"]
            direction    = snap.get("direction", "above")
            market_price = snap["market_price"]  # last recorded market price
            outcome      = snap["outcome"]       # 1 or 0

            if market_price <= 0 or market_price >= 1:
                continue  # skip extreme/invalid prices

            meas_date = date.fromisoformat(snap["measurement_date"])

            # Station config for this series
            station_cfg = series_to_station.get(series, {})
            if not station_cfg:
                continue
            lat = station_cfg.get("latitude", 0.0)
            lon = station_cfg.get("longitude", 0.0)
            station_id = station_cfg.get("station_id", "")

            # Fetch model forecast
            model_max = await fetch_forecast_max(
                http_client, lat, lon, meas_date, mode, cache_db
            )
            if model_max is None:
                continue

            # Apply bias correction
            month = meas_date.month
            bias_f = bias_db.get_bias(station_id=station_id, month=month)
            corrected_max = model_max - bias_f

            # Convert to probability
            raw_prob = _single_member_probability(corrected_max, threshold_f)

            # For "below" markets, flip probability
            if direction == "below":
                ensemble_prob = 1.0 - raw_prob
            else:
                ensemble_prob = raw_prob

            # Fake EnsembleResult-like for edge calculator
            # (EdgeParams.compute_edge needs market_price, ensemble_prob, confidence, fee_coeff)
            from src.data_feeds.weather_ensemble import EnsembleResult
            from datetime import date as _date
            fake_ensemble = EnsembleResult(
                market_ticker=ticker,
                target_date=meas_date,
                threshold_f=threshold_f,
                station_lat=lat,
                station_lon=lon,
                probability=ensemble_prob,
                total_members=1,      # single deterministic forecast
                members_exceeding=1 if ensemble_prob > 0.5 else 0,
                ensemble_mean=corrected_max,
                ensemble_spread=6.0,  # conservative: assume medium confidence
                std=3.0,
                confidence_interval_90=(corrected_max - 5, corrected_max + 5),
                models_used=["gfs_seamless"],
                member_keys_found=1,
                freshness_minutes=0,
                bias_correction_applied=bias_f,
                fetched_at=datetime.now(timezone.utc),
            )

            # Compute edge
            fee_coefficient = 0.07  # conservative default for backtest
            edge = compute_edge(
                market_ticker=ticker,
                market_price=market_price,
                ensemble_result=fake_ensemble,
                fee_coefficient=fee_coefficient,
                portfolio_balance=portfolio_value,
                max_position_dollars=risk_limits.max_position_dollars,
                params=edge_params,
            )

            # Simple risk checks (exposure/daily-loss state not tracked in backtest)
            would_trade = edge.recommendation in ("TRADE", "SANITY_CHECK")

            # Simulate P&L
            if would_trade:
                entry_price = edge.entry_price
                qty         = edge.recommended_contracts
                side        = "YES" if edge.direction == "BUY_YES" else "NO"

                # Maker fill: zero fees (target scenario)
                if side == "YES":
                    gross_pnl = (outcome - entry_price) * qty
                else:
                    gross_pnl = ((1 - outcome) - entry_price) * qty

                taker_fee = math.ceil(fee_coefficient * qty * entry_price * (1 - entry_price) * 100) / 100
                pnl_maker = gross_pnl              # zero fees
                pnl_taker = gross_pnl - taker_fee  # single taker fee on entry

                results.append({
                    "ticker":        ticker,
                    "city":          city,
                    "date":          meas_date.isoformat(),
                    "ensemble_prob": ensemble_prob,
                    "market_price":  market_price,
                    "direction":     edge.direction,
                    "edge":          edge.raw_edge,
                    "outcome":       outcome,
                    "pnl_maker":     pnl_maker,
                    "pnl_taker":     pnl_taker,
                    "would_trade":   True,
                })
            else:
                results.append({
                    "ticker":        ticker,
                    "city":          city,
                    "date":          meas_date.isoformat(),
                    "ensemble_prob": ensemble_prob,
                    "market_price":  market_price,
                    "direction":     None,
                    "edge":          edge.raw_edge,
                    "outcome":       outcome,
                    "pnl_maker":     0.0,
                    "pnl_taker":     0.0,
                    "would_trade":   False,
                })

            if (i + 1) % 50 == 0:
                console.print(f"  Processed {i + 1}/{len(snapshots)}...")

    # ------------------------------------------------------------------
    # Compute metrics
    # ------------------------------------------------------------------
    traded   = [r for r in results if r["would_trade"]]
    skipped  = [r for r in results if not r["would_trade"]]

    total_pnl_maker = sum(r["pnl_maker"] for r in traded)
    total_pnl_taker = sum(r["pnl_taker"] for r in traded)
    wins_maker = sum(1 for r in traded if r["pnl_maker"] > 0)
    wins_taker = sum(1 for r in traded if r["pnl_taker"] > 0)

    # Brier score on ALL markets evaluated (not just traded ones)
    brier = (
        sum((r["ensemble_prob"] - r["outcome"]) ** 2 for r in results) / len(results)
        if results else None
    )

    # Beat-market rate
    beats = sum(
        1 for r in results
        if abs(r["ensemble_prob"] - r["outcome"]) < abs(r["market_price"] - r["outcome"])
    )
    bmr = beats / len(results) if results else None

    # ------------------------------------------------------------------
    # Print results
    # ------------------------------------------------------------------
    console.print(f"\n[bold]Backtest Results[/bold]  "
                  f"({len(results)} markets, {len(traded)} traded)\n")

    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="cyan", no_wrap=True)
    overview.add_column()
    overview.add_row("Markets evaluated",    str(len(results)))
    overview.add_row("Would-trade signals",  f"{len(traded)} ({len(traded)/len(results):.1%})")
    overview.add_row("Brier score (all)",    f"{brier:.4f}" if brier is not None else "N/A")
    overview.add_row("Beat-market rate",     f"{bmr:.1%}" if bmr is not None else "N/A")
    overview.add_row("P&L (maker fills)",    f"${total_pnl_maker:+.2f}")
    overview.add_row("P&L (taker fills)",    f"${total_pnl_taker:+.2f}")
    overview.add_row("Win rate (maker)",     f"{wins_maker/len(traded):.1%}" if traded else "N/A")
    overview.add_row("ROI (maker, on capital deployed)",
        f"{total_pnl_maker / portfolio_value:.2%}" if portfolio_value else "N/A")
    console.print(overview)

    # Per-city breakdown
    by_city: dict[str, list[dict]] = defaultdict(list)
    for r in traded:
        by_city[r["city"]].append(r)

    if by_city:
        city_table = Table("City", "Traded", "Win Rate", "P&L (maker)", "P&L (taker)",
                           box=box.ROUNDED)
        for city, rows in sorted(by_city.items()):
            wins = sum(1 for r in rows if r["pnl_maker"] > 0)
            city_table.add_row(
                city,
                str(len(rows)),
                f"{wins/len(rows):.1%}",
                f"${sum(r['pnl_maker'] for r in rows):+.2f}",
                f"${sum(r['pnl_taker'] for r in rows):+.2f}",
            )
        console.print()
        console.print(city_table)

    if mode == "hindcast":
        console.print(
            "\n[yellow]Reminder: hindcast results are optimistic due to lookahead bias. "
            "Run with --mode forecast for unbiased results.[/yellow]"
        )
    elif mode == "forecast":
        console.print(
            "\n[dim]Note: forecast mode uses a single deterministic GFS run, not 139 ensemble members. "
            "Edge estimates will differ from the live system. "
            "Use this to validate the directional signal, not exact P&L magnitudes.[/dim]"
        )

    return 0


@click.command()
@click.option("--snapshots",       default="./data/market_snapshots.db",  show_default=True)
@click.option("--bias-db",         default="./data/station_bias.db",      show_default=True)
@click.option("--config-dir",      default="./config",                    show_default=True)
@click.option("--cache",           default="./data/backtest_cache.db",    show_default=True,
              help="SQLite cache for forecast API results.")
@click.option("--mode",            default="forecast",
              type=click.Choice(["forecast", "hindcast"]),                 show_default=True,
              help="forecast=Previous Runs API (unbiased). hindcast=Historical Forecast (faster, biased).")
@click.option("--start-date",      default=None,
              help="Start date for backtest (ISO format, e.g. 2025-01-01).")
@click.option("--end-date",        default=None,
              help="End date for backtest (ISO format).")
@click.option("--portfolio-value", default=1000.0, show_default=True,
              help="Simulated portfolio value for Kelly sizing.")
def main(
    snapshots: str,
    bias_db: str,
    config_dir: str,
    cache: str,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    portfolio_value: float,
) -> None:
    """Simulate the trading pipeline on historical Kalshi weather market snapshots."""
    start = date.fromisoformat(start_date) if start_date else None
    end   = date.fromisoformat(end_date)   if end_date   else None
    rc = asyncio.run(run_backtest(
        snapshots, bias_db, config_dir, cache, mode, start, end, portfolio_value
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
