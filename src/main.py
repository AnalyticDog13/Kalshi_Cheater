"""
CLI entry point for the Kalshi AI Weather Trading System.

Five subcommands:
  start   — load config, start daemon, block until stopped
  stop    — send SIGTERM to the running daemon via PID file
  status  — show open positions, P&L, Brier score from DB
  pause   — write pause flag: daemon scans but skips order execution
  resume  — remove pause flag: daemon resumes executing orders

PID file:   ./data/daemon.pid   (written on start, removed on clean shutdown)
Pause flag: ./data/daemon.pause (written by pause, removed by resume)

No unit tests — this is a thin CLI wrapper over TradingDaemon.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

from src.daemon import TradingDaemon

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

DATA_DIR        = Path("./data")
PID_FILE        = DATA_DIR / "daemon.pid"
PAUSE_FLAG      = DATA_DIR / "daemon.pause"

DEFAULT_CONFIG_DIR = "./config"
DEFAULT_DB_PATH    = "./data/trades.db"


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Kalshi AI Weather Trading System."""


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--config-dir",
    default=DEFAULT_CONFIG_DIR,
    show_default=True,
    help="Directory containing settings.yaml, station_mapping.yaml, market_categories.yaml.",
)
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the trades SQLite database.",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help="Enable live trading mode (overrides TRADING_MODE env var).",
)
def start(config_dir: str, db_path: str, live: bool) -> None:
    """Start the trading daemon (blocks until stopped)."""
    load_dotenv()

    if live:
        os.environ["TRADING_MODE"] = "live"
        click.echo(
            "WARNING: LIVE TRADING MODE ENABLED. Real orders will be placed.",
            err=True,
        )
    else:
        # Honour env var; default to demo if unset
        mode = os.environ.get("TRADING_MODE", "demo")
        if mode == "live":
            click.echo(
                "WARNING: TRADING_MODE=live from environment. Real orders will be placed.",
                err=True,
            )

    # Abort if another instance is already running
    if PID_FILE.exists():
        existing_pid = _read_pid()
        if existing_pid is not None and _is_running(existing_pid):
            click.echo(
                f"Daemon is already running (PID {existing_pid}). "
                "Use 'stop' to shut it down first.",
                err=True,
            )
            sys.exit(1)
        else:
            # Stale PID file from a previous crash — clean it up
            PID_FILE.unlink(missing_ok=True)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    _configure_logging()

    daemon = TradingDaemon.from_config(
        config_dir=config_dir,
        db_path=db_path,
        pause_flag_path=str(PAUSE_FLAG),
    )

    # Write PID so stop/status can find us
    PID_FILE.write_text(str(os.getpid()))
    click.echo(f"Daemon started (PID {os.getpid()}). Press Ctrl+C to stop.")

    try:
        asyncio.run(daemon.run())
    finally:
        PID_FILE.unlink(missing_ok=True)
        click.echo("Daemon stopped cleanly.")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

