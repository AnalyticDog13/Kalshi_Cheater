"""
Smoke test: verify Kalshi demo API credentials work.
Run with: python scripts/smoke_test.py

Checks:
  1. .env loads correctly
  2. Private key file is readable
  3. Kalshi demo API authenticates
  4. Can fetch account balance
  5. Can list open weather markets (KXHIGHNY)
  6. Telegram bot token is valid (if configured)
"""

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

console = Console()


async def main():
    console.print("\n[bold cyan]Kalshi Weather Trader — Smoke Test[/bold cyan]\n")

    results = []

    # ------------------------------------------------------------------
    # 1. Check .env variables are present
    # ------------------------------------------------------------------
    required_vars = [
        "TRADING_MODE",
        "KALSHI_DEMO_API_KEY_ID",
        "KALSHI_DEMO_PRIVATE_KEY_PATH",
        "ANTHROPIC_API_KEY",
    ]
    all_env_ok = True
    for var in required_vars:
        val = os.environ.get(var, "")
        if val and "PASTE_" not in val:
            results.append(("ENV", var, "OK", "green"))
        else:
            results.append(("ENV", var, "MISSING or placeholder", "red"))
            all_env_ok = False

    if not all_env_ok:
        _print_results(results)
        console.print("\n[red]Fix missing env vars in .env before continuing.[/red]")
        return

    # ------------------------------------------------------------------
    # 2. Check private key file
    # ------------------------------------------------------------------
    key_path = os.environ["KALSHI_DEMO_PRIVATE_KEY_PATH"]
    # Normalise Windows backslashes
    key_path = key_path.replace("\\", "/")

    if os.path.isfile(key_path):
        with open(key_path) as f:
            first_line = f.readline().strip()
        if "BEGIN" in first_line:
            results.append(("KEY", key_path, "OK valid PEM header", "green"))
        else:
            results.append(("KEY", key_path, f"Unexpected header: {first_line[:40]}", "yellow"))
    else:
        results.append(("KEY", key_path, "FILE NOT FOUND", "red"))
        _print_results(results)
        console.print(f"\n[red]Key file not found at: {key_path}[/red]")
        return

    # ------------------------------------------------------------------
    # 3 & 4. Connect to Kalshi demo API and fetch balance
    # ------------------------------------------------------------------
    try:
        from src.kalshi_client import KalshiClient, parse_price
        client = KalshiClient.from_env(mode="demo")
        results.append(("AUTH", "Kalshi demo API", "OK connected", "green"))

        balance_resp = await client.get_balance()
        # Balance object varies by SDK version — try common field names
        balance = (
            getattr(balance_resp, "balance", None)
            or getattr(balance_resp, "available_balance_cents", None)
            or getattr(balance_resp, "available_balance", None)
        )
        if balance is not None:
            results.append(("BALANCE", "Demo account balance", f"${float(balance)/100:.2f}", "green"))
        else:
            results.append(("BALANCE", "Demo account balance", f"Response: {balance_resp}", "yellow"))

    except Exception as exc:
        results.append(("AUTH", "Kalshi demo API", f"FAILED: {exc}", "red"))
        _print_results(results)
        return

    # ------------------------------------------------------------------
    # 5. List weather markets
    # ------------------------------------------------------------------
    try:
        markets = await client.list_markets(series_ticker="KXHIGHNY", status="open", limit=5)
        if markets:
            sample = markets[0]
            ticker = getattr(sample, "ticker", "?")
            bid = getattr(sample, "yes_bid_dollars", "?")
            ask = getattr(sample, "yes_ask_dollars", "?")
            results.append((
                "MARKETS",
                f"KXHIGHNY open markets",
                f"OK found {len(markets)} (e.g. {ticker} bid={bid} ask={ask})",
                "green",
            ))
        else:
            results.append(("MARKETS", "KXHIGHNY open markets", "0 found — may be off-hours", "yellow"))
    except Exception as exc:
        results.append(("MARKETS", "KXHIGHNY market list", f"FAILED: {exc}", "red"))

    # ------------------------------------------------------------------
    # 6. Telegram token check
    # ------------------------------------------------------------------
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if bot_token and "PASTE_" not in bot_token:
        try:
            import httpx
            url = f"https://api.telegram.org/bot{bot_token}/getMe"
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(url)
            data = resp.json()
            if data.get("ok"):
                bot_name = data["result"].get("username", "?")
                results.append(("TELEGRAM", "Bot token", f"OK @{bot_name}", "green"))
            else:
                results.append(("TELEGRAM", "Bot token", f"Invalid: {data}", "red"))
        except Exception as exc:
            results.append(("TELEGRAM", "Bot token", f"FAILED: {exc}", "red"))

        if chat_id and "PASTE_" not in chat_id:
            results.append(("TELEGRAM", "Chat ID", f"OK {chat_id}", "green"))
        else:
            results.append(("TELEGRAM", "Chat ID", "Not set yet", "yellow"))
    else:
        results.append(("TELEGRAM", "Bot token", "Not configured (log_only mode)", "yellow"))

    # ------------------------------------------------------------------
    # Print summary
    # ------------------------------------------------------------------
    _print_results(results)

    failed = [r for r in results if r[3] == "red"]
    if not failed:
        console.print("\n[bold green]All checks passed. Ready for Phase 1.[/bold green]\n")
    else:
        console.print(f"\n[bold red]{len(failed)} check(s) failed. Fix before continuing.[/bold red]\n")


def _print_results(results):
    table = Table(show_header=True, header_style="bold")
    table.add_column("Category", width=10)
    table.add_column("Check", width=40)
    table.add_column("Result", width=50)
    for category, check, result, color in results:
        table.add_row(category, check, f"[{color}]{result}[/{color}]")
    console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
