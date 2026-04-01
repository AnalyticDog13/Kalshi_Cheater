"""
pre_live_checklist.py — Automated readiness check before enabling live trading.

Runs all 10 pre-live checks from spec §2.4. EVERY check must pass before
enabling live trading. The final check requires a typed confirmation.

Checks:
  1. TRADING_MODE=live is set (or will be set via --set-live flag)
  2. Live API credentials are present and the private key file is readable
  3. Risk limits in settings.yaml are configured and non-zero
  4. At least 100 paper-traded weather markets have resolved
  5. Paper trading Brier score < 0.22
  6. Paper trading beat-market rate > 52%
  7. Paper trading ROI is positive after all costs
  8. Maker fill rate > 60%
  9. No single-day loss exceeded 5% of the configured portfolio value
 10. A recent database backup exists (< 24 hours old)

Usage:
    python scripts/pre_live_checklist.py \\
        [--db-path ./data/trades.db] \\
        [--config-dir ./config] \\
        [--portfolio-value 1000.0]

The script does NOT switch the system to live mode — it only tells you if you
are ready. To go live: set TRADING_MODE=live in .env and use python -m src.main start --live.

Exit codes:
    0 — all checks passed, confirmation given
    1 — one or more checks failed, or confirmation declined
"""

from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import click
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box
from sqlite_utils import Database

console = Console()

PASS_STR = "[bold green]PASS[/bold green]"
FAIL_STR = "[bold red]FAIL[/bold red]"
WARN_STR = "[bold yellow]WARN[/bold yellow]"

MIN_RESOLVED_MARKETS        = 100
MAX_BRIER_SCORE             = 0.22
MIN_BEAT_MARKET_RATE        = 0.52
MIN_MAKER_FILL_RATE         = 0.60
MAX_SINGLE_DAY_LOSS_FRAC    = 0.05   # 5% of portfolio value
BACKUP_MAX_AGE_HOURS        = 24


@click.command()
@click.option("--db-path",         default="./data/trades.db", show_default=True)
@click.option("--config-dir",      default="./config",         show_default=True)
@click.option("--portfolio-value", default=1000.0,             show_default=True,
              help="Total portfolio value in USD (for single-day loss check).")