@cli.command()
def stop() -> None:
    """Send SIGTERM to the running daemon."""
    pid = _read_pid()

    if pid is None:
        click.echo("Daemon is not running (PID file not found).")
        sys.exit(1)

    if not _is_running(pid):
        click.echo(f"No process found at PID {pid}. Removing stale PID file.")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"SIGTERM sent to PID {pid}. Daemon will stop after the current cycle.")
    except PermissionError:
        click.echo(f"Permission denied sending signal to PID {pid}.", err=True)
        sys.exit(1)
    except OSError as exc:
        click.echo(f"Failed to signal daemon: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the trades SQLite database.",
)
def status(db_path: str) -> None:
    """Show daemon status, open positions, P&L, and calibration metrics."""
    from src.tracker import Tracker

    # --- Daemon state --------------------------------------------------------
    pid = _read_pid()
    running = pid is not None and _is_running(pid)
    paused = PAUSE_FLAG.exists()

    click.echo("=== Kalshi Trader Status ===")
    if running:
        state = f"RUNNING  (PID {pid})"
        trade_state = "PAUSED (scanning only)" if paused else "ACTIVE"
    else:
        state = "STOPPED"
        trade_state = "N/A"

    click.echo(f"Daemon:   {state}")
    click.echo(f"Trading:  {trade_state}")

    # --- Database metrics ----------------------------------------------------
    db_file = Path(db_path)
    if not db_file.exists():
        click.echo(f"\nDatabase not found at {db_path}. No trades recorded yet.")
        return

    tracker = Tracker(db_path=db_path)

    # Open positions (conservative arm only — those are real fills)
    all_open = tracker.get_open_positions()
    positions = [p for p in all_open if p.strategy_arm == "conservative"]

    click.echo(f"\n--- Open Positions ({len(positions)}) ---")
    if positions:
        header = f"{'Ticker':<32} {'Side':<4} {'Qty':>5} {'Entry':>7} {'Edge':>7} {'City':<15}"
        click.echo(header)
        click.echo("-" * len(header))
        for p in positions:
            click.echo(
                f"{p.market_ticker:<32} {p.side:<4} {p.quantity:>5.1f} "
                f"{p.entry_price:>7.4f} {p.edge_at_entry:>7.1%} {p.city:<15}"
            )
    else:
        click.echo("  (none)")

    # P&L
    daily_pnl = tracker.compute_daily_pnl()
    today_str = date.today().isoformat()
    today_pnl = daily_pnl.get(today_str, None)
    total_pnl = sum(daily_pnl.values()) if daily_pnl else None

    click.echo("\n--- P&L ---")
    if today_pnl is not None:
        click.echo(f"Today ({today_str}):  ${today_pnl:+.2f}")
    else:
        click.echo(f"Today ({today_str}):  no resolved trades")
    if total_pnl is not None:
        click.echo(f"All-time:            ${total_pnl:+.2f}")
    else:
        click.echo("All-time:            no resolved trades")

    # Brier score
    brier_all = tracker.compute_brier_score()
    brier_50  = tracker.compute_brier_score(n_recent=50)

    click.echo("\n--- Calibration ---")
    click.echo(f"Brier (all):      {brier_all:.4f}" if brier_all is not None else "Brier (all):      N/A (no resolved trades)")
    click.echo(f"Brier (last 50):  {brier_50:.4f}"  if brier_50  is not None else "Brier (last 50):  N/A")

    # Circuit breakers — in-memory state lives inside the daemon process;
    # we can only indicate running/stopped here.
    click.echo("\n--- Circuit Breakers ---")
    if running:
        click.echo("  Live state is held in daemon memory.")
        click.echo("  Check daemon logs for circuit breaker trips.")
    else:
        click.echo("  Daemon not running — all breakers reset to CLOSED on next start.")


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------

@cli.command()
def pause() -> None:
    """Pause order execution (daemon continues scanning)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG.touch()
    click.echo("Pause flag set. Daemon will scan but not execute orders.")
    click.echo("Use 'resume' to re-enable order execution.")

    # Remind operator if daemon is not running
    pid = _read_pid()
    if pid is None or not _is_running(pid):
        click.echo("(Note: daemon is not currently running.)")


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

@cli.command()
def resume() -> None:
    """Resume order execution after a pause."""
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
        click.echo("Pause flag removed. Order execution resumed.")
    else:
        click.echo("Daemon was not paused.")

    pid = _read_pid()
    if pid is None or not _is_running(pid):
        click.echo("(Note: daemon is not currently running.)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_pid() -> int | None:
    """Read the PID from the PID file. Returns None if file absent or invalid."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_running(pid: int) -> bool:
    """Return True if a process with the given PID is alive."""
    import errno as _errno
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it — still running.
        return True
    except OSError as exc:
        return exc.errno == _errno.EPERM


def _configure_logging() -> None:
    """Basic logging configuration for the daemon process."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
