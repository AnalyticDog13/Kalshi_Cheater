"""
Report Generator.

Generates daily and weekly trading reports from the trades database.
All numbers come directly from SQLite — zero AI, zero hallucinations.

Called automatically by the daemon at UTC midnight.
Can also be run manually:

    python scripts/generate_reports.py --daily 2026-04-02
    python scripts/generate_reports.py --weekly 2026-04-06   # any date in the week
    python scripts/generate_reports.py --feed-to-ai          # regenerate from latest weekly

Output structure:
    reports/
        daily/          YYYY-MM-DD.md        (one per day)
        weekly/         YYYY-WXX.md          (one per ISO week)
        feed_to_ai/     YYYY-WXX_prompt.md   (paste into Claude/Opus for analysis)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Allow running directly from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

# ---------------------------------------------------------------------------
# Paths (relative to project root)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
DAILY_DIR = REPORTS_DIR / "daily"
WEEKLY_DIR = REPORTS_DIR / "weekly"
FEED_TO_AI_DIR = REPORTS_DIR / "feed_to_ai"
DEFAULT_DB_PATH = str(PROJECT_ROOT / "data" / "trades.db")
DEFAULT_SETTINGS_PATH = str(PROJECT_ROOT / "config" / "settings.yaml")


def _ensure_dirs() -> None:
    for d in (DAILY_DIR, WEEKLY_DIR, FEED_TO_AI_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: str):
    """Open the trades database. Returns None if the file doesn't exist."""
    from sqlite_utils import Database
    path = Path(db_path)
    if not path.exists():
        return None
    return Database(db_path)


def _rows(db, where: str = "", params: list | None = None) -> list[dict]:
    """
    Query the trades table. Returns a list of plain dicts.
    Returns [] if the table doesn't exist yet (first run before any trades).
    """
    if db is None:
        return []
    try:
        rows = list(db["trades"].rows_where(where, params or []) if where else db["trades"].rows)
        return [dict(r) for r in rows]
    except Exception:
        return []


def _date_of(iso_str: str | None) -> str | None:
    """Extract the DATE portion from an ISO datetime string."""
    if not iso_str:
        return None
    return iso_str[:10]


# ---------------------------------------------------------------------------
# Metric computations (pure math on lists of trade dicts)
# ---------------------------------------------------------------------------

def _brier_score(trades: list[dict], n_recent: int | None = None) -> float | None:
    """
    Brier score: mean((ensemble_prob - actual_outcome)^2).
    Only includes filled, resolved, non-cancelled conservative trades.
    """
    eligible = [
        t for t in trades
        if t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
        and t.get("order_filled_time") is not None
        and t.get("strategy_arm") == "conservative"
        and t.get("ensemble_prob_at_entry") is not None
    ]
    # Sort by resolution time descending so n_recent gets the most recent
    eligible.sort(key=lambda t: t.get("resolution_time") or "", reverse=True)
    if n_recent:
        eligible = eligible[:n_recent]
    if not eligible:
        return None
    return sum(
        (t["ensemble_prob_at_entry"] - t["actual_outcome"]) ** 2
        for t in eligible
    ) / len(eligible)


def _beat_market_rate(trades: list[dict], n_recent: int | None = None) -> tuple[float | None, int]:
    """
    Fraction of resolved trades where our ensemble estimate was closer to the
    actual outcome than the mid-price at signal time.
    Returns (rate, count).
    """
    eligible = [
        t for t in trades
        if t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
        and t.get("order_filled_time") is not None
        and t.get("strategy_arm") == "conservative"
        and t.get("ensemble_prob_at_entry") is not None
        and t.get("mid_price_at_signal") is not None
    ]
    eligible.sort(key=lambda t: t.get("resolution_time") or "", reverse=True)
    if n_recent:
        eligible = eligible[:n_recent]
    if not eligible:
        return None, 0
    beats = sum(
        1 for t in eligible
        if abs(t["ensemble_prob_at_entry"] - t["actual_outcome"])
        < abs(t["mid_price_at_signal"] - t["actual_outcome"])
    )
    return beats / len(eligible), len(eligible)


