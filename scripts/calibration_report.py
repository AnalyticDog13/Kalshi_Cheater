"""
calibration_report.py — Performance and calibration analysis of recorded trades.

Reads trades.db and prints:
  1. Overall summary (trades, fills, win rate, total P&L, AI spend)
  2. Brier score (all resolved trades, last 50)
  3. Beat-market rate (did our ensemble beat the market price?)
  4. Dual-arm comparison (conservative arm vs exploratory arm)
  5. Per-city breakdown (Brier, win rate, P&L)
  6. Regime check (rolling 50-market Brier vs alert threshold)
  7. Maker fill rate and average slippage

Usage:
    python scripts/calibration_report.py [--db-path ./data/trades.db]

Exit codes:
    0 — report generated successfully
    1 — no data or database not found
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from sqlite_utils import Database

console = Console()

BRIER_ALERT_THRESHOLD    = 0.25
BEAT_MARKET_HALT         = 0.48
BEAT_MARKET_ALERT        = 0.50
ROLLING_WINDOW           = 50
MIN_CITY_MARKETS         = 10   # minimum resolved markets before showing city row


def _load_resolved(db: Database, arm: str | None = None) -> list[dict]:
    """Load all resolved (non-cancelled) trades from the DB."""
    where = "actual_outcome IS NOT NULL AND order_cancelled = 0"
    if arm:
        where += f" AND strategy_arm = '{arm}'"
    return list(db["trades"].rows_where(where, order_by="resolution_time ASC"))


def _brier(rows: list[dict]) -> float | None:
    if not rows:
        return None
    total = sum((r["ensemble_prob_at_entry"] - r["actual_outcome"]) ** 2 for r in rows)
    return total / len(rows)


def _beat_market_rate(rows: list[dict]) -> float | None:
    """
    Beat-market rate: fraction of resolved trades where our ensemble estimate
    was closer to the actual outcome than the market price at signal time.
    """
    if not rows:
        return None
    beats = 0
    for r in rows:
        prob  = r["ensemble_prob_at_entry"]
        mktpx = r["mid_price_at_signal"]
        outcome = r["actual_outcome"]
        our_err = abs(prob - outcome)
        mkt_err = abs(mktpx - outcome)
        if our_err < mkt_err:
            beats += 1
    return beats / len(rows)


def _win_rate(rows: list[dict]) -> float | None:
    filled = [r for r in rows if r.get("order_filled_time") and r.get("pnl") is not None]
    if not filled:
        return None
    wins = sum(1 for r in filled if (r["pnl"] or 0) > 0)
    return wins / len(filled)


def _total_pnl(rows: list[dict]) -> float:
    return sum((r["pnl"] or 0.0) for r in rows)


def _maker_fill_rate(rows: list[dict]) -> float | None:
    filled = [r for r in rows if r.get("order_filled_time")]
    if not filled:
        return None
    makers = sum(1 for r in filled if r.get("was_maker_fill"))
    return makers / len(filled)


def _avg_slippage(rows: list[dict]) -> float | None:
    filled = [r for r in rows if r.get("order_filled_time") and r.get("realized_slippage") is not None]
    if not filled:
        return None
    return sum(r["realized_slippage"] for r in filled) / len(filled)


def _fmt_pct(v: float | None, decimals: int = 1) -> str:
    if v is None:
        return "N/A"
    return f"{v:.{decimals}%}"


def _fmt_float(v: float | None, prefix: str = "", decimals: int = 4) -> str:
    if v is None:
        return "N/A"
    return f"{prefix}{v:.{decimals}f}"


def run_report(db_path: str, config_dir: str) -> int:
    if not os.path.exists(db_path):
        console.print(f"[red]Database not found: {db_path}[/red]")
        return 1

    db = Database(db_path)
    if "trades" not in db.table_names():
        console.print("[red]No 'trades' table found — no data recorded yet.[/red]")
        return 1

    # Load settings for regime thresholds (use defaults if missing)
    settings: dict = {}
    cfg_path = os.path.join(config_dir, "settings.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            settings = yaml.safe_load(f) or {}
    regime_cfg = settings.get("regime", {})
    brier_alert   = regime_cfg.get("brier_alert_threshold", BRIER_ALERT_THRESHOLD)
    beat_halt     = regime_cfg.get("beat_market_halt_threshold", BEAT_MARKET_HALT)
    beat_alert    = regime_cfg.get("beat_market_alert_threshold", BEAT_MARKET_ALERT)

    all_trades    = list(db["trades"].rows_where(order_by="entry_time ASC"))
    resolved_cons = _load_resolved(db, arm="conservative")
    resolved_exp  = _load_resolved(db, arm="exploratory")
    all_resolved  = _load_resolved(db)

    total_placed   = sum(1 for r in all_trades if r["strategy_arm"] == "conservative")
    total_filled   = sum(1 for r in all_trades if r["strategy_arm"] == "conservative"
                         and r.get("order_filled_time"))
    total_resolved = len(resolved_cons)
    total_ai_cost  = sum((r.get("ai_cost_usd") or 0.0) for r in all_trades)

    # -----------------------------------------------------------------------
    # 1. Overall summary
    # -----------------------------------------------------------------------
    console.print(Panel(
        f"[bold]Database:[/bold] {db_path}\n"
        f"[bold]Report date:[/bold] {date.today().isoformat()}",
        title="Kalshi Weather Trader — Calibration Report",
        expand=False,
    ))

    summary_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary_table.add_column(style="bold cyan", no_wrap=True)
    summary_table.add_column()

    fill_rate_str = f"{total_filled/total_placed:.1%}" if total_placed else "N/A"
    summary_table.add_row("Conservative trades placed",   str(total_placed))
    summary_table.add_row("Conservative trades filled",   f"{total_filled}  ({fill_rate_str})")
    summary_table.add_row("Conservative trades resolved", str(total_resolved))
    summary_table.add_row("Total P&L (conservative)",     f"${_total_pnl(resolved_cons):+.2f}")
    summary_table.add_row("Total AI cost",                f"${total_ai_cost:.4f}")
    summary_table.add_row("Win rate",                     _fmt_pct(_win_rate(resolved_cons)))
    summary_table.add_row("Maker fill rate",              _fmt_pct(_maker_fill_rate(resolved_cons)))
    summary_table.add_row("Avg slippage",                 _fmt_float(_avg_slippage(resolved_cons),
                                                                      prefix="$", decimals=4))
    console.print("\n[bold]1. Overall Summary[/bold]")
    console.print(summary_table)

    # -----------------------------------------------------------------------
    # 2. Brier score
    # -----------------------------------------------------------------------
    brier_all    = _brier(all_resolved)
    brier_cons   = _brier(resolved_cons)
    recent_50    = all_resolved[-50:] if len(all_resolved) >= 50 else all_resolved
    brier_roll   = _brier(recent_50)

    roll_status = ""
    if brier_roll is not None:
        if brier_roll > brier_alert:
            roll_status = f" [bold red]ALERT (>{brier_alert})[/bold red]"
        elif brier_roll > 0.22:
            roll_status = f" [yellow]WARN (>0.22)[/yellow]"
        else:
            roll_status = " [green]OK[/green]"

    console.print("\n[bold]2. Brier Score[/bold]  (lower = better; target < 0.18)")
    brier_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    brier_table.add_column(style="cyan", no_wrap=True)
    brier_table.add_column()
    brier_table.add_row("All resolved (conservative)", _fmt_float(brier_cons))
    brier_table.add_row("All resolved (both arms)",    _fmt_float(brier_all))
    brier_table.add_row(f"Rolling last {len(recent_50)}",
                        _fmt_float(brier_roll) + roll_status)
    console.print(brier_table)

    # -----------------------------------------------------------------------
    # 3. Beat-market rate
    # -----------------------------------------------------------------------
    bmr_cons = _beat_market_rate(resolved_cons)
    bmr_all  = _beat_market_rate(all_resolved)
    bmr_roll = _beat_market_rate(recent_50)

    bmr_status = ""
    if bmr_roll is not None:
        if bmr_roll < beat_halt:
            bmr_status = f" [bold red]HALT SIGNAL (<{beat_halt:.0%})[/bold red]"
        elif bmr_roll < beat_alert:
            bmr_status = f" [yellow]WARN (<{beat_alert:.0%})[/yellow]"
        else:
            bmr_status = " [green]OK[/green]"

    console.print("\n[bold]3. Beat-Market Rate[/bold]  (target > 55%)")
    bmr_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    bmr_table.add_column(style="cyan", no_wrap=True)
    bmr_table.add_column()
    bmr_table.add_row("All resolved (conservative)", _fmt_pct(bmr_cons))
    bmr_table.add_row("All resolved (both arms)",    _fmt_pct(bmr_all))
    bmr_table.add_row(f"Rolling last {len(recent_50)}",
                      _fmt_pct(bmr_roll) + bmr_status)
    console.print(bmr_table)

    # -----------------------------------------------------------------------
    # 4. Dual-arm comparison
    # -----------------------------------------------------------------------
    console.print("\n[bold]4. Dual-Arm Comparison[/bold]")

    # Trades unique to exploratory (didn't pass conservative filter)
    cons_tickers = {r["market_ticker"] for r in resolved_cons}
    exp_unique   = [r for r in resolved_exp if r["market_ticker"] not in cons_tickers]

    arm_table = Table(
        "Metric", "Conservative", "Exploratory", "Exp-only",
        box=box.ROUNDED,
    )
    arm_table.add_row("Resolved trades",
        str(len(resolved_cons)),
        str(len(resolved_exp)),
        str(len(exp_unique)),
    )
    arm_table.add_row("Total P&L",
        f"${_total_pnl(resolved_cons):+.2f}",
        f"${_total_pnl(resolved_exp):+.2f}",
        f"${_total_pnl(exp_unique):+.2f}",
    )
    arm_table.add_row("Win rate",
        _fmt_pct(_win_rate(resolved_cons)),
        _fmt_pct(_win_rate(resolved_exp)),
        _fmt_pct(_win_rate(exp_unique)),
    )
    arm_table.add_row("Brier score",
        _fmt_float(_brier(resolved_cons)),
        _fmt_float(_brier(resolved_exp)),
        _fmt_float(_brier(exp_unique)),
    )
    arm_table.add_row("Beat-market rate",
        _fmt_pct(_beat_market_rate(resolved_cons)),
        _fmt_pct(_beat_market_rate(resolved_exp)),
        _fmt_pct(_beat_market_rate(exp_unique)),
    )
    console.print(arm_table)

    if exp_unique:
        exp_roi = _total_pnl(exp_unique)
        if exp_roi > 0:
            console.print(
                f"  [green]Exploratory arm captured {len(exp_unique)} profitable "
                f"trades the conservative arm skipped (+${exp_roi:.2f}). "
                "Consider relaxing guardrails.[/green]"
            )
        else:
            console.print(
                f"  [yellow]Exploratory arm's {len(exp_unique)} unique trades lost "
                f"${abs(exp_roi):.2f}. Conservative guardrails are protecting you.[/yellow]"
            )

    # -----------------------------------------------------------------------
    # 5. Per-city breakdown
    # -----------------------------------------------------------------------
    console.print("\n[bold]5. Per-City Breakdown[/bold]")

    by_city: dict[str, list[dict]] = defaultdict(list)
    for r in resolved_cons:
        by_city[r.get("city", "Unknown")].append(r)

    city_table = Table(
        "City", "Resolved", "Win Rate", "P&L", "Brier", "Beat-Mkt",
        box=box.ROUNDED,
    )

    for city, rows in sorted(by_city.items()):
        if len(rows) < MIN_CITY_MARKETS:
            city_table.add_row(
                city, str(len(rows)), "—", "—", "—",
                f"[dim](n<{MIN_CITY_MARKETS})[/dim]",
            )
        else:
            city_table.add_row(
                city,
                str(len(rows)),
                _fmt_pct(_win_rate(rows)),
                f"${_total_pnl(rows):+.2f}",
                _fmt_float(_brier(rows)),
                _fmt_pct(_beat_market_rate(rows)),
            )

    console.print(city_table)

    # -----------------------------------------------------------------------
    # 6. Regime check
    # -----------------------------------------------------------------------
    console.print("\n[bold]6. Regime Check[/bold]")

    # Rolling 50-market Brier trend (last 4 windows)
    if len(all_resolved) >= ROLLING_WINDOW:
        windows = []
        for i in range(max(0, len(all_resolved) - 4 * ROLLING_WINDOW), len(all_resolved), ROLLING_WINDOW):
            window_rows = all_resolved[i:i + ROLLING_WINDOW]
            if len(window_rows) >= ROLLING_WINDOW // 2:
                windows.append((i, window_rows))

        regime_table = Table("Window", "N", "Brier", "Beat-Mkt", "Status", box=box.SIMPLE)
        for start_idx, wr in windows:
            b = _brier(wr)
            bmr = _beat_market_rate(wr)
            b_str = _fmt_float(b)
            bmr_str = _fmt_pct(bmr)
            if b is not None and b > brier_alert:
                status = "[red]BRIER ALERT[/red]"
            elif bmr is not None and bmr < beat_halt:
                status = "[red]BEAT-MKT HALT[/red]"
            elif bmr is not None and bmr < beat_alert:
                status = "[yellow]WARN[/yellow]"
            else:
                status = "[green]OK[/green]"
            label = f"#{start_idx + 1}-{start_idx + len(wr)}"
            regime_table.add_row(label, str(len(wr)), b_str, bmr_str, status)
        console.print(regime_table)
    else:
        console.print(
            f"  [dim]Insufficient data for rolling analysis "
            f"(need {ROLLING_WINDOW}, have {len(all_resolved)})[/dim]"
        )

    # -----------------------------------------------------------------------
    # Done
    # -----------------------------------------------------------------------
    console.print("\n[bold]Report complete.[/bold]")
    return 0


@click.command()
@click.option("--db-path",    default="./data/trades.db", show_default=True,
              help="Path to the trades SQLite database.")
@click.option("--config-dir", default="./config",         show_default=True)
def main(db_path: str, config_dir: str) -> None:
    """Generate calibration and performance report from trades.db."""
    rc = run_report(db_path, config_dir)
    sys.exit(rc)


if __name__ == "__main__":
    main()
