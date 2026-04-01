"""
collect_historical.py — Scrape Kalshi historical (settled) weather markets.

Fetches all resolved weather markets for each series in station_mapping.yaml
and stores them in a SQLite database for backtesting.

Stored fields per market:
  ticker, series_ticker, city, measurement_date, threshold_f, direction,
  close_time, market_price (last_price_dollars at settlement), outcome
  (1=YES resolved, 0=NO resolved, None=unknown), title, volume_fp, collected_at

The output database is separate from trades.db — it records historical market
states, not our own trades.

Usage:
    python scripts/collect_historical.py \\
        [--config-dir ./config] \\
        [--output ./data/market_snapshots.db] \\
        [--max-pages 50]

Notes:
  - Re-running is safe — duplicate tickers are ignored (INSERT OR IGNORE).
  - Kalshi's /historical/markets endpoint returns markets in reverse
    chronological order. Pagination stops when max_pages is reached.
  - Markets where the threshold or measurement date cannot be parsed from
    the ticker are skipped with a warning.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box
from sqlite_utils import Database

from src.kalshi_client import KalshiClient, parse_price, parse_quantity
from src.market_scanner import (
    parse_threshold_from_ticker,
    parse_measurement_date_from_ticker,
    parse_direction_from_text,
)

console = Console()

TABLE = "market_snapshots"


def _ensure_schema(db: Database) -> None:
    if TABLE not in db.table_names():
        db[TABLE].create({
            "ticker":           str,
            "series_ticker":    str,
            "city":             str,
            "measurement_date": str,   # ISO date string
            "threshold_f":      float,
            "direction":        str,   # "above" or "below"
            "close_time":       str,   # ISO datetime string
            "market_price":     float, # last_price_dollars at settlement
            "outcome":          int,   # 1=YES resolved, 0=NO resolved, NULL=unknown
            "title":            str,
            "volume_fp":        float,
            "collected_at":     str,   # when this row was written
        }, pk="ticker", ignore=True)
    else:
        # Ensure table has all expected columns (for re-runs after schema changes)
        existing = {c.name for c in db[TABLE].columns}
        extras = {
            "city": str,
            "direction": str,
            "volume_fp": float,
        }
        for col, col_type in extras.items():
            if col not in existing:
                db[TABLE].add_column(col, col_type)


def _parse_outcome(market) -> int | None:
    """Extract settlement outcome from a Kalshi market object."""
    result = getattr(market, "result", None)
    if result is None:
        return None
    if isinstance(result, str):
        r = result.lower().strip()
        if r in ("yes", "yes_wins"):
            return 1
        if r in ("no", "no_wins"):
            return 0
    return None


async def collect(config_dir: str, output_path: str, max_pages: int) -> None:
    load_dotenv()

    with open(os.path.join(config_dir, "station_mapping.yaml")) as f:
        data = yaml.safe_load(f)
    station_mapping: dict = data.get("stations", {})

    kalshi = KalshiClient.from_env(mode=os.environ.get("TRADING_MODE", "demo"))

    db = Database(output_path)
    _ensure_schema(db)

    now_str = datetime.now(timezone.utc).isoformat()
    summary_rows: list[tuple] = []

    for series_ticker, cfg in station_mapping.items():
        city = cfg.get("city", series_ticker)
        console.print(f"\nFetching [bold]{series_ticker}[/bold] ({city})...")

        try:
            markets = await kalshi.list_historical_markets(
                series_ticker=series_ticker,
                max_pages=max_pages,
            )
        except Exception as exc:
            console.print(f"  [red]API error: {exc}[/red]")
            summary_rows.append((series_ticker, city, 0, 0, str(exc)[:40]))
            continue

        inserted = 0
        skipped_parse = 0
        skipped_dup = 0

        for m in markets:
            ticker = getattr(m, "ticker", None)
            if not ticker:
                continue

            # Parse threshold from ticker (e.g. KXHIGHNY-26APR01-T79 → 79.0)
            threshold_f = parse_threshold_from_ticker(ticker)
            if threshold_f is None:
                skipped_parse += 1
                continue

            # Parse measurement date from ticker
            meas_date = parse_measurement_date_from_ticker(ticker)
            if meas_date is None:
                skipped_parse += 1
                continue

            # Parse direction from title ("above" or "below")
            title = getattr(m, "title", "") or ""
            subtitle = getattr(m, "subtitle", "") or ""
            direction = parse_direction_from_text(f"{title} {subtitle}")

            # Market price at settlement
            market_price = parse_price(getattr(m, "last_price_dollars", None))
            if market_price == 0.0:
                # Fall back to yes_ask or yes_bid as a last resort
                market_price = parse_price(
                    getattr(m, "yes_ask_dollars", None)
                    or getattr(m, "yes_bid_dollars", None)
                )

            # Volume
            volume_fp = parse_quantity(getattr(m, "volume_fp", None))

            # Close time
            close_raw = getattr(m, "close_time", None)
            if close_raw is None:
                close_str = None
            elif isinstance(close_raw, datetime):
                close_str = close_raw.isoformat()
            else:
                close_str = str(close_raw)

            outcome = _parse_outcome(m)

            row = {
                "ticker":           ticker,
                "series_ticker":    series_ticker,
                "city":             city,
                "measurement_date": meas_date.isoformat(),
                "threshold_f":      threshold_f,
                "direction":        direction,
                "close_time":       close_str,
                "market_price":     market_price,
                "outcome":          outcome,
                "title":            title[:200],
                "volume_fp":        volume_fp,
                "collected_at":     now_str,
            }

            try:
                db[TABLE].insert(row, ignore=True)
                inserted += 1
            except Exception:
                skipped_dup += 1

        console.print(
            f"  inserted={inserted}  skipped_parse={skipped_parse}  "
            f"skipped_dup={skipped_dup}  total_fetched={len(markets)}"
        )
        summary_rows.append((series_ticker, city, len(markets), inserted, "OK"))

    # Summary table
    table = Table(
        "Series", "City", "Fetched", "Inserted", "Status",
        box=box.ROUNDED,
        title="Collection Summary",
    )
    for series, city, fetched, inserted, status in summary_rows:
        table.add_row(series, city, str(fetched), str(inserted), status)

    console.print()
    console.print(table)

    total_rows = db[TABLE].count
    console.print(
        f"\n[bold]Done.[/bold] "
        f"Total rows in [cyan]{output_path}[/cyan]: [bold]{total_rows}[/bold]"
    )
    console.print(
        "Use scripts/backtest.py to run a simulated backtest on these snapshots."
    )


@click.command()
@click.option("--config-dir", default="./config",                    show_default=True)
@click.option("--output",     default="./data/market_snapshots.db",  show_default=True,
              help="SQLite database for market snapshots.")
@click.option("--max-pages",  default=50,                            show_default=True,
              help="Maximum pagination pages per series (200 markets/page).")
def main(config_dir: str, output: str, max_pages: int) -> None:
    """Scrape settled Kalshi weather markets and store snapshots for backtesting."""
    asyncio.run(collect(config_dir, output, max_pages))


if __name__ == "__main__":
    main()