def _fill_rate(trades: list[dict]) -> tuple[float | None, int, int]:
    """
    Fraction of placed conservative orders that filled (vs expired unfilled).
    Returns (rate, filled_count, placed_count).
    """
    placed = [
        t for t in trades
        if t.get("strategy_arm") == "conservative"
    ]
    if not placed:
        return None, 0, 0
    filled = [t for t in placed if t.get("order_filled_time") is not None]
    return len(filled) / len(placed), len(filled), len(placed)


def _maker_rate(trades: list[dict]) -> tuple[float | None, int, int]:
    """
    Fraction of filled conservative trades that were maker fills (zero fees).
    Returns (rate, maker_count, filled_count).
    """
    filled = [
        t for t in trades
        if t.get("strategy_arm") == "conservative"
        and t.get("order_filled_time") is not None
    ]
    if not filled:
        return None, 0, 0
    makers = [t for t in filled if t.get("was_maker_fill", 0)]
    return len(makers) / len(filled), len(makers), len(filled)


def _total_pnl(trades: list[dict], arm: str = "conservative") -> float:
    return sum(
        t.get("pnl") or 0.0
        for t in trades
        if t.get("strategy_arm") == arm and t.get("pnl") is not None
    )


def _total_fees(trades: list[dict]) -> float:
    return sum(
        t.get("fees_paid") or 0.0
        for t in trades
        if t.get("strategy_arm") == "conservative"
    )


def _per_city_stats(trades: list[dict]) -> list[dict]:
    """
    Per-city breakdown for resolved conservative trades.
    Returns list of dicts sorted by city name.
    """
    cities: dict[str, list] = {}
    for t in trades:
        if t.get("strategy_arm") != "conservative":
            continue
        if t.get("actual_outcome") is None:
            continue
        if t.get("order_cancelled") or t.get("order_filled_time") is None:
            continue
        city = t.get("city") or "Unknown"
        cities.setdefault(city, []).append(t)

    results = []
    for city, city_trades in sorted(cities.items()):
        wins = sum(1 for t in city_trades if (t.get("pnl") or 0) > 0)
        resolved = len(city_trades)
        pnl = sum(t.get("pnl") or 0 for t in city_trades)
        brier = _brier_score(city_trades)
        brier_str = f"{brier:.3f}" if brier is not None else "N/A"
        results.append({
            "city": city,
            "resolved": resolved,
            "wins": wins,
            "win_rate": wins / resolved if resolved else 0,
            "pnl": pnl,
            "brier": brier_str,
        })
    return results


def _status_and_alerts(
    daily_pnl: float,
    all_time_trades: list[dict],
    brier_rolling: float | None,
    beat_rate_rolling: float | None,
    settings: dict,
) -> tuple[str, list[str]]:
    """
    Determine NORMAL / ALERT status and generate specific alert messages.
    """
    alerts = []
    risk = settings.get("risk", {})
    daily_loss_limit = risk.get("max_daily_loss_dollars", 3.0)
    regime = settings.get("regime", {})
    brier_halt = regime.get("brier_alert_threshold", 0.25)
    beat_halt = regime.get("beat_market_halt_threshold", 0.48)

    if daily_pnl <= -(daily_loss_limit * 0.75):
        alerts.append(
            f"Daily P&L ${daily_pnl:.2f} is >= 75% of daily loss limit "
            f"(-${daily_loss_limit:.2f}). Trading may halt today."
        )

    if brier_rolling is not None and brier_rolling > brier_halt:
        alerts.append(
            f"Rolling Brier score {brier_rolling:.3f} exceeds alert threshold "
            f"({brier_halt}). Ensemble accuracy degrading — investigate."
        )

    if beat_rate_rolling is not None and beat_rate_rolling < beat_halt:
        alerts.append(
            f"Rolling beat-market rate {beat_rate_rolling:.1%} is below halt "
            f"threshold ({beat_halt:.0%}). System may have lost edge — investigate."
        )

    # No fills in 3+ consecutive days
    filled_dates = sorted(set(
        _date_of(t.get("order_filled_time"))
        for t in all_time_trades
        if t.get("order_filled_time") and t.get("strategy_arm") == "conservative"
    ), reverse=True)
    if filled_dates:
        last_fill_date = date.fromisoformat(filled_dates[0])
        days_since_fill = (date.today() - last_fill_date).days
        if days_since_fill >= 3:
            alerts.append(
                f"No fills in {days_since_fill} days (last fill: {filled_dates[0]}). "
                "Check spread/volume conditions or order pricing logic."
            )

    status = "ALERT" if alerts else "NORMAL"
    return status, alerts


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