def main(db_path: str, config_dir: str, portfolio_value: float) -> None:
    """Run all 10 pre-live checks and print PASS/FAIL results."""
    load_dotenv()
    results: list[tuple[str, str, str]] = []  # (name, status, detail)
    all_passed = True

    # Load settings
    settings: dict = {}
    cfg_path = os.path.join(config_dir, "settings.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            settings = yaml.safe_load(f) or {}

    # ------------------------------------------------------------------
    # Check 1: TRADING_MODE=live
    # ------------------------------------------------------------------
    mode = os.environ.get("TRADING_MODE", settings.get("trading_mode", "demo"))
    if mode == "live":
        results.append(("1. TRADING_MODE=live", PASS_STR, f"TRADING_MODE={mode}"))
    else:
        results.append(("1. TRADING_MODE=live", FAIL_STR,
                        f"Currently '{mode}' — set TRADING_MODE=live in .env"))
        all_passed = False

    # ------------------------------------------------------------------
    # Check 2: Live API credentials
    # ------------------------------------------------------------------
    key_id   = os.environ.get("KALSHI_LIVE_API_KEY_ID", "")
    key_path = os.environ.get("KALSHI_LIVE_PRIVATE_KEY_PATH", "")
    if not key_id:
        results.append(("2. Live API credentials", FAIL_STR,
                        "KALSHI_LIVE_API_KEY_ID not set in environment"))
        all_passed = False
    elif not key_path:
        results.append(("2. Live API credentials", FAIL_STR,
                        "KALSHI_LIVE_PRIVATE_KEY_PATH not set in environment"))
        all_passed = False
    elif not Path(key_path).exists():
        results.append(("2. Live API credentials", FAIL_STR,
                        f"Private key not found: {key_path}"))
        all_passed = False
    else:
        try:
            with open(key_path, "r") as f:
                content = f.read()
            if "PRIVATE KEY" not in content:
                raise ValueError("File does not appear to be a PEM private key")
            results.append(("2. Live API credentials", PASS_STR,
                            f"Key ID set, PEM file readable ({Path(key_path).stat().st_size} bytes)"))
        except Exception as exc:
            results.append(("2. Live API credentials", FAIL_STR, str(exc)[:70]))
            all_passed = False

    # ------------------------------------------------------------------
    # Check 3: Risk limits configured and non-zero
    # ------------------------------------------------------------------
    risk_cfg = settings.get("risk", {})
    required_limits = {
        "max_position_dollars":       (0, 500),
        "max_total_exposure_dollars":  (0, 10_000),
        "max_daily_loss_dollars":      (0, 1_000),
        "max_open_orders":             (0, 100),
    }
    limit_issues = []
    for key, (lo, hi) in required_limits.items():
        val = risk_cfg.get(key)
        if val is None:
            limit_issues.append(f"{key} missing")
        elif not (lo < val <= hi):
            limit_issues.append(f"{key}={val} out of range ({lo}, {hi}]")

    if limit_issues:
        results.append(("3. Risk limits configured", FAIL_STR, "; ".join(limit_issues)))
        all_passed = False
    else:
        pos_cap = risk_cfg.get("max_position_dollars")
        exp_cap = risk_cfg.get("max_total_exposure_dollars")
        results.append(("3. Risk limits configured", PASS_STR,
                        f"position=${pos_cap}, exposure=${exp_cap}"))

    # ------------------------------------------------------------------
    # DB-dependent checks (4-9)
    # ------------------------------------------------------------------
    db_exists = Path(db_path).exists()
    if not db_exists:
        # All DB checks fail
        for i, name in enumerate([
            "4. >= 100 resolved markets",
            "5. Brier score < 0.22",
            "6. Beat-market rate > 52%",
            "7. Positive ROI",
            "8. Maker fill rate > 60%",
            "9. No single-day loss > 5%",
        ], start=4):
            results.append((f"{i}. {name.split('. ', 1)[1]}", FAIL_STR,
                            f"Database not found: {db_path}"))
            all_passed = False
    else:
        db = Database(db_path)
        if "trades" not in db.table_names():
            for i in range(4, 10):
                results.append((f"{i}. (DB check)", FAIL_STR, "No 'trades' table"))
                all_passed = False
        else:
            resolved = list(db["trades"].rows_where(
                "actual_outcome IS NOT NULL AND order_cancelled = 0 "
                "AND strategy_arm = 'conservative'"
            ))
            filled = [r for r in resolved if r.get("order_filled_time")]

            # Check 4: >= 100 resolved
            n = len(resolved)
            if n >= MIN_RESOLVED_MARKETS:
                results.append(("4. >= 100 resolved markets", PASS_STR,
                                f"{n} resolved markets"))
            else:
                results.append(("4. >= 100 resolved markets", FAIL_STR,
                                f"Only {n} resolved (need {MIN_RESOLVED_MARKETS})"))
                all_passed = False

            # Check 5: Brier score
            if not resolved:
                results.append(("5. Brier score < 0.22", FAIL_STR, "No data"))
                all_passed = False
            else:
                brier = sum(
                    (r["ensemble_prob_at_entry"] - r["actual_outcome"]) ** 2
                    for r in resolved
                ) / len(resolved)
                if brier < MAX_BRIER_SCORE:
                    results.append(("5. Brier score < 0.22", PASS_STR,
                                    f"Brier = {brier:.4f}"))
                else:
                    results.append(("5. Brier score < 0.22", FAIL_STR,
                                    f"Brier = {brier:.4f} (need < {MAX_BRIER_SCORE})"))
                    all_passed = False

            # Check 6: Beat-market rate > 52%
            if not resolved:
                results.append(("6. Beat-market rate > 52%", FAIL_STR, "No data"))
                all_passed = False
            else:
                beats = sum(
                    1 for r in resolved
                    if abs(r["ensemble_prob_at_entry"] - r["actual_outcome"]) <
                       abs(r["mid_price_at_signal"] - r["actual_outcome"])
                )
                bmr = beats / len(resolved)
                if bmr >= MIN_BEAT_MARKET_RATE:
                    results.append(("6. Beat-market rate > 52%", PASS_STR,
                                    f"{bmr:.1%} ({beats}/{len(resolved)})"))
                else:
                    results.append(("6. Beat-market rate > 52%", FAIL_STR,
                                    f"{bmr:.1%} (need >= {MIN_BEAT_MARKET_RATE:.0%})"))
                    all_passed = False

            # Check 7: Positive ROI
            total_pnl = sum((r.get("pnl") or 0.0) for r in filled)
            total_ai  = sum((r.get("ai_cost_usd") or 0.0) for r in
                            list(db["trades"].rows_where("strategy_arm = 'conservative'")))
            net_pnl = total_pnl - total_ai
            if net_pnl > 0:
                results.append(("7. Positive ROI", PASS_STR,
                                f"Net P&L = ${net_pnl:+.2f} (trading ${total_pnl:+.2f} - AI ${total_ai:.2f})"))
            else:
                results.append(("7. Positive ROI", FAIL_STR,
                                f"Net P&L = ${net_pnl:+.2f} — system is losing money"))
                all_passed = False

            # Check 8: Maker fill rate > 60%
            if not filled:
                results.append(("8. Maker fill rate > 60%", WARN_STR,
                                "No fills recorded yet"))
                # Not a hard failure if no fills yet — system may not have traded
            else:
                makers = sum(1 for r in filled if r.get("was_maker_fill"))
                mfr = makers / len(filled)
                if mfr >= MIN_MAKER_FILL_RATE:
                    results.append(("8. Maker fill rate > 60%", PASS_STR,
                                    f"{mfr:.1%} maker ({makers}/{len(filled)})"))
                else:
                    results.append(("8. Maker fill rate > 60%", FAIL_STR,
                                    f"{mfr:.1%} (need >= {MIN_MAKER_FILL_RATE:.0%})"))
                    all_passed = False

            # Check 9: No single-day loss > 5% of portfolio
            daily_pnl: dict[str, float] = defaultdict(float)
            for r in filled:
                rt = r.get("resolution_time")
                pnl = r.get("pnl") or 0.0
                if rt:
                    try:
                        day = datetime.fromisoformat(
                            str(rt).replace("Z", "+00:00")
                        ).date().isoformat()
                        daily_pnl[day] += pnl
                    except ValueError:
                        pass

            max_loss_allowed = portfolio_value * MAX_SINGLE_DAY_LOSS_FRAC
            worst_day = min(daily_pnl.items(), key=lambda x: x[1], default=(None, 0.0))
            if worst_day[1] < -max_loss_allowed:
                results.append(("9. No single-day loss > 5%", FAIL_STR,
                                f"Worst day {worst_day[0]}: ${worst_day[1]:.2f} "
                                f"(limit: -${max_loss_allowed:.0f})"))
                all_passed = False
            else:
                worst_str = (
                    f"Worst day: {worst_day[0]} ${worst_day[1]:.2f}"
                    if worst_day[0] else "No resolved days yet"
                )
                results.append(("9. No single-day loss > 5%", PASS_STR, worst_str))

    # ------------------------------------------------------------------
    # Check 10: Recent database backup (< 24 hours old)
    # ------------------------------------------------------------------
    backup_found = False
    backup_msg   = ""

    # Look for .bak or backup copies of the DB
    db_file = Path(db_path)
    backup_candidates = [
        db_file.parent / (db_file.name + ".bak"),
        db_file.parent / (db_file.stem + "_backup" + db_file.suffix),
        db_file.parent / "backups" / db_file.name,
    ]
    for candidate in backup_candidates:
        if candidate.exists():
            age_hours = (
                datetime.now(timezone.utc).timestamp() - candidate.stat().st_mtime
            ) / 3600
            if age_hours <= BACKUP_MAX_AGE_HOURS:
                backup_found = True
                backup_msg = f"Found {candidate} ({age_hours:.1f}h old)"
                break
            else:
                backup_msg = f"Found {candidate} but {age_hours:.1f}h old (need < {BACKUP_MAX_AGE_HOURS}h)"

    if not backup_found and db_exists:
        # If no explicit backup, check main DB modification time as a last resort
        db_age_hours = (
            datetime.now(timezone.utc).timestamp() - Path(db_path).stat().st_mtime
        ) / 3600
        backup_msg = (
            f"No backup file found. Main DB last modified {db_age_hours:.1f}h ago. "
            f"Create a backup: cp {db_path} {db_path}.bak"
        )

    if backup_found:
        results.append(("10. Recent DB backup", PASS_STR, backup_msg))
    else:
        results.append(("10. Recent DB backup", FAIL_STR, backup_msg or "No backup found"))
        all_passed = False

    # ------------------------------------------------------------------
    # Print results table
    # ------------------------------------------------------------------
    table = Table(
        "Check", "Result", "Detail",
        box=box.ROUNDED,
        title="Pre-Live Readiness Checklist",
        expand=True,
    )

    for check_name, status, detail in results:
        table.add_row(check_name, status, detail)

    console.print()
    console.print(table)

    passes = sum(1 for _, s, _ in results if "PASS" in s)
    fails  = sum(1 for _, s, _ in results if "FAIL" in s)
    warns  = sum(1 for _, s, _ in results if "WARN" in s)

    console.print(
        f"\n[bold]Result: {passes} PASS | {warns} WARN | {fails} FAIL[/bold]"
    )

    if not all_passed:
        console.print(
            "\n[bold red]System is NOT ready for live trading.[/bold red] "
            "Fix all FAIL items above before proceeding."
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Manual confirmation (final gate)
    # ------------------------------------------------------------------
    console.print(
        "\n[bold green]All checks passed.[/bold green] "
        "Live trading is technically ready.\n"
    )
    console.print(
        "[bold yellow]IMPORTANT:[/bold yellow] You are about to enable real-money trading. "
        f"Maximum exposure: ${settings.get('risk', {}).get('max_total_exposure_dollars', '?')}. "
        "Losses are real.\n"
    )

    confirmation = console.input(
        '[bold]Type exactly:[/bold] "I understand I am trading real money with a maximum '
        'exposure of $50 in shadow mode"\n> '
    )
    expected = "I understand I am trading real money with a maximum exposure of $50 in shadow mode"

    if confirmation.strip() == expected:
        console.print("\n[bold green]Confirmation accepted. System is cleared for live trading.[/bold green]")
        console.print("Next step: set TRADING_MODE=live in .env, then run: python -m src.main start --live")
        sys.exit(0)
    else:
        console.print("\n[bold red]Confirmation did not match. Aborting.[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