def generate_daily_report(
    db_path: str = DEFAULT_DB_PATH,
    report_date: date | None = None,
    settings: dict | None = None,
) -> Path:
    """
    Generate a daily report for `report_date` (defaults to yesterday UTC).
    Writes to reports/daily/YYYY-MM-DD.md and returns the path.
    """
    _ensure_dirs()

    if report_date is None:
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    if settings is None:
        settings = _load_settings()

    date_iso = report_date.isoformat()
    db = _open_db(db_path)
    all_trades = _rows(db)

    # --- Trades active on report_date ---
    placed_today = [
        t for t in all_trades
        if _date_of(t.get("order_placed_time")) == date_iso
        and t.get("strategy_arm") == "conservative"
    ]
    filled_today = [
        t for t in all_trades
        if _date_of(t.get("order_filled_time")) == date_iso
        and t.get("strategy_arm") == "conservative"
        and not t.get("order_cancelled", 0)
    ]
    resolved_today = [
        t for t in all_trades
        if _date_of(t.get("resolution_time")) == date_iso
        and t.get("strategy_arm") == "conservative"
        and t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
    ]
    expired_today = [
        t for t in placed_today
        if t.get("order_cancelled", 0)
    ]
    open_positions = [
        t for t in all_trades
        if t.get("order_filled_time") is not None
        and t.get("actual_outcome") is None
        and not t.get("order_cancelled", 0)
        and t.get("strategy_arm") == "conservative"
    ]
    exp_placed_today = [
        t for t in all_trades
        if _date_of(t.get("order_placed_time")) == date_iso
        and t.get("strategy_arm") == "exploratory"
    ]

    # --- P&L ---
    daily_pnl = sum(t.get("pnl") or 0 for t in resolved_today)
    all_time_pnl = _total_pnl(all_trades)
    all_time_fees = _total_fees(all_trades)
    open_exposure = sum(
        (t.get("fill_price") or 0) * (t.get("quantity") or 0)
        for t in open_positions
    )

    # --- Rolling performance ---
    brier_rolling = _brier_score(all_trades, n_recent=50)
    brier_all = _brier_score(all_trades)
    beat_rate_rolling, beat_n_rolling = _beat_market_rate(all_trades, n_recent=50)

    # --- Status ---
    status, alerts = _status_and_alerts(
        daily_pnl, all_trades, brier_rolling, beat_rate_rolling, settings
    )

    # --- Build report ---
    lines = [
        f"# Daily Trading Report — {date_iso}",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        f"## Status: {status}",
    ]
    if alerts:
        lines.append("")
        for alert in alerts:
            lines.append(f"- **ALERT:** {alert}")
    lines += [
        "",
        "---",
        "",
        "## Activity",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Orders placed (conservative) | {len(placed_today)} |",
        f"| Orders filled | {len(filled_today)} |",
        f"| Orders expired unfilled | {len(expired_today)} |",
        f"| Markets resolved | {len(resolved_today)} |",
        f"| Exploratory arm (paper-only, would-have-placed) | {len(exp_placed_today)} |",
        "",
        "---",
        "",
        "## P&L",
        "",
        f"| | Amount |",
        f"|--|--------|",
        f"| Today's resolved P&L | ${daily_pnl:+.2f} |",
        f"| All-time realized P&L | ${all_time_pnl:+.2f} |",
        f"| All-time fees paid | ${all_time_fees:.2f} |",
        f"| All-time net P&L (after fees) | ${all_time_pnl - all_time_fees:+.2f} |",
        f"| Open positions (unrealized exposure) | {len(open_positions)} positions, ${open_exposure:.2f} at risk |",
        "",
    ]

    # Resolved trades table
    if resolved_today:
        lines += [
            "### Resolved Today",
            "",
            "| Market | City | Side | Ensemble P | Actual Outcome | Actual Temp | Threshold | Members | Spread | Bias | P&L |",
            "|--------|------|------|-----------|----------------|-------------|-----------|---------|--------|------|-----|",
        ]
        for t in sorted(resolved_today, key=lambda x: x.get("resolution_time") or ""):
            temp_f = t.get("actual_max_temp_f")
            temp_str = f"{temp_f:.1f}F" if temp_f is not None else "N/A"
            threshold = t.get("market_ticker", "").split("-T")[-1] if "-T" in t.get("market_ticker", "") else "N/A"
            ens_p = t.get("ensemble_prob_at_entry")
            ens_p_str = f"{ens_p:.1%}" if ens_p is not None else "N/A"
            lines.append(
                f"| {t.get('market_ticker','')} | {t.get('city','')} | "
                f"{t.get('side','')} | {ens_p_str} | "
                f"{t.get('actual_outcome', '?')} | {temp_str} | {threshold}F | "
                f"{t.get('ensemble_members_at_entry') or 'N/A'} | "
                f"{(t.get('ensemble_spread_at_entry') or 0):.1f}F | "
                f"{(t.get('bias_correction_applied') or 0):+.1f}F | "
                f"${(t.get('pnl') or 0):+.2f} |"
            )
        lines.append("")

    # Open positions table
    if open_positions:
        lines += [
            "### Open Positions",
            "",
            "| Market | City | Side | Entry Price | Ensemble P | Placed |",
            "|--------|------|------|------------|-----------|--------|",
        ]
        for t in sorted(open_positions, key=lambda x: x.get("order_placed_time") or ""):
            lines.append(
                f"| {t.get('market_ticker','')} | {t.get('city','')} | "
                f"{t.get('side','')} | ${t.get('entry_price', 0):.2f} | "
                f"{t.get('ensemble_prob_at_entry', 0):.1%} | "
                f"{_date_of(t.get('order_placed_time')) or 'N/A'} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "## Performance Snapshot (All-Time)",
        "",
        f"| Metric | Value | Target |",
        f"|--------|-------|--------|",
        f"| Brier score (rolling 50) | {f'{brier_rolling:.3f}' if brier_rolling is not None else 'N/A (< 50 trades)'} | < 0.18 |",
        f"| Brier score (all-time) | {f'{brier_all:.3f}' if brier_all is not None else 'N/A'} | < 0.18 |",
        f"| Beat-market rate (rolling 50) | {f'{beat_rate_rolling:.1%} ({beat_n_rolling} trades)' if beat_rate_rolling is not None else 'N/A'} | > 55% |",
        "",
    ]

    # Exploratory arm note
    if exp_placed_today:
        exp_pnl_today = sum(
            t.get("pnl") or 0 for t in exp_placed_today
            if _date_of(t.get("resolution_time")) == date_iso
        )
        lines += [
            "---",
            "",
            "## Exploratory Arm (Paper-Only — Never Executes Real Orders)",
            "",
            f"Would have placed {len(exp_placed_today)} additional trades today.",
            f"Hypothetical P&L on trades that resolved today: ${exp_pnl_today:+.2f}",
            "",
        ]

    output_path = DAILY_DIR / f"{date_iso}.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------

def generate_weekly_report(
    db_path: str = DEFAULT_DB_PATH,
    week_end: date | None = None,
    settings: dict | None = None,
) -> Path:
    """
    Generate a weekly report for the 7-day period ending on `week_end`
    (defaults to yesterday, i.e. the just-completed Sunday).
    Writes to reports/weekly/YYYY-WXX.md and returns the path.
    """
    _ensure_dirs()

    if week_end is None:
        week_end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    week_start = week_end - timedelta(days=6)
    if settings is None:
        settings = _load_settings()

    iso_year, iso_week, _ = week_end.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"

    db = _open_db(db_path)
    all_trades = _rows(db)

    # Filter to this week
    def in_week(iso_str: str | None) -> bool:
        d = _date_of(iso_str)
        if not d:
            return False
        return week_start.isoformat() <= d <= week_end.isoformat()

    week_placed = [
        t for t in all_trades
        if in_week(t.get("order_placed_time")) and t.get("strategy_arm") == "conservative"
    ]
    week_filled = [
        t for t in all_trades
        if in_week(t.get("order_filled_time"))
        and t.get("strategy_arm") == "conservative"
        and not t.get("order_cancelled", 0)
    ]
    week_resolved = [
        t for t in all_trades
        if in_week(t.get("resolution_time"))
        and t.get("strategy_arm") == "conservative"
        and t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
        and t.get("order_filled_time") is not None
    ]
    week_expired = [
        t for t in week_placed if t.get("order_cancelled", 0)
    ]
    week_exp = [
        t for t in all_trades
        if in_week(t.get("order_placed_time")) and t.get("strategy_arm") == "exploratory"
    ]
    week_exp_resolved = [
        t for t in all_trades
        if in_week(t.get("resolution_time"))
        and t.get("strategy_arm") == "exploratory"
        and t.get("actual_outcome") is not None
    ]

    # P&L
    week_pnl = sum(t.get("pnl") or 0 for t in week_resolved)
    all_time_pnl = _total_pnl(all_trades)
    all_time_fees = _total_fees(all_trades)

    # Performance metrics (all-time for rolling context)
    brier_rolling50 = _brier_score(all_trades, n_recent=50)
    brier_all = _brier_score(all_trades)
    beat_rate_rolling50, beat_n50 = _beat_market_rate(all_trades, n_recent=50)
    beat_rate_all, beat_n_all = _beat_market_rate(all_trades)

    # Fill / maker quality
    fill_rate, filled_n, placed_n = _fill_rate(all_trades)
    maker_rate, maker_n, filled_total = _maker_rate(all_trades)
    avg_slippage = (
        sum(t.get("realized_slippage") or 0 for t in all_trades if t.get("order_filled_time"))
        / max(1, len([t for t in all_trades if t.get("order_filled_time")]))
    )

    # Per-city breakdown (all-time)
    city_stats = _per_city_stats(all_trades)

    # Dual-arm comparison (all-time)
    cons_resolved = [
        t for t in all_trades
        if t.get("strategy_arm") == "conservative"
        and t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
        and t.get("order_filled_time") is not None
    ]
    exp_resolved = [
        t for t in all_trades
        if t.get("strategy_arm") == "exploratory"
        and t.get("actual_outcome") is not None
    ]
    cons_pnl = _total_pnl(all_trades, arm="conservative")
    exp_pnl = _total_pnl(all_trades, arm="exploratory")
    cons_wins = sum(1 for t in cons_resolved if (t.get("pnl") or 0) > 0)
    exp_wins = sum(1 for t in exp_resolved if (t.get("pnl") or 0) > 0)

    # Day-by-day P&L this week
    daily_pnl_map: dict[str, float] = {}
    for t in week_resolved:
        d = _date_of(t.get("resolution_time")) or "unknown"
        daily_pnl_map[d] = round(daily_pnl_map.get(d, 0.0) + (t.get("pnl") or 0), 4)

    # Status
    brier_latest = brier_rolling50
    beat_latest = beat_rate_rolling50
    status, alerts = _status_and_alerts(
        week_pnl, all_trades, brier_latest, beat_latest, settings
    )

    # --- Build report ---
    lines = [
        f"# Weekly Trading Report — {week_label}",
        f"Period: {week_start.isoformat()} to {week_end.isoformat()}",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        f"## Status: {status}",
    ]
    if alerts:
        lines.append("")
        for alert in alerts:
            lines.append(f"- **ALERT:** {alert}")

    lines += [
        "",
        "---",
        "",
        "## Weekly Summary",
        "",
        f"| Metric | This Week | All-Time |",
        f"|--------|-----------|---------|",
        f"| Orders placed | {len(week_placed)} | {len([t for t in all_trades if t.get('strategy_arm')=='conservative'])} |",
        f"| Orders filled | {len(week_filled)} | {filled_total} |",
        f"| Orders expired unfilled | {len(week_expired)} | {len([t for t in all_trades if t.get('order_cancelled') and t.get('strategy_arm')=='conservative'])} |",
        f"| Markets resolved | {len(week_resolved)} | {len(cons_resolved)} |",
        f"| Realized P&L | ${week_pnl:+.2f} | ${all_time_pnl:+.2f} |",
        f"| Fees paid | ${_total_fees([t for t in all_trades if in_week(t.get('order_placed_time'))]):.2f} | ${all_time_fees:.2f} |",
        f"| Net P&L (after fees) | — | ${all_time_pnl - all_time_fees:+.2f} |",
        "",
        "---",
        "",
        "## Day-by-Day P&L (This Week)",
        "",
        "| Date | Resolved P&L |",
        "|------|-------------|",
    ]
    for day_offset in range(7):
        d = (week_start + timedelta(days=day_offset)).isoformat()
        pnl_val = daily_pnl_map.get(d, 0.0)
        resolved_count = sum(1 for t in week_resolved if _date_of(t.get("resolution_time")) == d)
        lines.append(f"| {d} | ${pnl_val:+.2f} ({resolved_count} resolved) |")

    lines += [
        "",
        "---",
        "",
        "## Ensemble & Prediction Performance (All-Time)",
        "",
        "| Metric | Value | Target | Interpretation |",
        "|--------|-------|--------|---------------|",
        f"| Brier score (rolling 50) | {f'{brier_rolling50:.3f}' if brier_rolling50 is not None else 'N/A'} | < 0.18 | Lower = better calibrated |",
        f"| Brier score (all-time) | {f'{brier_all:.3f}' if brier_all is not None else 'N/A'} | < 0.18 | |",
        f"| Beat-market rate (rolling 50) | {f'{beat_rate_rolling50:.1%} over {beat_n50} trades' if beat_rate_rolling50 is not None else 'N/A'} | > 55% | Are we better than the market? |",
        f"| Beat-market rate (all-time) | {f'{beat_rate_all:.1%} over {beat_n_all} trades' if beat_rate_all is not None else 'N/A'} | > 55% | |",
        "",
        "---",
        "",
        "## Fill Quality (All-Time)",
        "",
        "| Metric | Value | Target | Interpretation |",
        "|--------|-------|--------|---------------|",
        f"| Fill rate | {f'{fill_rate:.1%} ({filled_n}/{placed_n})' if fill_rate is not None else 'N/A'} | > 50% | Orders that actually fill |",
        f"| Maker fill rate | {f'{maker_rate:.1%} ({maker_n}/{filled_total})' if maker_rate is not None else 'N/A'} | > 70% | Zero-fee fills |",
        f"| Average slippage | ${avg_slippage:+.4f} | < $0.01 | Fill price vs mid at signal |",
        "",
        "---",
        "",
        "## Per-City Performance (All-Time Resolved)",
        "",
        "| City | Resolved | Wins | Win Rate | P&L | Brier |",
        "|------|----------|------|----------|-----|-------|",
    ]
    if city_stats:
        for cs in city_stats:
            lines.append(
                f"| {cs['city']} | {cs['resolved']} | {cs['wins']} | "
                f"{cs['win_rate']:.1%} | ${cs['pnl']:+.2f} | {cs['brier']} |"
            )
    else:
        lines.append("| No resolved trades yet | — | — | — | — | — |")

    # Calibration detail table — all resolved conservative trades, most recent first
    all_resolved_cons = [
        t for t in all_trades
        if t.get("strategy_arm") == "conservative"
        and t.get("actual_outcome") is not None
        and not t.get("order_cancelled", 0)
        and t.get("order_filled_time") is not None
    ]
    all_resolved_cons.sort(key=lambda t: t.get("resolution_time") or "", reverse=True)

    lines += [
        "",
        "---",
        "",
        "## Calibration Detail — All Resolved Trades (Most Recent First)",
        "",
        "_(For AI bias analysis: ensemble probability vs actual temperature vs outcome)_",
        "",
        "| Date | Market | City | Side | Ens P | Actual Outcome | Actual Temp | Threshold | Members | Spread | Bias | P&L |",
        "|------|--------|------|------|-------|----------------|-------------|-----------|---------|--------|------|-----|",
    ]
    if all_resolved_cons:
        for t in all_resolved_cons:
            temp_f = t.get("actual_max_temp_f")
            temp_str = f"{temp_f:.1f}F" if temp_f is not None else "N/A"
            ticker = t.get("market_ticker", "")
            threshold = ticker.split("-T")[-1] + "F" if "-T" in ticker else "N/A"
            res_date = _date_of(t.get("resolution_time")) or "N/A"
            lines.append(
                f"| {res_date} | {ticker} | {t.get('city','')} | "
                f"{t.get('side','')} | {t.get('ensemble_prob_at_entry', 0):.1%} | "
                f"{t.get('actual_outcome', '?')} | {temp_str} | {threshold} | "
                f"{t.get('ensemble_members_at_entry', '?')} | "
                f"{t.get('ensemble_spread_at_entry', 0):.1f}F | "
                f"{t.get('bias_correction_applied', 0):+.1f}F | "
                f"${(t.get('pnl') or 0):+.2f} |"
            )
    else:
        lines.append("| No resolved trades yet | — | — | — | — | — | — | — | — | — | — | — |")

    lines += [
        "",
        "---",
        "",
        "## Dual-Arm Comparison (All-Time)",
        "",
        "The exploratory arm uses looser thresholds and runs paper-only.",
        "Comparing arms shows whether current guardrails are too tight.",
        "",
        "| Metric | Conservative (Real) | Exploratory (Paper) |",
        "|--------|--------------------|--------------------|",
        f"| Trades placed | {len([t for t in all_trades if t.get('strategy_arm')=='conservative'])} | {len([t for t in all_trades if t.get('strategy_arm')=='exploratory'])} |",
        f"| Resolved | {len(cons_resolved)} | {len(exp_resolved)} |",
        f"| Wins | {cons_wins} | {exp_wins} |",
        f"| Win rate | {f'{cons_wins/len(cons_resolved):.1%}' if cons_resolved else 'N/A'} | {f'{exp_wins/len(exp_resolved):.1%}' if exp_resolved else 'N/A'} |",
        f"| Total P&L | ${cons_pnl:+.2f} | ${exp_pnl:+.2f} |",
        f"| Brier score | {f'{_brier_score(all_trades):.3f}' if _brier_score(all_trades) else 'N/A'} | {f'{_brier_score(exp_resolved):.3f}' if _brier_score(exp_resolved) else 'N/A'} |",
        "",
        "_If the exploratory arm significantly outperforms conservative over 100+ markets,_",
        "_consider relaxing specific thresholds (see CLAUDE.md spec section 3.5)._",
        "",
        "---",
        "",
        "## Current Configuration Snapshot",
        "",
        "_(Recorded at time of report generation — for AI context)_",
        "",
    ]
    # Embed relevant settings sections
    for section in ("risk", "edge", "ensemble", "scanner", "regime", "experimentation"):
        section_data = settings.get(section)
        if section_data:
            lines.append(f"**{section}:**")
            lines.append("```yaml")
            for k, v in section_data.items():
                lines.append(f"  {k}: {v}")
            lines.append("```")
            lines.append("")

    output_path = WEEKLY_DIR / f"{week_label}.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Feed-to-AI package
# ---------------------------------------------------------------------------

def generate_feed_to_ai(
    db_path: str = DEFAULT_DB_PATH,
    current_week_path: Path | None = None,
    settings: dict | None = None,
) -> Path:
    """
    Assemble a ready-to-paste prompt package for Claude/Opus analysis.

    Contains:
      - Last 4 weekly reports (pure data, no AI opinions)
      - Current settings.yaml (risk limits, thresholds)
      - Pre-written analysis questions

    YOU control when this gets shown to an AI — nothing is sent automatically.
    Just open the file and paste its contents into Claude or Opus.
    """
    _ensure_dirs()

    if settings is None:
        settings = _load_settings()

    # Collect last 4 weekly reports (most recent first)
    weekly_files = sorted(WEEKLY_DIR.glob("*.md"), reverse=True)[:4]

    # Determine output filename from latest weekly
    if current_week_path:
        week_label = current_week_path.stem
    elif weekly_files:
        week_label = weekly_files[0].stem
    else:
        iso_year, iso_week, _ = date.today().isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"

    lines = [
        "# Kalshi Weather Trading System — AI Analysis Package",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "> **Instructions:** Paste this entire file into Claude Opus (or Sonnet) and",
        "> ask it to answer the analysis questions at the bottom. All data below is",
        "> sourced directly from the trading database — no AI has touched these numbers.",
        "",
        "---",
        "",
    ]

    # Include weekly reports
    if weekly_files:
        lines.append(f"# Performance Data ({len(weekly_files)} Most Recent Weekly Reports)")
        lines.append("")
        for wf in weekly_files:
            lines.append(f"<!-- SOURCE: {wf.name} -->")
            lines.append(wf.read_text(encoding="utf-8"))
            lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines += [
            "# Performance Data",
            "",
            "_No weekly reports generated yet. Run the daemon for at least one week_",
            "_before using this feed-to-AI package._",
            "",
            "---",
            "",
        ]

    # Current settings
    lines += [
        "# Current System Configuration",
        "",
        "```yaml",
    ]
    for section in ("trading_mode", "risk", "edge", "ensemble", "scanner", "regime", "calibration", "experimentation"):
        val = settings.get(section)
        if val is None:
            continue
        if isinstance(val, dict):
            lines.append(f"{section}:")
            for k, v in val.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append(f"{section}: {val}")
    lines += [
        "```",
        "",
        "---",
        "",
        "# Analysis Questions",
        "",
        "Please analyze the performance data above and answer the following:",
        "",
        "1. **Is the system profitable?** What is the trend in P&L, and is it statistically",
        "   meaningful given the number of resolved trades?",
        "",
        "2. **Is the ensemble well-calibrated?** Is the Brier score improving, stable, or",
        "   degrading? Are there cities where calibration is notably worse?",
        "",
        "3. **Are we beating the market?** Is the beat-market rate above 55%? Is there a",
        "   trend in the rolling 50-trade window?",
        "",
        "4. **Are the guardrails correctly sized?** Compare the conservative and exploratory",
        "   arm results. Are we skipping profitable trades (exploratory much better)?",
        "   Or are we correctly filtering out losers (exploratory worse)?",
        "",
        "5. **Fill quality assessment.** Is the fill rate acceptable (> 50%)? Is the maker",
        "   fill rate above 70%? Is slippage within target (< $0.01)?",
        "",
        "6. **Per-city analysis.** Which cities are performing best and worst? Are there",
        "   cities where the system should pause trading pending investigation?",
        "",
        "7. **Recommended parameter changes.** Based on the dual-arm comparison and",
        "   performance data, suggest any specific threshold changes (e.g. edge minimum,",
        "   Kelly fraction, volume filter) with reasoning. Be conservative — only recommend",
        "   changes supported by 50+ resolved trades of evidence.",
        "",
        "8. **Red flags.** Are there any anomalies, unexpected patterns, or signs of",
        "   regime change that warrant investigation before the next trading week?",
        "",
        "9. **Ensemble bias analysis.** Using the Calibration Detail table, examine cases",
        "   where `actual_max_temp_f` is available. For each city:",
        "   - Is the ensemble systematically over- or under-predicting temperatures?",
        "   - Does the bias vary by direction (e.g. warm bias vs cold bias)?",
        "   - Are there cases where ensemble spread was low (high confidence) but the",
        "     actual temp was far from the ensemble mean — suggesting the model was",
        "     confidently wrong?",
        "   - Suggest specific `bias_correction_applied` adjustments per city if patterns",
        "     are clear and N >= 10 observations per city.",
        "",
    ]

    output_path = FEED_TO_AI_DIR / f"{week_label}_prompt.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Settings loader
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    settings_path = Path(DEFAULT_SETTINGS_PATH)
    if not settings_path.exists():
        return {}
    with open(settings_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate trading reports.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--daily",
        metavar="YYYY-MM-DD",
        help="Generate daily report for this date (default: yesterday UTC)",
        nargs="?",
        const="yesterday",
    )
    group.add_argument(
        "--weekly",
        metavar="YYYY-MM-DD",
        help="Generate weekly report ending on this date (default: yesterday UTC)",
        nargs="?",
        const="yesterday",
    )
    group.add_argument(
        "--feed-to-ai",
        action="store_true",
        help="Regenerate the feed-to-AI prompt package from existing weekly reports",
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to trades.db")

    args = parser.parse_args()
    settings = _load_settings()

    if args.daily is not None:
        if args.daily == "yesterday":
            report_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        else:
            report_date = date.fromisoformat(args.daily)
        path = generate_daily_report(db_path=args.db, report_date=report_date, settings=settings)
        print(f"Daily report written to: {path}")

    elif args.weekly is not None:
        if args.weekly == "yesterday":
            week_end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        else:
            week_end = date.fromisoformat(args.weekly)
        path = generate_weekly_report(db_path=args.db, week_end=week_end, settings=settings)
        print(f"Weekly report written to: {path}")
        ai_path = generate_feed_to_ai(db_path=args.db, current_week_path=path, settings=settings)
        print(f"Feed-to-AI package written to: {ai_path}")

    elif args.feed_to_ai:
        path = generate_feed_to_ai(db_path=args.db, settings=settings)
        print(f"Feed-to-AI package written to: {path}")


if __name__ == "__main__":
    main()
