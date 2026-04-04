# Kalshi AI Weather Trading System — Complete Specification v2.0

**This is the single, authoritative specification. It supersedes all prior versions (main spec v1.3, addendum v1.4, audit reports v1 and v2). Give this document to Claude Code with a fresh GitHub repository.**

**Last verified against Kalshi API: March 25, 2026**

---

## FUTURE IMPROVEMENTS (not yet built — requires data before implementing)

### Smart stale order replacement (medium complexity)
**What:** Instead of cancelling every order older than `order_expiry_minutes`, each cycle re-evaluate each resting order's market and only cancel/replace if: (a) the edge has disappeared, or (b) a meaningfully better edge exists at the current price. Orders where the edge is still valid and the limit price is still competitive would stay on the book.

**Why it helps:** On thin weather markets it can take >10 min to get a maker fill. The current system cancels a perfectly good order just because it's old, then re-places it at essentially the same price — wasting a cycle and risking a slightly worse price.

**Why not yet:** No fill-rate data to confirm the 10-min timeout is actually hurting fills. More complex order management = more potential bugs in a live system. Worth building after 50+ trades if you see a pattern of "cancelled at 10 min, re-placed, filled 2 min later."

**Files to change:** `src/order_executor.py` `monitor_open_orders()` (add per-order edge re-computation and comparison logic), `src/daemon.py` (pass current scan results into monitor so it can compare).

---

## CHANGES (session 16 — 2026-04-03)
*Stale order cancellation bug fixed. Order cap now immediately reflects cancellations. Cancel log deduped.*

| Change | File | Description |
|--------|------|-------------|
| CRITICAL FIX | `src/order_executor.py` `monitor_open_orders()` | Same Kalshi status string bug as session 15 dedup fix. Was calling `get_orders(status="open")` which returns 0 — Kalshi uses `"resting"`. Fix: fetch all orders with `status=None`, filter to resting statuses. Without this, stale orders were never auto-cancelled, order cap stayed permanently at 5/5, and no new orders could ever be placed. |
| FIX | `tests/test_order_executor.py` | Added `order.status = "resting"` to `_mock_open_order()` helper and `test_order_with_string_created_time` so stale-cancel tests pass with the new status filter. |
| FIX | `src/daemon.py` `_monitor_orders()` | Changed return type from `None` to `list[str]` — now returns the list of cancelled order IDs so `_run_cycle()` can use it. |
| FIX | `src/daemon.py` `_run_cycle()` | `open_orders_count` now subtracts `len(just_cancelled)` (orders cancelled this cycle by `_monitor_orders`). Kalshi's GET endpoint has eventual-consistency lag — cancelled orders may still appear as `"resting"` for several seconds after cancellation. Without this, the order cap stayed at 5/5 for the entire cycle even after stale orders were cancelled, blocking new trades until the next cycle. |
| FIX | `src/daemon.py` `_sync_fills()` | Added `logged_cancelled: set[str]` to deduplicate "Marked cancelled" log per ticker per cycle. Ghost records cause the same ticker to be re-processed every cycle (multiple DB records per ticker from pre-fix duplicate order sessions). Now each ticker logs at most once per cycle regardless of ghost record count. |

**State as of end of session 16:** 529/529 tests passing. Stale cancellation now fully working — verified live: 5 orders cancelled (64 min old) in first cycle after restart. Order cap immediately freed (subtraction fix). Cancel log noise eliminated. All three fixes working together: stale cancel → immediate cap relief → no duplicate new orders.

## CHANGES (session 15 — 2026-04-03)
*Fill dedup bug fixed (resting orders not counted). AI date bug fixed. DB corrected with real fill prices. Missing position inserted.*

| Change | File | Description |
|--------|------|-------------|
| CRITICAL FIX | `src/daemon.py` `_run_cycle()` | `get_orders(status="open")` returned 0 because Kalshi uses `"resting"` not `"open"`. Fix: fetch all orders with `status=None`, filter to resting ones (`{"resting","open","pending"}`). `open_orders_count` now correctly reflects resting orders on the book — the order cap was always showing `0+N >= 5` instead of `N+0 >= 5`, causing duplicate orders every cycle. |
| CRITICAL FIX | `src/daemon.py` `_run_cycle()` dedup | `db_open_tickers` only included filled positions (`get_open_positions()`), not pending/resting orders (`get_pending_trades()`). Fix: union both sets. Previously the daemon re-ordered the same tickers every cycle as long as the limit orders hadn't filled yet. |
| FIX | `src/ai_reasoner.py` `_build_prompt()` | Added `Today's date (UTC): {today}` line to sanity-check prompt. Without it, Claude thought April 4, 2026 was "15+ months away" (it was reasoning from its training cutoff), wasting API budget on irrelevant concerns. |
| FIX | `src/main.py` status command | Changed `Entry` column to `Fill` — now displays `fill_price` (actual execution price) instead of `entry_price` (limit order price). Both were set to 0.50 as placeholders for pre-fix records. |
| MANUAL DB FIX | `data/trades.db` | Corrected fill prices and entry prices for 3 pre-fix records: KXHIGHCHI-26APR04-T62 ×2 → 0.38, KXHIGHDEN-26APR04-T61 × 1 → 0.49. Also updated `entry_price` to match. |
| MANUAL DB INSERT | `data/trades.db` | Inserted missing KXHIGHCHI-26APR03-T59 position (NO, 2 contracts, fill_price=0.52) — this real position was never tracked by the daemon at all. |

**Verified live (session 15):** Kalshi status strings confirmed as `"resting"` (pending), `"executed"` (filled), `"canceled"` (cancelled). All three are handled correctly. After fix: cycle shows `open_orders_count=5`, order cap correctly blocks new orders, no duplicates placed. Fill sync working — cancelled orders correctly marked in DB within one cycle of cancellation.

## CHANGES (session 14 — 2026-04-03)
*API data integrity overhaul. All Kalshi values now read from the API rather than assumed. Critical fill-sync no-op bug fixed. 529/529 tests passing.*

| Change | File | Description |
|--------|------|-------------|
| CRITICAL FIX | `src/daemon.py` `_sync_fills()` + `src/tracker.py` | `_sync_fills()` called `get_open_positions()` (SQL: `order_filled_time IS NOT NULL`) then filtered for `t.order_filled_time is None` — those two conditions can never both be true. `pending` was always empty, so fills were **never detected** since the function was first written. Fixed by adding `tracker.get_pending_trades()` (SQL: `order_filled_time IS NULL AND order_cancelled=0 AND strategy_arm='conservative'`) and updating `_sync_fills()` to call it instead. Fill sync now actually runs. |
| NEW | `src/tracker.py` `get_pending_trades()` | New method: returns conservative-arm trades that are placed but not yet filled (`order_filled_time IS NULL`). Distinct from `get_open_positions()` which returns filled-but-unresolved holdings. |
| FIX | `src/kalshi_client.py` `get_orders()` | Added `status=None` support. Calling with `status=None` fetches ALL orders without a status filter. Used by `_sync_fills()` to get every order and inspect `order.status` directly, avoiding the failure mode where `get_orders(status="filled")` returned 0 because Kalshi uses different status strings (e.g. `"executed"` not `"filled"`). |
| FIX | `src/daemon.py` `_sync_fills()` | Complete overhaul (separate from no-op fix above). Now makes ONE call with `status=None`, inspects `order.status` on each order directly, matches against broad `FILLED_STATUSES = {"filled","executed","complete","closed"}` and `CANCELLED_STATUSES = {"cancelled","canceled","expired","voided"}`. Added: (1) diagnostic log showing Kalshi order count and status breakdown every cycle; (2) quantity cross-check with WARNING and DB update on mismatch; (3) field dump when fill price extraction fails; (4) pre-fix UUID trades logged at debug level only. |
| FIX | `src/daemon.py` `_extract_fill_price()` | Added `avg_price` as first-priority field (most reliable for actual execution price, especially on partial fills). Added debug log showing which field was used. Previously only tried `yes_price`, `no_price`, `yes_bid_dollars`, `no_bid_dollars`, `price`. |
| NEW | `src/daemon.py` `_extract_fill_time()` | New helper. Tries `updated_time`, `fill_time`, `execution_time`, `close_time`, `placed_time` in priority order. Previously used inline `getattr(order, "close_time") or getattr(order, "updated_time")` which often missed the actual fill time. |
| NEW | `src/daemon.py` `_extract_filled_quantity()` | New helper. Reads `filled_count`, `filled_count_fp`, `executed_count` first; falls back to `count - remaining_count`; then `count`. Returns `None` if undetermined (caller keeps DB value). |
| NEW | `src/daemon.py` `_order_field_summary()` | New debug helper. Dumps all non-None fields from a Kalshi order object via `model_dump()` or `__dict__`. Used in warning logs when fill price extraction fails. |
| FIX | `src/daemon.py` dedup (`_run_cycle`) | `db_open_tickers` now filters `strategy_arm != "exploratory"`. Previously included exploratory paper trades which could block real order placement on those tickers. |
| FIX | `src/daemon.py` `_sync_resolutions()` | When resolving a market where `fill_price` is 0.0 or 0.50 (sentinel placeholder from pre-fix orders), logs a WARNING that P&L for that trade is unreliable. |
| FIX | `src/tracker.py` `update_fill()` | Added optional `filled_quantity` parameter. When provided, overwrites DB `quantity` so partial fills are recorded correctly. Pass `None` to leave unchanged (default). |
| FIX | `tests/test_order_executor.py` | Fixed stale test `test_buy_yes_empty_book_uses_fair_value_minus_buffer` — expected 0.68 (fair_value - 0.02) but `MAX_CONTRACT_PRICE=0.50` cap (added session 9, test never updated) brings it to 0.50. |
| NEW | `tests/test_tracker.py` | Added 6 tests for `get_pending_trades()` and 2 tests for `update_fill()` `filled_quantity` parameter. |

**State as of end of session 14:** 529/529 tests passing. Fill sync now works for the first time — `get_pending_trades()` correctly queries unfilled orders, `_sync_fills()` fetches all Kalshi orders and uses `order.status` directly. On first run the log will show `"Status breakdown: {'resting': N, ...}"` revealing the actual Kalshi status strings. If fills still aren't detected after the first run, check that log line and update `FILLED_STATUSES` in `daemon.py` if needed.

## CHANGES (session 13 — 2026-04-03)
*Kalshi API response unwrapping bugs fixed. Fill tracking and resolution detection now work. Status command fixed. API data integrity overhaul identified as next priority.*

| Change | File | Description |
|--------|------|-------------|
| FIX | `src/kalshi_client.py` `get_market()` | `GetMarketResponse` wraps the actual market inside a `.market` attribute (a Pydantic `Market` object). `get_market()` now unwraps and returns the inner object directly so callers can access `.status`, `.result`, `.close_time` etc. via normal attribute access. Previously all these fields returned `None` or `""`. |
| FIX | `src/kalshi_client.py` `create_order()` | `CreateOrderResponse` wraps the actual order inside a `.order` attribute. `create_order()` now unwraps and returns the inner `Order` object directly so `order.order_id` is accessible. Previously `order_id` was always `None`, causing the daemon to generate a random UUID as the trade_id — making fill sync impossible since Kalshi order IDs never matched DB trade IDs. |
| FIX | `src/daemon.py` `_sync_resolutions()` | Updated to handle both dict and Pydantic model responses from `get_market()` — falls through to `getattr` branch since the unwrapped inner object is a Pydantic `Market` model. Now correctly reads `status` and `result` fields. |
| FIX | `src/main.py` `status` command | Same `strategy_arm == "conservative"` bug as was fixed in `_send_daily_summary()` last session. Changed to `strategy_arm != "exploratory"` so older DB records with `strategy_arm=None` are not excluded. |
| MANUAL DB FIX | `data/trades.db` | Manually marked 3 known real positions as filled (since historical fills are not retrievable from Kalshi API): KXHIGHCHI-26APR04-T62 ×2 records, KXHIGHDEN-26APR04-T61 ×1 record. `order_filled_time` and `fill_price` set to current time and entry_price (0.50 — real fill prices unknown, see NEXT STEPS). |

**State as of end of session 13:** Status command now shows 3 open positions (Chicago T62, Denver T61). `_sync_resolutions()` will correctly detect settled status tomorrow. `create_order()` fix means future orders will store real Kalshi order IDs and fill sync will work automatically going forward. Existing pre-fix DB records have wrong trade_ids (UUIDs) and wrong fill prices (0.50) and cannot be retroactively corrected from the API.

## CHANGES (session 12 — 2026-04-03)
*Claude API waste fix, daily summary fix, automatic resolution detection added.*

| Change | File | Description |
|--------|------|-------------|
| FIX | `src/market_scanner.py` | Added `skip_tickers` param to `scan()` and `_scan_series()`. Claude API calls now suppressed for any ticker already in the dedup/manual-skip set — no wasted credits on trades that will be blocked anyway. |
| FIX | `src/daemon.py` | Moved `open_order_tickers` / `_manual_skip` / `db_open_tickers` assembly to BEFORE `scan()` call, then passes `skip_tickers=open_order_tickers` into scanner. Previously the skip set was built after scan, so Claude could fire on already-blocked tickers. |
| FIX | `src/daemon.py` `_send_daily_summary()` | Fixed "Open positions: 0" in daily summary. Changed filter from `strategy_arm == "conservative"` to `strategy_arm != "exploratory"` — older DB records have `strategy_arm=None` and were being excluded. |
| NEW | `src/daemon.py` `_sync_resolutions()` | Added automatic outcome recording. Every cycle, checks if any filled open position has `status="settled"` on Kalshi. If so, reads `result` ("yes"/"no"), calls `tracker.record_outcome()`, and releases exposure. Tickers drop off the dedup list automatically — no manual command needed after markets resolve. |

**Confirmed working (4 cycles observed):** No Python errors. Dedup stable at 20 tickers. Zero new orders placed (all approved trades already held). Zero Claude API calls. Ensemble cache working (cycle 1: 22s, cycles 2-4: ~2s). Ready for VPS.

## BUGS FIXED (session 11 — 2026-04-03)
*Deduplication, order cap, and fill sync bugs fixed. Daemon now stable.*

| Bug | File | Fix applied |
|-----|------|-------------|
| #1 CRITICAL | `src/kalshi_client.py` | `get_positions()` had `settlement_status="unsettled"` param — invalid for this SDK version (pydantic error). Removed. Root cause: Kalshi returns `{'positions': None}` — SDK bug, cannot be fixed client-side |
| #2 CRITICAL | `src/daemon.py` | Within-cycle order cap not enforced — all 12 approved trades placed in one cycle ignoring `max_open_orders=5`. Fixed with `placed_this_cycle` counter that stops execution when `open_orders_count + placed_this_cycle >= max_orders` |
| #3 CRITICAL | `src/daemon.py` | DB dedup not used — cancelled orders stayed in DB as "open", blocking fresh trades across restarts. Fixed by adding `tracker.get_open_positions()` to `open_order_tickers` set (persists across restarts) |
| #4 HIGH | `src/daemon.py` | `_sync_fills()` only fetched `status="filled"` orders — never detected cancellations, so cancelled orders accumulated in DB forever. Fixed by also fetching `status="cancelled"` and calling `_mark_order_cancelled()` for matches |

**Root cause of `get_positions()` returning None:** Kalshi SDK returns `{'positions': None, 'cursor': ''}` — the `positions` field exists but is `None`. Added `limit=100` param (may help) but fundamentally the Kalshi API isn't returning position data via this SDK method. **Workaround in place:** DB dedup via `tracker.get_open_positions()` makes this moot — we track filled positions locally.

## BUGS FIXED (session 10 — 2026-04-03)
*get_positions() deduplication bug causing runaway orders on filled positions.*

| Bug | File | Fix applied |
|-----|------|-------------|
| #1 CRITICAL | `src/daemon.py` | `open_order_tickers` dedup used `getattr(o, "ticker")` only — positions use `market_ticker`. Fixed with helper that tries both attribute names |
| #2 CRITICAL | `src/kalshi_client.py` | `get_positions()` returning empty list — added `settlement_status="unsettled"` param and fallback to `market_positions` attr. Added WARNING log to show raw response attrs |

**Result of bug:** Denver YES position (8 contracts from session 9) was not detected, daemon re-placed orders every cycle accumulating 12 Denver contracts total. $0.84 at risk (7¢ each), payout $12 if Denver hits 59°F today.

**Status of `get_positions()` fix:** WORKAROUND IN PLACE. Kalshi SDK returns `positions=None` — unfixable on our side. DB dedup now used instead.

## BUGS FIXED (session 9 — 2026-04-02)
*6 runtime bugs found and fixed during first live trading session.*

| Bug | File | Fix applied |
|-----|------|-------------|
| #1 CRITICAL | `src/daemon.py` | Duplicate orders every cycle — added `open_order_tickers` set; skip re-ordering tickers with existing open orders |
| #2 CRITICAL | `src/order_executor.py` | BUY_YES empty-book fallback used `fair_value - 0.02` uncapped — 100% ensemble → $0.98 order. Added `MAX_CONTRACT_PRICE = 0.50` cap |
| #3 HIGH | `src/risk_manager.py` | Check 11 required status `"open"` but Kalshi returns `"active"` — all markets rejected. Now accepts both |
| #4 MEDIUM | `src/market_scanner.py` | AI sanity check fired on already-rejected markets, wasting Claude API budget. Now only fires when `risk.approved == True` |
| #5 MEDIUM | `src/market_scanner.py` | `_compute_spread()` defaulted missing YES ask to $1.00, making all empty-book markets look like $0.55+ spread. Now infers from NO bid, returns 0.0 if both absent |
| #6 MEDIUM | `config/settings.yaml` | `min_volume_fp: 5` blocked all markets (all had vol=0.00 — freshly opened). Changed to `0` |
| #7 LOW | `src/market_scanner.py` | "Between" range markets (`B71.5` format, title "be 71-72°") caused parse warnings. Now silently skipped at ticker detection |

## BUGS FIXED (session 8 — 2026-04-02)
*All 5 bugs from the session 7 audit were fixed. 521/521 tests still passing.*

| Bug | File | Fix applied |
|-----|------|-------------|
| #1 CRITICAL | `src/kalshi_client.py:466` | `"market_positions"` → `"positions"` |
| #2 MEDIUM | `src/edge_calculator.py:326` | `%.1%%` → `%.1f%%`, args ×100 |
| #2 MEDIUM | `src/data_feeds/weather_ensemble.py:333` | `%.1%%` → `%.1f%%` |
| #3 LOW-MEDIUM | `src/kalshi_client.py:387-393` | `"get_series"` → `"get_series_by_ticker"`, `series_ticker=` → `ticker=` |
| #4 LOW-MEDIUM | `src/daemon.py:329` | fallback `100.0` → `30.0` (updated again for $30 bankroll) |
| #4 LOW-MEDIUM | `src/daemon.py:992` | `cents > 100` → `cents >= 200` |
| #5 LOW | `src/kalshi_client.py:546` | Added `self._cache.invalidate("positions")` |

---

## BUILD PROGRESS LOG
*Last updated: 2026-04-03 (session 16 — Stale order cancellation fixed. Order cap now immediately reflects stale cancellations. Cancel log deduped. 529/529 tests passing.). Update this section at the start of every new session.*

### Current Status: 529/529 tests passing. LIVE trading active. Stale order cancellation working. Order cap correctly freed after cancellations. APR03 markets resolving tonight, APR04 tomorrow. VPS deployment is next priority.

### Live Configuration (current)
- **Bankroll:** ~$29.77 (live Kalshi account as of session 11)
- **Environment:** LIVE Kalshi API (`api.elections.kalshi.com`) — real money, real fills, NOT demo
- `config/settings.yaml`: `trading_mode: "live"`, `max_position_dollars: 1.00`,
  `max_total_exposure_dollars: 15.00`, `max_daily_loss_dollars: 3.00`, `max_open_orders: 5`,
  `max_contract_price_dollars: 0.50` (hard cap — Check 12 in risk manager),
  `min_volume_fp: 0` (Kalshi weather markets open at zero volume)
- `src/risk_manager.py`: 12-point pre-trade checklist; Check 11 accepts `"active"` or `"open"`
- `src/daemon.py`: dedup uses DB `tracker.get_open_positions()` + Kalshi open orders + manual skip set. `placed_this_cycle` counter enforces `max_open_orders` cap within a cycle. `_sync_fills()` fetches both filled and cancelled orders. `_sync_resolutions()` auto-records outcomes for settled markets each cycle.
- `src/market_scanner.py`: Claude API calls suppressed for tickers in `skip_tickers` (dedup/manual-skip set), preventing wasted credits on blocked trades.
- `src/kalshi_client.py`: `get_positions()` returns empty (Kalshi SDK bug — `positions=None`). Workaround: DB dedup. WARNING log still present for diagnostics.
- `src/order_executor.py`: BUY_YES empty-book price capped at $0.50
- Test count: 521/521

### Open positions as of end of session 16 (2026-04-03 ~2pm ET)
- Chicago NO × 2 (`KXHIGHCHI-26APR03-T59`) — fill_price=0.52 — resolves tonight
- Chicago YES × 4 (`KXHIGHCHI-26APR04-T62`, 2 DB records × 2 qty) — fill_price=0.38 — resolves Apr 4
- Denver YES × 2 (`KXHIGHDEN-26APR04-T61`) — fill_price=0.49 — resolves Apr 4
- 5 previously-resting orders (T55, T67, T60, T85, T78) — auto-cancelled as stale in session 16 cycle 1. Order cap now at 0, daemon will place new orders next cycle.
- Daemon is RUNNING.

### Manual skip list in `src/daemon.py` (clean up after Apr 3 resolves)
```python
_manual_skip: set[str] = {
    "KXHIGHDEN-26APR03-T59",  # sold manually
    "KXHIGHDEN-26APR03-T58",  # sold manually
    "KXHIGHCHI-26APR04-T62",  # filled (Chicago 63F+)
    "KXHIGHDEN-26APR03-T61",  # filled (Denver 60F or below)
    "KXHIGHCHI-26APR03-T59",  # filled (Chicago today 59F or below)
    "KXHIGHDEN-26APR04-T60",  # safety net
    "KXHIGHDEN-26APR04-T61",  # safety net
}
```
After APR03 markets resolve and the daemon auto-records their outcomes, the APR03 entries in `_manual_skip` are dead weight. Remove them (leave APR04 entries until Apr 4 resolves).

### KNOWN REMAINING ISSUES (as of session 16)
- `get_positions()` returns `{'positions': None}` from Kalshi SDK — unfixable, workaround in place via DB dedup.
- `parse_err=40` per cycle — B-range markets correctly skipped. Cosmetic only.
- `Could not fetch fee coefficient for KXHIGHDC/PHX/SEA/ATL` — 404 on series endpoint, uses default 0.07. No trading impact.
- **DB has ~489 ghost records** from pre-fix sessions. Only block old APR03/APR04 tickers which resolve tonight/tomorrow — new APR05+ tickers are unaffected.
- **`_sync_fills()` matches by `trade.trade_id` against Kalshi `order_id`** — only works for orders placed AFTER the session 13 `create_order()` fix. Pre-fix UUID trades get a debug-level log — cosmetic, not an error.
- **APR04 entries in `_manual_skip`** still present — clean up after Apr 4 resolves.
- **Kalshi status strings confirmed:** `"resting"` = pending on book, `"executed"` = filled, `"canceled"` = cancelled. All handled correctly.

### NEXT STEP (read this first on new session)

#### Priority 1 — Confirm new orders placed after session 16 restart
After restart with stale-cancel fix, order cap was freed. Next cycle (cycle 2) should show new orders placed on unblocked tickers. Look for `[TRADE PLACED]` in logs or Telegram. If no new orders after 2 cycles, paste cycle 2 log.

#### Priority 2 — Clean up manual skip after APR04 resolves (night of Apr 4)
Remove all entries from `_manual_skip` in `src/daemon.py`. `_sync_resolutions()` handles cleanup automatically.
```python
_manual_skip: set[str] = {
    # Empty after APR04 resolves
}
```

#### Priority 3 — Monitor fill detection on new orders
New orders placed post-session-16 have real Kalshi order IDs. When one fills, `_sync_fills()` should log:
```
Marked filled (Kalshi status='executed'): KXHIGH...
```

#### Priority 4 — VPS deployment
1. Stop daemon: `python -m src.main stop`
2. Set up VPS (DigitalOcean/Linode/Hetzner ~$6/month)
3. Copy project, `.env`, `.pem` key
4. Ask Claude Code to write the systemd service file
5. Start: `python -m src.main start --live`

---

## REPORT SYSTEM

**Zero AI in report generation.** All numbers come directly from `data/trades.db`. AI is only involved when you manually paste a report into Claude/Opus.

### Output Structure
```
reports/
├── daily/          YYYY-MM-DD.md        — generated every day at UTC midnight
├── weekly/         YYYY-WXX.md          — generated every Monday at UTC midnight (covers prior Mon–Sun)
└── feed_to_ai/     YYYY-WXX_prompt.md   — generated with each weekly report; paste into Opus manually
```

### What Each Report Contains

**Daily (`reports/daily/YYYY-MM-DD.md`):**
- Status: `NORMAL` or `ALERT` with specific reason (e.g. "Daily P&L -$2.50 approaching loss limit", "Brier score 0.26 above threshold", "No fills in 3 days")
- Activity: orders placed / filled / expired / resolved; exploratory arm paper trades
- P&L: today's resolved P&L, all-time P&L, fees, open exposure
- Tables: resolved trades today, open positions
- Performance snapshot: rolling-50 Brier score, rolling-50 beat-market rate

**Weekly (`reports/weekly/YYYY-WXX.md`):**
- Full summary table (this week + all-time)
- Day-by-day P&L breakdown
- Ensemble & prediction performance (Brier, beat-market rate with rolling windows)
- Fill quality (fill rate, maker rate, average slippage)
- Per-city performance table (resolved count, win rate, P&L, Brier per city)
- Dual-arm comparison (conservative real trades vs exploratory paper trades)
- Current configuration snapshot (risk limits, edge thresholds, ensemble settings)

**Feed-to-AI (`reports/feed_to_ai/YYYY-WXX_prompt.md`):**
- Last 4 weekly reports concatenated
- Current `settings.yaml` relevant sections
- 8 pre-written analysis questions for Opus (P&L trend, calibration, beat-market, guardrail sizing, fill quality, per-city, parameter recommendations, red flags)
- **Never sent anywhere automatically — open the file and paste it into Claude/Opus yourself**

### How Reports Are Triggered
- The daemon generates reports automatically at UTC midnight (first cycle after midnight)
- Reports are written via `asyncio.to_thread` so they never block the trading loop
- A report write failure logs a warning and trading continues unaffected

### Manual Generation
```bash
python scripts/generate_reports.py --daily              # yesterday
python scripts/generate_reports.py --daily 2026-04-02   # specific date
python scripts/generate_reports.py --weekly             # last completed week
python scripts/generate_reports.py --weekly 2026-04-06  # specific week-end date
python scripts/generate_reports.py --feed-to-ai         # rebuild prompt package from existing weeklies
```

### How to Use the Feed-to-AI Package
1. Open `reports/feed_to_ai/YYYY-WXX_prompt.md`
2. Copy the entire contents
3. Paste into Claude Opus (or Sonnet) and ask it to answer the questions at the bottom
4. Use Opus's recommendations to decide whether to adjust thresholds, investigate cities, or change parameters
5. Any parameter changes must be made manually in `config/settings.yaml` — the AI never touches the system

---

---

## PHASE 3: GETTING IT RUNNING (Operational Checklist)

*The code is done and bugs are fixed. The system is configured for LIVE real-money trading on the live Kalshi environment (not demo). Follow these steps to start.*

### Step 1 — Verify Everything Works (30 minutes, one time)

Run these in order. Fix any failures before proceeding.

```bash
# 1. Verify all API credentials and connections
python scripts/smoke_test.py

# 2. Verify station mappings match NOAA and Kalshi sources
python scripts/validate_stations.py

# 3. Build initial bias corrections (takes ~10-15 min, makes API calls)
python scripts/compute_station_bias.py --days-back 365
```

**What to watch for in validate_stations.py:**
- WARN rows are usually OK — small coordinate differences between NOAA and our config
- FAIL rows mean the station_id is wrong and must be fixed in `config/station_mapping.yaml` before trading
- If a Kalshi API call fails on a particular series, that's OK — NOAA check is the critical one

**What to watch for in compute_station_bias.py:**
- "0 months stored" for a city = NOAA has no data for that station, or station ID is wrong
- Biases larger than ±4°F are suspicious — may indicate wrong station coordinates
- The script writes to `data/station_bias.db`; check it exists after the script finishes

### Step 2 — First Daemon Run (watch the logs live)

```bash
# Start daemon in live mode, log to console
python -m src.main start --live

# In a separate terminal — check status
python -m src.main status
```

**What to watch in the logs (first 2-3 cycles):**

| Log message | Meaning |
|-------------|---------|
| `=== Cycle 1 @ ...` | Daemon started, first cycle running |
| `Scan: fetched=N, evaluated=M, approved=K` | Scanner working; K=approved trades this cycle |
| `kalshi_api circuit OPEN` | Kalshi API unreachable; wait for 5-min cooldown |
| `open_meteo circuit OPEN` | Open-Meteo unreachable; check internet/rate limits |
| `Edge evaporated` | Market moved before order; normal, not an error |
| `[TRADE PLACED]` in Telegram | First live order submitted |
| `Service kalshi_api failure 1/3` | API hiccup; not tripped yet, just watching |

**Common first-run issues and fixes:**

| Problem | Fix |
|---------|-----|
| `KALSHI_LIVE_API_KEY_ID not set` | Check `.env` is loaded with live credentials; re-run `python scripts/smoke_test.py` |
| `approved=0` every cycle | Normal if no markets have sufficient edge right now |
| `filtered_no_station=N` (high N) | Station mapping missing entries; add series to `station_mapping.yaml` |
| `parse_threshold_from_ticker` warnings | Kalshi changed ticker format; fix regex in `market_scanner.py:_parse_threshold_from_text()` |
| `open_meteo circuit OPEN` immediately | Open-Meteo rate-limited; daemon will auto-recover in 5 min |

### Step 3 — Deploy for Continuous Operation (Week 1+)

The system **cannot run on a laptop that sleeps**. Options:

**Option A — Keep your Windows machine running** (simplest for now):
```bash
# Run in background, log to file
python -m src.main start > logs/daemon.log 2>&1
```
Create `logs/` directory first: `mkdir logs`

**Option B — Deploy to a VPS** (recommended for real trading):
- DigitalOcean, Linode, or Hetzner — ~$6/month for a basic droplet
- Copy the project, `.env`, and PEM key to the server
- Run as a systemd service so it restarts on crash
- Ask Claude Code to write the systemd service file when you're ready

**Monitoring while running:**
```bash
# Check current status
python -m src.main status

# View recent log output
tail -f logs/daemon.log

# Pause trading without stopping the daemon (e.g. during Kalshi maintenance)
python -m src.main pause
python -m src.main resume

# Graceful stop
python -m src.main stop
```

### Step 4 — Evaluate After ~100 Resolved Markets (2-4 weeks)

Weather markets in the system's cities typically resolve once per day each. With 10 cities and daily markets, you should accumulate ~100+ resolved markets within 2-4 weeks of continuous operation.

```bash
# Run after the daemon has been running for 2+ weeks
python scripts/calibration_report.py
```

**Key metrics and what they mean:**

| Metric | Target | If failing |
|--------|--------|------------|
| Brier score (rolling 50) | < 0.18 | Ensemble is mis-calibrated; run `compute_station_bias.py` with more data |
| Beat-market rate | > 55% | Our probabilities are better than the market's |
| Beat-market rate (rolling) | > 50% | If < 48%, system auto-raises edge thresholds; if sustained, halt |
| Maker fill rate | > 70% | If low, orders are expiring unfilled; may need to be more aggressive on price |
| ROI | Positive | If negative after 100+ trades, there may be a systematic issue |
| Dual-arm comparison | Exp arm ≈ Cons arm | If exp arm much better, guardrails are too tight |

Also run `collect_historical.py` to backfill historical market data, then `backtest.py --mode hindcast` for a quick sanity check (see backtest caveat about lookahead bias).

### Step 5 — Live Trading (CURRENT STATE)

**The system is already configured for live trading.** `trading_mode: "live"` is set in `config/settings.yaml` and the `--live` flag is required at startup.

| Parameter | Value | Notes |
|-----------|-------|-------|
| Bankroll | $30 | Live Kalshi account |
| Max position | $1.00 | Per market |
| Max exposure | $15.00 | 50% of bankroll |
| Daily loss limit | $3.00 | 10% of bankroll — halts trading for the day |
| Max contract price | $0.50 | Never pay more than this per contract |

To start:
```bash
python -m src.main start --live
```

After accumulating 100+ resolved markets, run `pre_live_checklist.py` and `calibration_report.py` to evaluate whether to increase position sizes. Do not raise `max_position_dollars` above $1 until metrics are confirmed.

---

## KNOWN THINGS THAT MAY NEED FIXING AFTER FIRST RUN

These are runtime issues that only reveal themselves when talking to live APIs. Claude Code can fix any of these in a new session — just describe the error.

1. **Kalshi SDK API changes**: If Kalshi updates their SDK or API, method names may change. Check `src/kalshi_client.py` and the SDK changelog.

2. **Open-Meteo model member count**: The system expects 139 members (GFS=30, ECMWF=50, ICON=39, GEM=20). If Open-Meteo changes their ensemble format again, `compute_threshold_probability()` in `weather_ensemble.py` will need updating. The log will show `total_members=N` — if N < 30, the scanner will skip the market.

3. **Kalshi ticker format changes**: All parsing logic is in `parse_threshold_from_ticker()` and `parse_measurement_date_from_ticker()` in `market_scanner.py`. If these return None for most markets and `filtered_parse_error` is high in scan logs, fix the regex there.

4. **No markets with edge**: Totally normal early on. The system is designed to skip rather than force trades. If you run for 2 weeks and see 0 approved trades, check: (a) are weather markets open and liquid on Kalshi right now? (b) is the ensemble spread too wide (low confidence)? (c) are edge thresholds too high for current market conditions?

5. **Telegram not sending**: Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. The daemon will still function; notifications are fire-and-forget and never block trading.

6. **Database grows large**: `data/trades.db` will grow slowly but that's fine. SQLite handles millions of rows without issue.

### Build Checklist — Phase 2
| Step | File | Status |
|------|------|--------|
| 1 | `src/edge_calculator.py` | ✅ DONE |
| 2 | `src/risk_manager.py` | ✅ DONE |
| 3 | `src/market_scanner.py` | ✅ DONE |
| 4 | `src/ai_reasoner.py` | ✅ DONE |
| 5 | `src/order_executor.py` | ✅ DONE |
| 6 | `src/tracker.py` | ✅ DONE |
| 7 | `src/calibration_engine.py` | ✅ DONE |
| 8 | `src/notifier.py` | ✅ DONE |
| 9 | `src/health_monitor.py` | ✅ DONE |
| 10 | `src/daemon.py` | ✅ DONE |
| 11 | `src/main.py` | ✅ DONE |
| 12 | 6 scripts | ✅ DONE |

### Test Suite
**521/521 tests passing.** Run with: `pytest tests/ -q`

### Credentials & Environment
- `.env` is fully populated: `TRADING_MODE=live`, live Kalshi credentials, Anthropic, Telegram
- **Live Kalshi API:** real-money account, $30 starting balance
- Demo credentials also present in `.env` (unused during live trading, kept for reference)
- Python 3.12 on Windows 11
- All dependencies installed via `pip install -r requirements.txt`
- `cryptography` package added to requirements.txt (missing from spec, required by kalshi-python)

### Known API Corrections (spec had these wrong — do NOT revert)
| Item | Spec said | Reality (verified live) |
|---|---|---|
| Open-Meteo ensemble base URL | `api.open-meteo.com/v1/ensemble` | `ensemble-api.open-meteo.com/v1/ensemble` |
| Member field name format | `temperature_2m_member_0` | `temperature_2m_member01_ecmwf_ifs025_ensemble` |
| Total ensemble members | ~143 | 139 (GFS=30, ECMWF=50, ICON=39, GEM=20) |
| Time string format | `2026-03-31T00:00:00` | `2026-03-31T00:00` (no seconds) |
| Kalshi ticker format | (unspecified) | `KXHIGHNY-26APR01-T79` (verified live) |
| Kalshi title format | (unspecified) | `"Will the **high temp in NYC** be >79° on Apr 1, 2026?"` |
| Kalshi `close_time` | (unspecified) | datetime object, day AFTER measurement date, ~05:00 UTC |

### All Files — Complete Inventory

#### Configuration (all complete)
| File | Notes |
|------|-------|
| `config/settings.yaml` | All tunable parameters; ensemble URL corrected to `ensemble-api.open-meteo.com` |
| `config/station_mapping.yaml` | 10 cities with NWS station IDs and lat/lon |
| `config/market_categories.yaml` | Whitelisted series tickers, exclusions |
| `requirements.txt` | Added `cryptography>=42.0.0` (missing from spec, required by kalshi-python) |
| `.env.example` | Template; real `.env` populated with live credentials |
| `.gitignore` | Excludes `.env`, `*.pem`, `data/*.db`, logs |

#### Source Code — COMPLETE (16 files)
| File | Key Contents |
|------|-------------|
| `src/__init__.py` | Package marker |
| `src/data_feeds/__init__.py` | Package marker |
| `src/data_feeds/base.py` | Abstract DataFeed base class |
| `src/kalshi_client.py` | SDK wrapper: TTL cache, retry/backoff, pagination, March 2026 price format; `get_series()` cached 10 min |
| `src/data_feeds/weather_ensemble.py` | Open-Meteo ensemble client; `EnsembleResult` dataclass; P(max ≥ threshold) computation |
| `src/data_feeds/weather_observations.py` | NOAA client: station coordinate validation + post-resolution ground truth |
| `src/probability_engine.py` | fetch→bias→probability pipeline; `BiasDatabase`; `MarketInfo`; batch `estimate_multiple()` |
| `src/edge_calculator.py` | `EdgeParams`, `EdgeAssessment`, `compute_edge()`; 3-scenario fee math; quarter-Kelly sizing; SANITY_CHECK at >15pp |
| `src/risk_manager.py` | `RiskLimits`, `RiskManager`; 11-point pre-trade checklist; stateful exposure/P&L; NO async |
| `src/market_scanner.py` | Full scan pipeline; ticker/title parsing; below-market probability flip; direction detection; AI wired |
| `src/ai_reasoner.py` | `AIParams`, `SanityCheckResult`, `AIReasoner`; veto-only; 50-call/day budget; fail-open on any error |
| `src/order_executor.py` | `OrderResult`, `OrderExecutor`; staleness guard (2¢); maker pricing; depth cap; 10-min stale cancellation |
| `src/tracker.py` | `TradeRecord`; `record_trade`, `update_fill`, `record_outcome`, `compute_brier_score`, `compute_daily_pnl`, `get_open_positions`; sqlite-utils; dual-arm |
| `src/calibration_engine.py` | `BiasDatabase` (per-station/month °F bias, lazy-load); `CalibrationEngine` (isotonic regression, temporal holdout, pickle persistence, auto-refit every 20 obs) |
| `src/notifier.py` | `NotifierConfig`, `Notifier`; async Telegram Bot API; 5 methods; fail-silent; `log_only` mode |
| `src/health_monitor.py` | `HealthMonitor`; 3-state circuit breakers (CLOSED→OPEN→HALF-OPEN→CLOSED); 3 failures trip, 5-min cooldown; `record_failure()` returns True on trip |
| `src/daemon.py` | `TradingDaemon`; full async pipeline; dual-arm eval; SIGINT/SIGTERM graceful shutdown; high-freq windows; fill sync; daily summary; circuit breaker integration; pause flag support |
| `src/main.py` | Click CLI: `start` (PID file, asyncio.run), `stop` (SIGTERM), `status` (DB metrics), `pause` (flag file), `resume` (remove flag) |

#### Source Code — COMPLETE (17 files, all built)
*(No source files remaining — all 17 src/ files complete)*

#### Tests — COMPLETE (12 files, 518 tests total)
| File | Count | Key coverage |
|------|-------|-------------|
| `tests/__init__.py` | — | Package marker |
| `tests/test_kalshi_client.py` | 34 | Cache, retry, pagination, price helpers |
| `tests/test_probability_engine.py` | 38 | Ensemble fetch, bias, P(max≥threshold) |
| `tests/test_edge_calculator.py` | 55 | Fee scenarios, Kelly sizing, SANITY_CHECK threshold |
| `tests/test_risk_manager.py` | 63 | All 11 checklist items, exposure tracking, daily P&L |
| `tests/test_market_scanner.py` | 82 | Ticker/date/direction parsing, below-market flip, full pipeline |
| `tests/test_ai_reasoner.py` | 36 | Budget, daily reset, fail-open fallbacks, markdown JSON, adjusted_probability |
| `tests/test_order_executor.py` | 39 | Staleness guard, maker pricing, empty-book, depth cap, stale cancellation |
| `tests/test_tracker.py` | 54 | Schema, record_trade, update_fill, record_outcome P&L, Brier score, daily P&L, open positions, datetime helpers |
| `tests/test_calibration_engine.py` | 39 | BiasDatabase CRUD, lazy-load, CalibrationEngine fit/calibrate/holdout/refit, cross-instance persistence |
| `tests/test_notifier.py` | 38 | log_only mode, Telegram HTTP mocking, fail-silent on errors/timeouts, message content |
| `tests/test_health_monitor.py` | 40 | All 3 state transitions, multi-service independence, cooldown timing, HALF-OPEN recovery, manual reset |

#### Tests — NOT YET BUILT
| File | For |
|------|-----|
| *(none — daemon.py and main.py do not require unit tests per spec)* | |

#### Scripts
| File | Status | Notes |
|------|--------|-------|
| `scripts/smoke_test.py` | COMPLETE | Validates all credentials + live API; run after any env change |
| `scripts/compute_station_bias.py` | NOT BUILT | pending |
| `scripts/collect_historical.py` | NOT BUILT | pending |
| `scripts/backtest.py` | NOT BUILT | pending |
| `scripts/calibration_report.py` | NOT BUILT | pending |
| `scripts/pre_live_checklist.py` | NOT BUILT | pending |
| `scripts/validate_stations.py` | NOT BUILT | pending |

### Key Design Decisions Made (not in original spec)
- Env/mode validation happens BEFORE the kalshi-python import in `_connect()` so tests run without the SDK installed
- `estimate_multiple()` takes the raw fetch result and passes it directly to computation (not cache lookup) so mocked fetches work correctly in tests
- `BiasDatabase` lazy-loads on first `.get_bias()` call and silently returns 0.0 if DB missing (no crash on fresh install)
- Windows terminal encoding: use ASCII strings not unicode symbols in any terminal output scripts
- `EdgeParams.from_settings()` reads `ai.sanity_check_edge_threshold` (not `edge.*`) since that's where it lives in settings.yaml
- `sanity_check_edge_threshold` added to EdgeParams (spec mentioned it in ai section, not edge section — consolidated)
- `RiskManager.check_trade()` evaluates ALL 11 checks without short-circuit so operators see every failing check at once
- Risk manager is stateful (tracks exposure + daily P&L) and has NO async/IO — pure Python, fully testable without mocks
- **`MarketInfo.resolution_date` comes from the ticker date segment, NOT `close_time.date()`** — verified live: `close_time` is the day AFTER the measurement date (e.g. ticker `KXHIGHNY-26APR01-T79` measures Apr 1, but `close_time = 2026-04-02 04:59:00+00:00`). Using `close_time.date()` would cause the probability engine to forecast the wrong day. `parse_measurement_date_from_ticker()` parses `26APR01` → `date(2026, 4, 1)`. `close_time` is only used for `hours_to_resolution` (when can we still trade).
- **Fee coefficient fetched from series `fee_multiplier` field** via `get_series()` (cached 10 min). Falls back to `0.07` if field absent. Scanner calls this once per series per cycle.
- **Threshold parsed from market ticker first, then title/subtitle** — ticker regex `[_-]T(\d+)` (e.g. `KXHIGHNY-26MAR31-T75` → 75.0), then title regex `(\d+)\s*[°]?\s*[Ff]`. Markets where both fail are skipped with a WARNING log. **If titles look different than expected, fix the regex in `_parse_threshold_from_text()` in `market_scanner.py` — it is the only place to change.**
- **AI reasoner is wired into the scanner** — `MarketScanner.__init__` accepts `ai_reasoner: AIReasoner | None = None`. When `ai_reasoner is None`, `SANITY_CHECK` recommendations pass through unchanged. When set, `SANITY_CHECK` triggers `await self.ai_reasoner.sanity_check(...)` and a `proceed=False` response overrides `risk.approved=False`. The `adjusted_probability` from Claude is logged only (veto-only, Option B). See FUTURE IMPROVEMENT notes in `src/ai_reasoner.py` and `src/market_scanner.py` for where to wire up repricing if desired later.
- **OrderExecutor mid-price uses only present book sides** — when `yes_bid=0` (empty buy side), mid is computed from `yes_ask` alone (not `(0 + ask)/2`). Both sides absent → mid falls back to `edge.market_price` so staleness guard never false-triggers on an empty book.
- **`scan()` returns ALL evaluated results** (approved + rejected) — caller filters on `result.risk.approved`. This supports the dual-arm experimentation log (spec section 3.5): the daemon logs both arms' assessments for every scan.
- **Kalshi market direction verified live** — titles use `>79°` (above) or `<72°` (below). `parse_direction_from_text()` detects `<` before a digit → "below". For "below" markets, P(YES) = 1 − P(max ≥ threshold). The scanner stores the raw unflipped ensemble in `ScanResult.ensemble` (for calibration) and passes the flipped probability to `compute_edge()`. `copy.copy()` is used instead of `dataclasses.replace()` so that test mocks also work.
- **Kalshi `close_time` is a datetime object** (not a string) from the SDK — verified live format: `datetime(2026, 4, 2, 4, 59, tzinfo=utc)`. `parse_close_time()` handles both datetime objects and all ISO8601 string variants including space separators.
- **Known API corrections verified live 2026-03-31:** ticker format `KXHIGHNY-26APR01-T79`; title format `"Will the **high temp in NYC** be >79° on Apr 1, 2026?"`; subtitle `"80° or above"`; close_time is day-after measurement at ~05:00 UTC.

---

## INSTRUCTIONS FOR CLAUDE CODE

**Read this section before writing any code. It defines how you should work with the developer.**

### How to Interact with the Developer

You are building a real trading system that will eventually handle real money. The developer is not a professional software engineer — they understand the strategy and the markets but may not know Python idioms, async patterns, or deployment details. Your job is to be their technical partner, not a silent code generator.

**Ask questions when you encounter ambiguity.** If the spec says something vague or you see two reasonable approaches, stop and ask. For example: "The spec says to cache ensemble data for 15 minutes, but I could also cache per-station instead of globally — which would you prefer?" Don't guess. The wrong guess could affect real money.

**Explain your reasoning when making design decisions.** When you choose a library, a data structure, or an architectural pattern, briefly explain why. "I'm using `aiosqlite` instead of `sqlite-utils` here because the daemon loop is async and synchronous database calls would block it."

**Think like these experts when making decisions:**
- **Kalshi market expert:** Understand that this is a thin, retail-dominated market where execution quality matters. Every order placement decision should consider whether it achieves maker status (zero fees) or crosses the spread (pays fees).
- **Weather forecasting expert:** The ensemble probability is only as good as the station mapping and bias correction. When implementing the probability engine, validate that the coordinates actually correspond to the Kalshi settlement station. A 10-mile error in coordinates can mean a 3-5°F forecast error near coastlines or elevation changes.
- **Statistician:** Sample sizes matter. Don't let the system draw conclusions from 15 data points. Every calibration update, bias correction, and parameter change should have an explicit minimum N before it takes effect.
- **Behavioral scientist:** The system's edge comes from OTHER people's biases. When implementing the scanner and edge calculator, remember that the market price reflects retail participants who anchor to yesterday's weather, overweight media narratives, and reprice slowly after model updates. The system's advantage is speed and objectivity, not cleverness.
- **Risk manager who has seen systems blow up:** Every trade must pass every check. No shortcuts, no "just this once" overrides. The daily loss limit, position caps, and minimum edge thresholds are not suggestions — they are hard stops that prevent catastrophic losses.

**Flag potential issues proactively.** If you notice that something in the spec could cause a problem the developer hasn't considered — an edge case, a race condition, a data format mismatch — say so. "I notice the order executor doesn't handle the case where the orderbook is completely empty. Should I add a guard for that?"

**Test as you build.** Write unit tests for every module. The developer should be able to run `pytest` at any point and see what's working and what's not. Don't save testing for the end.

---

## PREAMBLE

### What This System Does

This is an autonomous AI trading agent that exploits weather forecast data to trade temperature threshold markets on Kalshi, the CFTC-regulated prediction market exchange. It uses physics-based ensemble weather models (not opinions or sentiment) to compute probabilities more accurately than retail market participants, then places limit orders to capture the mispricing.

### Scope

- **Primary focus: Weather temperature threshold markets ONLY** (e.g., "Will NYC's high exceed 85°F on July 15?")
- Precipitation markets are secondary (Phase 2, after temperature is validated)
- All other categories (economics, financial, sports, political) are explicitly excluded from v2.0
- The architecture supports adding categories later without refactoring

### Capital & Risk Profile

- Starting capital: Under $1,000 (proof of concept)
- Default mode: Paper/demo trading. Real money requires BOTH `TRADING_MODE=live` in environment AND `--live` flag at runtime.
- Per-market position cap: $50 maximum (5% of $1,000)
- Total open exposure cap: $250 maximum (25% of $1,000)
- Goal: Validate the system over 200+ resolved weather markets before risking real money

### Key Design Decisions

1. **Official Kalshi Python SDK** (`kalshi-python`) for authentication and basic API calls, wrapped in a custom layer for retry logic, caching, and logging
2. **Open-Meteo Ensemble API** as the primary probability engine — the ensemble IS the probability estimate, not Claude
3. **Claude API (Sonnet)** used sparingly: sanity-checking ensemble estimates, parsing settlement criteria, failure analysis — NOT for weather probability estimation
4. **Zero maker fees** are the target: the system uses limit orders exclusively, aiming for maker fills where Kalshi charges $0 in fees
5. **Free-tier data sources only** — no paid API subscriptions

---

## PART 0: WHERE THE EDGE COMES FROM

### The Physics-Based Edge

Kalshi weather markets reference specific NWS observation stations (e.g., Central Park for NYC). The market question is: "Will the maximum temperature recorded at [station] on [date] exceed [threshold]°F?"

The Open-Meteo Ensemble API provides 200+ individual weather forecasts from multiple numerical weather models (GFS: 31 members, ECMWF: 51 members, ICON: 40 members, GEM: 21 members, and others). Each member is a full atmospheric simulation with slightly different initial conditions. If 42 out of 51 ECMWF members predict a max temperature above the threshold, the physics-based probability is 82.4%. This is not an opinion — it's a statistical summary of fluid dynamics simulations.

### The Behavioral Edge

Kalshi's weather market participants are overwhelmingly retail users. Documented biases:

1. **Anchoring to yesterday's temperature.** If yesterday was 92°F, participants anchor to ~92°F for tomorrow even when the latest model run shows a cold front dropping temps to 78°F.
2. **Slow repricing after model updates.** Weather models update every 6 hours (00Z, 06Z, 12Z, 18Z). Model output becomes available ~3.5 hours after initialization. Kalshi weather markets typically take 1-4 hours AFTER model availability to fully reprice. Your system acts within 30 minutes.
3. **Favorite-longshot bias.** Documented in Kalshi academic research (Whelan, 2024): contracts below 20¢ (longshots) lose more often than implied, contracts above 80¢ (favorites) win more often than implied. For temperature markets, this means "Will it exceed 100°F?" contracts at 15¢ are consistently overpriced when models say the probability is really 8%.
4. **Availability bias on extreme weather.** Media coverage of heat waves and cold snaps makes extreme temperature events feel more likely than they are, inflating contract prices for extreme thresholds.

### The Fee Structure Edge

Kalshi charges ZERO fees on maker orders (limit orders that rest on the book and get filled by a taker). The system exclusively uses limit orders. If the system achieves a high maker fill rate (target: >70%), the effective fee is near zero, meaning even small edges (3-4 percentage points) are profitable.

For the cases where a taker fee applies, the formula is:
```
taker_fee = ceil(coefficient * num_contracts * price * (1 - price) * 100) / 100
```
The coefficient is typically 0.07 but varies by market series. The fee peaks at P=0.50 and approaches zero at P=0.01 or P=0.99. Fetch the actual coefficient from the series `fee_type` and `fee_multiplier` fields.

---

## PART 1: SYSTEM ARCHITECTURE

### 1.0 Project Structure

```
kalshi-trader/
├── config/
│   ├── settings.yaml              # All configurable parameters
│   ├── station_mapping.yaml       # Kalshi settlement stations → lat/lon coordinates
│   └── market_categories.yaml     # Market type definitions (weather subtypes)
├── src/
│   ├── __init__.py
│   ├── main.py                    # Entry point, CLI commands
│   ├── kalshi_client.py           # SDK wrapper: retry, caching, logging over kalshi-python
│   ├── market_scanner.py          # Weather market discovery and filtering
│   ├── data_feeds/
│   │   ├── __init__.py
│   │   ├── base.py                # Abstract base class for all feeds
│   │   ├── weather_ensemble.py    # Open-Meteo Ensemble API (PRIMARY probability engine)
│   │   ├── weather_observations.py # NOAA Weather API (settlement verification)
│   │   └── weather_historical.py  # Open-Meteo Historical Forecast API (bias correction)
│   ├── probability_engine.py      # Ensemble → probability computation (NO AI needed)
│   ├── ai_reasoner.py             # Claude API for sanity checks and edge cases only
│   ├── calibration_engine.py      # Isotonic regression, bias correction, confidence model
│   ├── edge_calculator.py         # Fair value vs market price, fee-aware
│   ├── risk_manager.py            # Position limits, daily loss, exposure caps
│   ├── order_executor.py          # Limit order placement, monitoring, maker-fill optimization
│   ├── tracker.py                 # Performance logging, Brier scores, P&L
│   ├── daemon.py                  # Autonomous async loop, signal handling
│   ├── health_monitor.py          # Circuit breakers, health checks
│   └── notifier.py                # Telegram/webhook notifications
├── data/
│   ├── trades.db                  # SQLite: all trades, fills, outcomes
│   ├── calibration.db             # SQLite: probability estimates vs outcomes
│   ├── station_bias.db            # SQLite: per-station, per-model bias corrections
│   ├── market_snapshots/          # Historical market states for backtesting
│   └── reasoning_logs/            # AI reasoning chains (JSON)
├── scripts/
│   ├── setup.sh                   # Install deps, validate environment
│   ├── collect_historical.py      # Scrape Kalshi historical weather markets
│   ├── compute_station_bias.py    # Compute per-station model bias corrections
│   ├── backtest.py                # Run historical backtests
│   ├── calibration_report.py      # Generate calibration analysis
│   └── pre_live_checklist.py      # Automated readiness check
├── tests/
│   ├── test_kalshi_client.py
│   ├── test_probability_engine.py
│   ├── test_edge_calculator.py
│   ├── test_risk_manager.py
│   └── test_order_executor.py
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
└── README.md
```

### 1.1 Kalshi API Client (`kalshi_client.py`)

**Foundation: Official SDK**

```bash
pip install kalshi-python
```

The official `kalshi-python` SDK (v2.1.4+) handles RSA-PSS authentication internally. Do NOT implement auth from scratch.

```python
from kalshi_python import Configuration, KalshiClient

def create_kalshi_client(mode: str = "demo") -> KalshiClient:
    """Create an authenticated Kalshi client."""
    urls = {
        "demo": "https://demo-api.kalshi.co/trade-api/v2",
        "live": "https://api.elections.kalshi.com/trade-api/v2",
    }
    
    config = Configuration(host=urls[mode])
    
    key_id_var = f"KALSHI_{mode.upper()}_API_KEY_ID"
    key_path_var = f"KALSHI_{mode.upper()}_PRIVATE_KEY_PATH"
    
    config.api_key_id = os.environ[key_id_var]
    with open(os.environ[key_path_var], "r") as f:
        config.private_key_pem = f.read()
    
    return KalshiClient(config)
```

**Custom Wrapper Layer:** Build a thin wrapper around the SDK client that adds:
- Exponential backoff with jitter on 429 (rate limit) and 5xx errors
- Request-level logging (method, path, status, latency)
- Response caching with configurable TTL (market data: 30s, portfolio: 5s)
- Automatic pagination for `get_markets()` (Kalshi has 85,000+ markets, returns max 1,000 per page)

**CRITICAL — March 2026 API Changes:**

As of March 12, 2026, Kalshi removed legacy integer fields. All code must use:
- Price fields: `yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars` (strings like `"0.5600"`)
- Quantity fields: `volume_fp`, `open_interest_fp`, `count_fp` (strings like `"10.00"`)
- Convert to float for calculations: `float(market.yes_bid_dollars)`

Some markets now support fractional trading (`fractional_trading_enabled: true`) with sub-integer quantities and subpenny pricing at the tails. The system must check this flag and handle both formats.

**Required API Operations:**
- `GET /markets` — list markets with filters (series_ticker, status=open, cursor pagination)
- `GET /markets/{ticker}` — single market details
- `GET /markets/{ticker}/orderbook` — orderbook (returns YES bids and NO bids; a YES bid at 60¢ = NO ask at 40¢)
- `GET /series/{series_ticker}` — series info including `settlement_sources` and `fee_type`/`fee_multiplier`
- `POST /portfolio/orders` — place limit order
- `DELETE /portfolio/orders/{order_id}` — cancel order
- `GET /portfolio/orders` — list open orders (includes fill info)
- `GET /portfolio/positions` — current positions
- `GET /portfolio/balance` — available balance and portfolio value
- `GET /historical/markets` — archived settled markets (for backtesting)

**WebSocket** (Phase 2): `wss://api.elections.kalshi.com/trade-api/ws/v2` for real-time orderbook updates. Not needed for MVP — polling every 60s is sufficient for weather markets that resolve daily.

### 1.2 Market Scanner (`market_scanner.py`)

**Scope: Weather temperature markets only.** Filter by series tickers matching weather patterns.

Known Kalshi weather series tickers (verify and update as Kalshi adds new ones):
```yaml
# config/market_categories.yaml
weather_series:
  temperature:
    - "KXHIGHNY"    # Highest temp in NYC (Central Park)
    - "KXHIGHCHI"   # Highest temp in Chicago
    - "KXHIGHLAX"   # Highest temp in Los Angeles
    - "KXHIGHDC"    # Highest temp in DC
    - "KXHIGHHOU"   # Highest temp in Houston
    - "KXHIGHMIA"   # Highest temp in Miami
    - "KXHIGHDEN"   # Highest temp in Denver
    - "KXHIGHPHX"   # Highest temp in Phoenix
    - "KXHIGHSEA"   # Highest temp in Seattle
    - "KXHIGHATL"   # Highest temp in Atlanta
    # Add more as discovered — the scanner should also search for "KXHIGH*" and "KXLOW*"
  precipitation:
    - "KXRAIN*"     # Phase 2
    - "KXSNOW*"     # Phase 2
```

**Scanner Pipeline (simplified for weather-only):**

```
1. GET /markets?series_ticker=KXHIGHNY&status=open (repeat for each series)
   → Returns all open temperature threshold markets for each city

2. For each market: check liquidity
   → volume_fp > 50 AND spread < 8¢ → passes

3. For each passing market: check time to resolution
   → Resolving in 6 hours to 7 days → passes (sweet spot for forecast accuracy)

4. For each passing market: get ensemble probability estimate
   → This is FREE (Open-Meteo API call, no AI cost)

5. Compare ensemble probability to market mid-price
   → If |ensemble_prob - market_price| > minimum_edge → flag for trading

6. ONLY if edge exists: optionally call Claude for sanity check
   → "The ensemble says 82%, the market says 65%. Any reason the ensemble could be wrong?"
```

This pipeline evaluates potentially hundreds of weather markets per cycle while spending $0 on AI for the vast majority that have no edge.

### 1.3 Weather Data Feeds (`data_feeds/`)

#### 1.3.1 Primary: Open-Meteo Ensemble API (`weather_ensemble.py`)

**URL:** `https://api.open-meteo.com/v1/ensemble`
**Cost:** Free for non-commercial use (our paper trading phase). Professional plan (~$29/month) required for commercial use at scale.
**Rate limits:** Fair use policy. Don't hammer it — cache responses and poll no more than every 15 minutes per location.

```python
import httpx
from dataclasses import dataclass
from datetime import datetime, date

@dataclass
class EnsembleResult:
    """Result of ensemble probability computation for a single market."""
    market_ticker: str
    target_date: date
    threshold_f: float
    station_lat: float
    station_lon: float
    
    # Core probability estimate
    probability: float              # P(max temp >= threshold)
    total_members: int              # how many ensemble members contributed
    members_exceeding: int          # how many predicted above threshold
    
    # Confidence metrics
    ensemble_mean: float            # mean of all member max-temp predictions
    ensemble_spread: float          # max - min across members
    confidence_interval_90: tuple   # (5th percentile, 95th percentile)
    
    # Metadata
    models_used: list[str]          # which models contributed
    freshness_minutes: int          # age of the model run
    bias_correction_applied: float  # per-station correction (from station_bias.db)
    
    @property
    def confidence(self) -> str:
        """Heuristic confidence based on ensemble agreement."""
        if self.ensemble_spread < 6:
            return "high"
        elif self.ensemble_spread < 12:
            return "medium"
        else:
            return "low"

class OpenMeteoEnsembleClient:
    BASE_URL = "https://api.open-meteo.com/v1/ensemble"
    
    # Models to request — verify these names against Open-Meteo docs on startup
    DEFAULT_MODELS = [
        "gfs_seamless",          # NOAA GFS: 31 members, global
        "ecmwf_ifs025",          # ECMWF: 51 members, global
        "icon_seamless",         # DWD ICON: 40 members, Europe (good for East Coast US too)
        "gem_global",            # Canadian GEM: 21 members, global
    ]
    
    async def get_ensemble_forecast(
        self, lat: float, lon: float, models: list[str] | None = None
    ) -> dict:
        """Fetch ensemble forecast for a location."""
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "models": ",".join(models or self.DEFAULT_MODELS),
            "forecast_days": 7,
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            return response.json()
    
    def compute_threshold_probability(
        self,
        ensemble_data: dict,
        target_date: date,
        threshold_f: float,
        bias_correction: float = 0.0,
    ) -> EnsembleResult:
        """
        THE CORE PROBABILITY ENGINE.
        
        Count ensemble members exceeding the threshold on the target date.
        This replaces Claude API calls for weather probability estimation.
        
        Args:
            ensemble_data: raw response from Open-Meteo ensemble API
            target_date: the date the Kalshi market resolves
            threshold_f: the temperature threshold in °F
            bias_correction: per-station adjustment (positive = model runs hot)
        """
        member_daily_maxes = []
        
        # Parse the response — structure varies by model
        # Each model returns hourly data for member_0, member_1, ..., member_N
        times = ensemble_data.get("hourly", {}).get("time", [])
        
        for key, values in ensemble_data.get("hourly", {}).items():
            if key == "time":
                continue
            if "temperature_2m_member" not in key:
                continue
            
            # Extract daily max for the target date
            daily_max = None
            for i, t in enumerate(times):
                if t.startswith(target_date.isoformat()):
                    temp = values[i]
                    if temp is not None:
                        if daily_max is None or temp > daily_max:
                            daily_max = temp
            
            if daily_max is not None:
                # Apply station-specific bias correction
                corrected_max = daily_max - bias_correction
                member_daily_maxes.append(corrected_max)
        
        if not member_daily_maxes:
            return EnsembleResult(
                market_ticker="", target_date=target_date,
                threshold_f=threshold_f, station_lat=0, station_lon=0,
                probability=0.5, total_members=0, members_exceeding=0,
                ensemble_mean=0, ensemble_spread=0,
                confidence_interval_90=(0, 0), models_used=[],
                freshness_minutes=999, bias_correction_applied=0,
            )
        
        exceeding = sum(1 for t in member_daily_maxes if t >= threshold_f)
        total = len(member_daily_maxes)
        
        sorted_maxes = sorted(member_daily_maxes)
        p5 = sorted_maxes[max(0, int(0.05 * total))]
        p95 = sorted_maxes[min(total - 1, int(0.95 * total))]
        
        return EnsembleResult(
            market_ticker="",  # filled by caller
            target_date=target_date,
            threshold_f=threshold_f,
            station_lat=0,  # filled by caller
            station_lon=0,
            probability=exceeding / total,
            total_members=total,
            members_exceeding=exceeding,
            ensemble_mean=sum(member_daily_maxes) / total,
            ensemble_spread=max(member_daily_maxes) - min(member_daily_maxes),
            confidence_interval_90=(p5, p95),
            models_used=self.DEFAULT_MODELS,
            freshness_minutes=0,  # computed by caller from model run time
            bias_correction_applied=bias_correction,
        )
```

#### 1.3.2 Settlement Verification: NOAA Weather API (`weather_observations.py`)

**URL:** `https://api.weather.gov` (FREE, no key)

Used for TWO purposes:
1. **Station mapping:** Parse Kalshi's `settlement_sources` to find the exact NWS station, then get its coordinates for the ensemble query
2. **Post-resolution verification:** After a market settles, pull the actual observation to record ground truth for calibration

```python
class NOAAObservationClient:
    BASE_URL = "https://api.weather.gov"
    
    async def get_station_info(self, station_id: str) -> dict:
        """Get station metadata including lat/lon."""
        url = f"{self.BASE_URL}/stations/{station_id}"
        headers = {"User-Agent": "KalshiWeatherTrader/1.0 (contact@example.com)"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            coords = data["geometry"]["coordinates"]
            return {
                "station_id": station_id,
                "name": data["properties"]["name"],
                "longitude": coords[0],
                "latitude": coords[1],
            }
    
    async def get_actual_observation(self, station_id: str, date: str) -> dict:
        """Get actual recorded temperature for calibration."""
        url = f"{self.BASE_URL}/stations/{station_id}/observations"
        params = {"start": f"{date}T00:00:00Z", "end": f"{date}T23:59:59Z"}
        headers = {"User-Agent": "KalshiWeatherTrader/1.0 (contact@example.com)"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            observations = resp.json()["features"]
            temps = [
                obs["properties"]["maxTemperatureLast24Hours"]["value"]
                for obs in observations
                if obs["properties"].get("maxTemperatureLast24Hours", {}).get("value") is not None
            ]
            if temps:
                # Convert Celsius to Fahrenheit
                max_temp_c = max(temps)
                max_temp_f = max_temp_c * 9 / 5 + 32
                return {"max_temp_f": max_temp_f, "station_id": station_id, "date": date}
            return None
```

#### 1.3.3 Historical Bias Correction (`weather_historical.py`)

**URL:** `https://api.open-meteo.com/v1/forecast` with `start_date` and `end_date` params for historical data.

Also: Open-Meteo Previous Runs API at `https://previous-runs-api.open-meteo.com/v1/forecast` for archived model forecasts.

**Purpose:** Compute per-station, per-model, per-season bias corrections. Example: "In July, GFS overpredicts Central Park daily highs by 1.8°F on average." Store these in `station_bias.db` and apply them to ensemble members before computing probabilities.

Run `scripts/compute_station_bias.py` once during setup, then update monthly.

### 1.4 Station Mapping (`config/station_mapping.yaml`)

**This is critical and easy to get wrong.** Each Kalshi weather series settles based on a specific NWS observation station. The ensemble forecast must query the correct coordinates.

```yaml
# config/station_mapping.yaml
# Mapping: Kalshi series ticker → NWS station → coordinates
# Verify each mapping by checking the series settlement_sources on Kalshi API

stations:
  KXHIGHNY:
    station_id: "KNYC"       # Central Park, NY
    latitude: 40.7789
    longitude: -73.9692
    city: "New York"
  KXHIGHCHI:
    station_id: "KORD"       # O'Hare International, Chicago
    latitude: 41.9742
    longitude: -87.9073
    city: "Chicago"
  KXHIGHLAX:
    station_id: "KCQT"       # Downtown LA / USC campus
    latitude: 34.0236
    longitude: -118.2912
    city: "Los Angeles"
  KXHIGHDC:
    station_id: "KDCA"       # Reagan National Airport
    latitude: 38.8512
    longitude: -77.0402
    city: "Washington DC"
  # ADD MORE AS DISCOVERED
  # CRITICAL: Verify each station_id against the Kalshi series settlement_sources
  # Use AI (Claude) to parse the settlement_sources text ONCE per series, then cache here
```

**Startup validation:** On first run, for each configured station, call `GET /series/{ticker}` from Kalshi, extract `settlement_sources`, and verify the station mapping matches. Log a WARNING if there's a discrepancy.

### 1.5 Probability Engine (`probability_engine.py`)

This is the heart of the system. It does NOT use AI. It is deterministic math on ensemble data.

```python
class ProbabilityEngine:
    """
    Computes market probabilities from ensemble weather data.
    No AI, no API costs, no latency beyond the Open-Meteo call.
    """
    
    def __init__(self, ensemble_client, bias_db):
        self.ensemble = ensemble_client
        self.bias_db = bias_db
    
    async def estimate(self, market, station_config: dict) -> EnsembleResult:
        """
        Full probability estimation pipeline for a weather market.
        
        1. Fetch ensemble forecast for the station coordinates
        2. Apply per-station bias correction
        3. Count members exceeding threshold
        4. Return probability estimate
        """
        # 1. Get the ensemble data
        raw_data = await self.ensemble.get_ensemble_forecast(
            lat=station_config["latitude"],
            lon=station_config["longitude"],
        )
        
        # 2. Look up bias correction for this station
        bias = self.bias_db.get_bias(
            station_id=station_config["station_id"],
            month=market.resolution_date.month,
        )
        
        # 3. Compute probability
        result = self.ensemble.compute_threshold_probability(
            ensemble_data=raw_data,
            target_date=market.resolution_date,
            threshold_f=market.threshold_value,
            bias_correction=bias or 0.0,
        )
        
        result.market_ticker = market.ticker
        result.station_lat = station_config["latitude"]
        result.station_lon = station_config["longitude"]
        
        return result
```

### 1.6 AI Reasoner (`ai_reasoner.py`) — Used Sparingly

Claude is called ONLY in these situations:

1. **Sanity check on large edges (>15pp).** If the ensemble says 85% and the market says 60%, that's a 25pp edge — suspiciously large. Ask Claude: "Is there any reason the ensemble could be systematically wrong for this specific market?"

2. **Parsing settlement criteria for new series.** When the scanner discovers a new weather series, use Claude ONCE to parse the settlement rules and extract the station ID, measurement type, and threshold format. Cache forever.

3. **Post-failure analysis.** After a batch of mispredictions, use Claude to classify failure modes (see calibration section).

**Claude is NEVER called for routine probability estimation.** The ensemble handles that.

```python
class AIReasoner:
    """Thin wrapper around Claude API. Called rarely."""
    
    def __init__(self, api_key: str):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-20250514"
        self.call_count_today = 0
        self.max_calls_per_day = 50  # hard cost control
    
    async def sanity_check(
        self, market_description: str, ensemble_prob: float,
        market_price: float, ensemble_details: str
    ) -> dict:
        """
        Called only when edge > 15pp. Ask Claude if there's a reason
        the ensemble could be wrong.
        """
        if self.call_count_today >= self.max_calls_per_day:
            return {"proceed": True, "reason": "AI call budget exhausted, proceeding on ensemble alone"}
        
        self.call_count_today += 1
        
        response = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            temperature=0.1,
            messages=[{
                "role": "user",
                "content": f"""You are reviewing a weather prediction market trade.

Market: {market_description}
Ensemble probability (from {ensemble_details}): {ensemble_prob:.1%}
Current Kalshi market price: {market_price:.1%}
Edge: {abs(ensemble_prob - market_price):.1%}

This is a large edge. Before we trade, is there any reason the ensemble forecast 
could be systematically wrong for THIS specific market? Consider:
- Urban heat island effects at the settlement station
- Known model biases for this region/season
- Upcoming weather events the ensemble might handle poorly (e.g., boundary of a front)
- Settlement criteria nuances

Return ONLY JSON:
{{
    "proceed": true | false,
    "concern": "null or specific concern",
    "adjusted_probability": {ensemble_prob} or adjusted value if you have a specific reason
}}

Default to proceed=true. Only flag false if you have a SPECIFIC, CONCRETE concern."""
            }]
        )
        
        import json
        try:
            return json.loads(response.content[0].text)
        except (json.JSONDecodeError, IndexError):
            return {"proceed": True, "reason": "Failed to parse AI response, proceeding on ensemble"}
```

### 1.7 Edge Calculator (`edge_calculator.py`)

```python
import math
from dataclasses import dataclass

@dataclass
class EdgeAssessment:
    market_ticker: str
    market_price: float          # current mid-price (0-1 scale, from dollars string)
    fair_value: float            # our ensemble probability
    raw_edge: float              # |fair_value - market_price|
    direction: str               # "BUY_YES" or "BUY_NO"
    
    # Fee-aware calculations (three scenarios per section 5.1)
    taker_fee: float             # fee if we get a taker fill on entry
    maker_fee: float             # fee if we get a maker fill (should be 0)
    early_exit_fee: float        # fee if we sell before settlement (taker exit)
    edge_after_taker_fee: float  # raw_edge - taker_fee (likely case: hold to settlement)
    edge_after_maker_fee: float  # raw_edge - maker_fee (best case: maker fill, hold to settlement)
    edge_after_round_trip: float # raw_edge - taker_entry - taker_exit (worst case: early exit)
    
    # Sizing
    kelly_fraction: float        # quarter-Kelly position size
    recommended_contracts: int   # actual number of contracts to buy
    recommended_dollars: float   # dollar cost of the position
    
    # Decision
    recommendation: str          # "TRADE", "NO_TRADE", "SANITY_CHECK"
    skip_reason: str | None      # why we're not trading, if applicable


def compute_edge(
    market_price: float,
    ensemble_prob: float,
    ensemble_confidence: str,
    fee_coefficient: float,
    portfolio_balance: float,
    max_position_dollars: float,
) -> EdgeAssessment:
    """
    Compute the fee-adjusted edge and Kelly-optimal position size.
    """
    raw_edge = abs(ensemble_prob - market_price)
    direction = "BUY_YES" if ensemble_prob > market_price else "BUY_NO"
    
    # The price we'd pay
    entry_price = market_price if direction == "BUY_YES" else (1.0 - market_price)
    
    # Fee calculations
    taker_fee_per_contract = math.ceil(fee_coefficient * 1 * entry_price * (1 - entry_price) * 100) / 100
    maker_fee_per_contract = 0.0  # Kalshi charges zero fees on maker orders
    
    edge_after_taker = raw_edge - taker_fee_per_contract
    edge_after_maker = raw_edge - maker_fee_per_contract  # = raw_edge
    
    # Kelly sizing (use maker edge since we target maker fills, but validate with taker edge)
    # Only trade if profitable even with taker fill (conservative)
    if edge_after_taker <= 0:
        return EdgeAssessment(
            market_ticker="", market_price=market_price, fair_value=ensemble_prob,
            raw_edge=raw_edge, direction=direction,
            taker_fee=taker_fee_per_contract, maker_fee=0,
            edge_after_taker_fee=edge_after_taker, edge_after_maker_fee=edge_after_maker,
            kelly_fraction=0, recommended_contracts=0, recommended_dollars=0,
            recommendation="NO_TRADE", skip_reason="Edge does not survive taker fee"
        )
    
    # Kelly for binary outcome: f = edge / (1 - entry_price)
    # Guard against division by zero at extreme prices
    if entry_price <= 0.02 or entry_price >= 0.98:
        return EdgeAssessment(
            market_ticker="", market_price=market_price, fair_value=ensemble_prob,
            raw_edge=raw_edge, direction=direction,
            taker_fee=taker_fee_per_contract, maker_fee=0,
            edge_after_taker_fee=edge_after_taker, edge_after_maker_fee=edge_after_maker,
            kelly_fraction=0, recommended_contracts=0, recommended_dollars=0,
            recommendation="NO_TRADE", skip_reason="Price too extreme for safe Kelly sizing"
        )
    
    full_kelly = edge_after_maker / (1.0 - entry_price)
    quarter_kelly = full_kelly * 0.25  # conservative fraction
    
    # Position size in dollars
    position_dollars = min(
        portfolio_balance * quarter_kelly,
        max_position_dollars,  # per-market cap
    )
    
    # Convert to contract count
    recommended_contracts = max(1, int(position_dollars / entry_price))
    actual_dollars = recommended_contracts * entry_price
    
    # Minimum edge threshold: the HIGHER of a fixed floor OR 3x the transaction cost
    # This adapts to contract price — mid-priced contracts need more edge, extreme-priced need less
    fixed_floor = 0.04 if ensemble_confidence == "high" else 0.06 if ensemble_confidence == "medium" else 0.10
    cost_based_min = taker_fee_per_contract * 3  # must exceed 3x the taker fee
    min_edge = max(fixed_floor, cost_based_min)
    
    # Assumed slippage: deduct 1 cent from the edge to account for not getting the mid-price
    # This is conservative — actual slippage may be less, but we'd rather skip marginal trades
    assumed_slippage = 0.01
    edge_after_slippage = raw_edge - assumed_slippage
    
    if edge_after_slippage < min_edge:
        recommendation = "NO_TRADE"
        skip_reason = f"Edge {raw_edge:.1%} minus {assumed_slippage:.0%} slippage = {edge_after_slippage:.1%}, below minimum {min_edge:.1%}"
    elif raw_edge > 0.15:
        recommendation = "SANITY_CHECK"  # large edge, verify with AI
        skip_reason = None
    else:
        recommendation = "TRADE"
        skip_reason = None
    
    return EdgeAssessment(
        market_ticker="", market_price=market_price, fair_value=ensemble_prob,
        raw_edge=raw_edge, direction=direction,
        taker_fee=taker_fee_per_contract, maker_fee=0,
        early_exit_fee=taker_fee_per_contract,  # same formula, applied at exit price
        edge_after_taker_fee=edge_after_taker, edge_after_maker_fee=edge_after_maker,
        edge_after_round_trip=raw_edge - (2 * taker_fee_per_contract),
        kelly_fraction=quarter_kelly, recommended_contracts=recommended_contracts,
        recommended_dollars=actual_dollars,
        recommendation=recommendation, skip_reason=skip_reason
    )
```

### 1.8 Risk Manager (`risk_manager.py`)

**Hard Limits for $1,000 Portfolio:**

| Parameter | Value | Rationale |
|---|---|---|
| `max_position_dollars` | $50 | 5% of portfolio per market |
| `max_total_exposure_dollars` | $250 | 25% total open positions |
| `max_daily_loss_dollars` | $30 | 3% daily loss → stop trading |
| `min_edge` | 0.04 | 4pp minimum (at high confidence) |
| `min_ensemble_members` | 30 | Don't trade if too few members contributed |
| `min_confidence` | "medium" | Skip "low" confidence (wide ensemble spread) |
| `min_volume_fp` | 5 | Minimum contracts traded (lowered from 50 — Kalshi weather markets are thin) |
| `max_spread_dollars` | 0.15 | Max $0.15 bid-ask spread (raised from 0.08 — weather markets have wider spreads) |
| `min_hours_to_resolution` | 6 | Don't enter markets resolving in <6 hours |
| `max_days_to_resolution` | 7 | Focus on 1-7 day forecasts (highest accuracy) |
| `kalshi_position_limit` | $25,000 | Kalshi's own per-market limit (won't hit at $1k portfolio) |

**Pre-Trade Checklist (ALL must pass):**
1. Position size within per-market dollar limit
2. Total exposure within portfolio limit
3. Daily loss limit not breached
4. Edge above minimum for confidence level
5. Edge survives taker fee (conservative: assume we might get taker fill)
6. Ensemble has sufficient members (>30)
7. Ensemble confidence is medium or high
8. Market has sufficient volume
9. Spread below maximum
10. Time to resolution within 6 hours - 7 days
11. Market status is "open" (not closed, settled, or unopened)

### 1.9 Order Executor (`order_executor.py`)

**Strategy: Always limit orders, never market orders. Target maker fills (zero fees).**

```python
class OrderExecutor:
    async def place_order(self, edge: EdgeAssessment, market) -> dict:
        """
        Place a limit order designed to rest on the book (maker fill).
        """
        # Re-fetch orderbook immediately before ordering (staleness guard)
        book = await self.client.get_orderbook(market.ticker)
        
        if edge.direction == "BUY_YES":
            best_bid = float(book.yes_bid_dollars) if book.yes_bid_dollars else 0
            best_bid_size = float(book.yes_bid_size_fp) if getattr(book, 'yes_bid_size_fp', None) else 999
            order_price = best_bid if best_bid > 0 else round(edge.fair_value - 0.02, 2)
        else:
            best_no_bid = float(book.no_bid_dollars) if book.no_bid_dollars else 0
            best_bid_size = float(book.no_bid_size_fp) if getattr(book, 'no_bid_size_fp', None) else 999
            order_price = best_no_bid if best_no_bid > 0 else round((1 - edge.fair_value) - 0.02, 2)
        
        # Staleness guard: re-check that the edge still exists at current prices
        current_mid = (float(book.yes_bid_dollars or 0) + float(book.yes_ask_dollars or 1)) / 2
        current_edge = abs(edge.fair_value - current_mid)
        if current_edge < 0.02:
            logger.info(f"Edge evaporated: was {edge.raw_edge:.1%}, now {current_edge:.1%}")
            return None
        
        # Depth check: don't order more than what's available at the best price
        # On thin books, this prevents orders that would partially fill at worse prices
        order_size = min(edge.recommended_contracts, max(1, int(best_bid_size)))
        
        order_price = max(0.01, min(0.99, order_price))
        
        order = await self.client.create_order(
            ticker=market.ticker,
            side="yes" if edge.direction == "BUY_YES" else "no",
            type="limit",
            price=order_price,
            count=order_size,
        )
        
        return order
    
    async def monitor_open_orders(self):
        """
        Check open orders. Cancel and re-evaluate if:
        - Order is older than 10 minutes (market may have moved)
        - Market has moved against us (new price is worse than our edge threshold)
        """
        orders = await self.client.get_orders(status="open")
        for order in orders:
            age_minutes = (datetime.utcnow() - order.created_time).total_seconds() / 60
            if age_minutes > 10:
                await self.client.cancel_order(order.order_id)
                logger.info(f"Cancelled stale order {order.order_id} after {age_minutes:.0f} min")
```

### 1.10 Performance Tracker (`tracker.py`)

Every trade recorded with:
```python
@dataclass
class TradeRecord:
    trade_id: str
    market_ticker: str
    city: str
    entry_time: datetime
    entry_price: float
    side: str                       # "YES" or "NO"
    quantity: float                  # now supports fractional
    ensemble_prob_at_entry: float
    ensemble_members_at_entry: int
    ensemble_spread_at_entry: float
    bias_correction_applied: float
    edge_at_entry: float
    mid_price_at_signal: float      # mid-price when the edge was first computed
    fill_price: float               # actual price the order filled at
    realized_slippage: float        # fill_price - mid_price_at_signal (positive = adverse)
    was_maker_fill: bool            # critical: tracks if we paid zero fees
    fees_paid: float
    order_placed_time: datetime     # when the limit order was submitted
    order_filled_time: datetime | None  # when it actually filled (None if cancelled)
    order_cancelled: bool           # True if order expired without filling
    resolution_time: datetime | None
    actual_outcome: int | None      # 0 or 1
    actual_max_temp_f: float | None # from NOAA observation
    pnl: float | None
    ai_was_consulted: bool          # was Claude called for this trade?
    ai_cost_usd: float              # API cost if Claude was called
    bias_version: str               # which bias correction version was active
    calibration_version: str        # which calibration model was active
    param_version: str              # which edge/Kelly parameters were active
```

**Key Metrics (computed daily):**
- Win rate (overall and by city)
- Brier score: `mean((ensemble_prob - actual_outcome)^2)` — target: below 0.18
- Beat-market rate: % of trades where our estimate was closer to truth than market price — target: above 55%
- Maker fill rate: % of fills that were maker (zero fee) — target: above 70%
- Order fill rate: % of placed orders that actually filled (vs cancelled unfilled) — target: above 50%
- Average realized slippage: mean of fill_price - mid_price_at_signal — target: below $0.01
- Average time-to-fill: mean of order_filled_time - order_placed_time — context metric
- ROI: net P&L / capital deployed — must be positive after fees
- Total AI cost: sum of Claude API spend — track as expense line item
- Net P&L: trading P&L minus fees minus AI costs minus hosting costs
- **Rolling 50-market Brier score and beat-market rate** — for regime change detection (see section 5.4)

---

## PART 2: CONFIGURATION & DEPLOYMENT

### 2.1 `settings.yaml`

```yaml
trading_mode: "live"  # "demo" or "live" — THIS DEPLOYMENT USES "live"

api:
  demo_base_url: "https://demo-api.kalshi.co/trade-api/v2"
  live_base_url: "https://api.elections.kalshi.com/trade-api/v2"
  request_timeout_seconds: 30
  max_retries: 3
  cache_ttl_market_seconds: 30
  cache_ttl_portfolio_seconds: 5

scanner:
  poll_interval_minutes: 5
  min_volume_fp: 5                # lowered from 50 — Kalshi weather markets are thin
  max_spread_dollars: 0.15        # raised from 0.08 — weather markets have wider spreads
  min_resolution_hours: 6
  max_resolution_days: 7

ensemble:
  base_url: "https://api.open-meteo.com/v1/ensemble"
  models: ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_global"]
  cache_ttl_minutes: 15  # don't re-fetch within 15 min
  min_members_for_trade: 30

ai:
  model: "claude-sonnet-4-20250514"
  max_calls_per_day: 50
  sanity_check_edge_threshold: 0.15  # only call AI when edge > 15pp
  temperature: 0.1

edge:
  min_edge_high_confidence: 0.04
  min_edge_medium_confidence: 0.06
  min_edge_low_confidence: 0.10  # effectively skip low confidence
  kelly_fraction: 0.25

risk:
  max_position_dollars: 50
  max_total_exposure_dollars: 250
  max_daily_loss_dollars: 30
  max_open_orders: 10

notifications:
  enabled: true
  method: "telegram"  # "telegram", "webhook", or "log_only"

scheduling:
  high_frequency_windows:
    - name: "weather_model_00Z"
      days: ["daily"]
      start_utc: "03:00"     # GFS 00Z available ~03:30 UTC
      end_utc: "04:00"
      poll_interval_minutes: 2
    - name: "weather_model_12Z"
      days: ["daily"]
      start_utc: "15:00"     # GFS 12Z available ~15:30 UTC
      end_utc: "16:00"
      poll_interval_minutes: 2
  low_frequency_windows:
    - name: "overnight"
      days: ["daily"]
      start_utc: "04:00"
      end_utc: "10:00"
      poll_interval_minutes: 15
  default_poll_interval_minutes: 5
```

### 2.2 Environment Variables (`.env`)

```bash
# DEMO CREDENTIALS (kept for reference, unused during live trading)
KALSHI_DEMO_API_KEY_ID=your_demo_key_id
KALSHI_DEMO_PRIVATE_KEY_PATH=/path/to/demo_private_key.pem

# REQUIRED — THIS DEPLOYMENT IS LIVE (real money, $30 bankroll)
TRADING_MODE=live
KALSHI_LIVE_API_KEY_ID=your_live_key_id
KALSHI_LIVE_PRIVATE_KEY_PATH=/path/to/live_private_key.pem
ANTHROPIC_API_KEY=your_anthropic_key

# NOTIFICATIONS
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# OPTIONAL
LOG_LEVEL=INFO
DB_PATH=./data/trades.db
```

### 2.3 Autonomous Operation

The system runs as a long-lived daemon process using `asyncio`. No interactive input anywhere in the codebase. Every decision is programmatic.

**Deployment options (pick one):**

1. **systemd service** (Linux VPS, recommended): auto-restarts on crash, survives reboots, persistent logs via journalctl. Template service file included in the project.

2. **Docker container**: `docker-compose up -d`. Volumes persist data across restarts.

3. **tmux/nohup** (development): `nohup python -m src.daemon >> logs/daemon.log 2>&1 &`

**The system CANNOT run on a laptop that goes to sleep.** Deploy to a $5-10/month VPS (DigitalOcean, Hetzner, Linode) or a home machine that stays powered.

**Health monitoring:** Circuit breakers for Kalshi API, Open-Meteo API, and NOAA API. Each breaker trips after 3 consecutive failures, pauses for 5 minutes, then tests recovery. Telegram notification on every circuit breaker trip.

**Graceful shutdown:** On SIGTERM/SIGINT, finish the current cycle, don't cancel open orders (they persist on Kalshi's side), and stop. On restart, sync positions from Kalshi API (source of truth), reconcile with local DB, and resume.

**CLI interface:**
```bash
python -m src.daemon start       # start in background
python -m src.daemon stop        # graceful shutdown
python -m src.daemon status      # current state, open positions, P&L
python -m src.daemon pause       # pause trading, keep scanning
python -m src.daemon resume      # resume trading
```

### 2.4 Demo-to-Live Transition

**Pre-Live Checklist (automated, must ALL pass):**
1. `TRADING_MODE=live` is set
2. Live API credentials are present and valid
3. Risk limits are configured and non-zero
4. At least 100 paper-traded weather markets have resolved
5. Paper trading Brier score < 0.22
6. Paper trading beat-market rate > 52%
7. Paper trading ROI is positive after all costs
8. Maker fill rate > 60%
9. No single-day loss exceeded 5% of portfolio in paper trading
10. Database backup exists from last 24 hours

Manual confirmation: `"I understand I am trading real money with a maximum exposure of $50 in shadow mode"` typed at the terminal.

**Shadow mode (default for first live deployment):** Real orders placed up to $50 total exposure. Remaining opportunities paper-traded in parallel. Both tracked separately. Do NOT increase beyond $50 until 200+ total resolved markets (paper + live) with sustained positive metrics.

---

## PART 3: SELF-IMPROVEMENT LOOP

### 3.1 Post-Resolution Recording

After every weather market resolves:
1. Pull actual observation from NOAA for the settlement station
2. Record: ensemble probability, market price at entry, actual outcome, actual temperature, P&L
3. Compute: prediction_error = |ensemble_prob - actual_outcome|, market_error = |market_price - actual_outcome|
4. Store `did_we_beat_market = prediction_error < market_error`

### 3.2 Calibration (Every 50 Resolved Markets)

- Compute Brier score overall and per city
- Compute calibration curve (predicted vs actual by decile)
- Compute beat-market rate
- Per-station, per-month bias analysis: is the ensemble systematically over/under-predicting at specific stations?
- Update `station_bias.db` with new corrections

**Auto-kill switch:** If beat-market rate drops below 50% over a rolling 100-market window, HALT all trading and notify. The system has no edge and should not be trading.

### 3.3 Bias Correction Updates (Event-Driven + Seasonal Awareness)

**Event-driven updates:** Update the bias correction for a specific station whenever it accumulates 20 new resolved markets since the last correction update. For a city like NYC with daily markets, that's roughly every 3 weeks. For a less-traded city, it might take longer. This adapts naturally to data availability without overfitting to small samples.

**Seasonal transition handling:** Weather model bias changes meaningfully between seasons (summer convection vs winter frontal systems). When the calendar month changes, the system should:
1. Flag that the current bias correction may be stale for the new season
2. Widen the minimum edge threshold by 2 percentage points for the first 10 resolved markets in the new month (require more edge to trade when bias correction is uncertain)
3. After 10+ markets resolve in the new month, refit the correction and return to normal thresholds

**Isotonic regression calibration:** Fit per-city once N > 80 resolved markets for that city. Apply to raw ensemble probabilities before edge calculation. Refit whenever 20 new markets accumulate since the last fit.

### 3.4 Prompt Evolution (Every 200 Resolved Markets)

Pull all AI sanity-check logs where the AI flagged a trade that turned out to be correct (false negative) or approved a trade that failed badly (false positive). Use Claude to generate corrective instructions for the sanity-check prompt. Version the prompt and track performance before/after changes.

### 3.5 Dual-Arm Experimentation Framework (Finding the Right Aggression Level)

**The problem:** If every guardrail is set to maximum conservatism, the system may never find enough qualifying trades to generate meaningful data. But if guardrails are loosened across the board, a lucky streak could masquerade as an edge, leading to overconfident live deployment. The solution: run two strategies simultaneously on the same market data and compare their PAPER results.

**How it works:** For every market the scanner evaluates, compute the edge and recommendation under TWO parameter sets:

```python
# Conservative arm (default — this is what places real orders)
conservative = {
    "min_edge_high_confidence": 0.04,
    "min_edge_medium_confidence": 0.06,
    "assumed_slippage": 0.01,
    "fee_multiple_required": 3.0,
    "min_ensemble_members": 30,
    "min_volume_fp": 5,
    "kelly_fraction": 0.25,
}

# Exploratory arm (paper-only — NEVER places real orders)
exploratory = {
    "min_edge_high_confidence": 0.025,     # lower threshold
    "min_edge_medium_confidence": 0.04,
    "assumed_slippage": 0.005,             # half the slippage assumption
    "fee_multiple_required": 1.5,          # lower fee coverage
    "min_ensemble_members": 20,            # accept smaller ensembles
    "min_volume_fp": 5,                    # accept thinner markets (exploratory arm matches conservative floor)
    "kelly_fraction": 0.25,               # same sizing (for fair comparison)
}
```

**Both arms evaluate every market.** The conservative arm's recommendations drive real orders. The exploratory arm's recommendations are logged but never executed. After each market resolves, both arms' hypothetical P&L is computed.

**What you learn:**
- If the exploratory arm significantly outperforms the conservative arm over 100+ markets, your guardrails are too tight. The trades you're skipping are actually profitable.
- If the exploratory arm underperforms or loses money, your guardrails are correctly protecting you. The trades you're skipping would have lost money.
- If both arms perform similarly, the extra conservatism isn't costing you much but isn't helping much either — simplify by tightening to the conservative arm.

**After 100+ resolved markets in both arms, the calibration report should include:**
```
=== EXPERIMENTATION REPORT ===
Conservative arm: 87 trades evaluated, 23 would have traded, 16 won, ROI +4.2%
Exploratory arm:  87 trades evaluated, 41 would have traded, 26 won, ROI +5.8%

Trades the exploratory arm took that conservative skipped: 18
  Of those 18: 10 won, 8 lost, ROI +2.1%
  → The skipped trades WERE profitable on average
  → Consider relaxing: min_edge from 0.04 to 0.03 for high confidence

Trades unique to exploratory that lost: 8
  Common pattern: 5 of 8 were on thin markets (volume < 50)
  → Keep the volume threshold at 50, but consider lowering edge threshold
```

**This is how you tune aggression empirically rather than guessing.** You don't weaken guardrails blindly — you measure exactly which guardrails are costing you money and which are saving you money, then adjust based on evidence.

**Key constraints on the exploratory arm:**
- It NEVER places real orders. It is purely hypothetical.
- It uses the SAME ensemble data and market prices as the conservative arm (no separate API calls, no extra cost).
- It computes its own P&L assuming maker fills at the prices available at the time of evaluation.
- Results are logged in the same database with a `strategy_arm` field ("conservative" or "exploratory").
- After 200+ markets, you can choose to adopt specific relaxations from the exploratory arm into the conservative arm. Each relaxation should be adopted individually, not all at once.

---

## PART 4: IMPLEMENTATION TIMELINE (Compressed 5-Week Plan)

The system goes live with real money (small amounts) after 100 resolved paper markets, then continues accumulating data to refine the algorithm while trading. Early live trading IS continued validation — the system improves from its own real-world performance.

### Phase 0: Foundation (Days 1-5)
1. Set up project structure and Git repo
2. Install `kalshi-python` SDK, build the custom wrapper with retry/caching
3. Connect to demo API, list weather markets, parse orderbooks
4. Begin data collection: snapshot all weather market states every 5 minutes
5. Build station mapping from Kalshi series → lat/lon

### Phase 1: Probability Engine (Days 6-10)
1. Implement `weather_ensemble.py` with Open-Meteo Ensemble API
2. Implement `probability_engine.py` — ensemble → probability computation
3. Test against known weather outcomes: does the ensemble actually predict well?
4. Implement `weather_observations.py` for NOAA ground truth
5. Run `scripts/compute_station_bias.py` to generate initial bias corrections

### Phase 2: Trading Logic + Execution (Days 11-17)
1. Implement `edge_calculator.py` with correct quadratic fee formula and zero maker fees
2. Implement `risk_manager.py` with all hard limits
3. Implement `ai_reasoner.py` for sanity checks on large edges
4. Implement `order_executor.py` with limit-order-only, maker-fill-optimized execution
5. Implement `tracker.py` with full trade recording
6. Wire the full pipeline: scanner → ensemble → probability → edge → risk → execute → log
7. Run in dry-run mode: logging what trades WOULD be made, not executing

### Phase 3: Daemon + Deployment (Days 18-21)
1. Implement `daemon.py`, `health_monitor.py`, `notifier.py`
2. Deploy to VPS
3. Verify it runs unattended overnight, sends Telegram notifications
4. Confirm dry-run trades are being logged correctly with ensemble probabilities

### Phase 4: Paper Validation (Days 22-42, ~3 weeks)
1. System runs autonomously in dry-run mode on VPS
2. Accumulate 100+ resolved weather markets across multiple cities
3. At Day 35 (~100 markets): run first calibration report
4. Evaluate: Brier score < 0.22? Beat-market rate > 52%? Maker fill rate feasible?
5. If metrics pass: prepare for shadow-mode live transition
6. If metrics fail: identify and fix systematic biases, extend paper trading

### Phase 5: Live Trading + Continued Learning (Day 43+)
1. **Shadow mode first:** $50 maximum real-money exposure. Remaining opportunities continue as paper trades.
2. Run the pre-live checklist. Manual confirmation required.
3. Compare live fills and fees against paper expectations for 2 weeks.
4. If live results match paper within reasonable variance: increase to full $250 exposure cap.
5. **Continue accumulating data.** Every resolved market improves bias corrections and calibration.
6. At 200 resolved markets: full calibration report, bias correction refit, optional prompt evolution.
7. At 500 resolved markets: walk-forward parameter optimization (edge thresholds, Kelly fraction).
8. **The system never stops improving.** Live trading data is the best validation data because it includes real execution costs, real fill rates, and real market impact.

### Why This Works Better Than Waiting

Traditional quant advice says "don't go live until you have 200+ markets of validation." That advice assumes you're risking significant capital. With a $50 shadow cap, your maximum possible loss during the live validation phase is $50 total. The information you gain from real execution (actual fill rates, actual fees, actual slippage) is worth far more than $50. You're paying for data, and it's cheap data.

The key constraint: **never increase exposure beyond $50 until you have 200+ resolved markets (paper + live combined) with sustained positive metrics.** Impatience at the sizing stage is where traders blow up, not at the entry stage.

---

## PART 5: HIDDEN RISKS — WHAT KILLS TRADING SYSTEMS

These risks are not theoretical. Each one has destroyed real trading systems. The code must handle all of them.

### 5.1 Transaction Cost Reality

**The scenario:** You spot a weather contract at $0.40 with an ensemble probability of 0.48. That looks like an 8-cent edge. But:
- If you BUY at $0.40 (taker fill): fee = ceil(0.07 * 1 * 0.40 * 0.60 * 100) / 100 = $0.02
- If you later SELL before resolution at $0.45 (taker fill): fee = ceil(0.07 * 1 * 0.45 * 0.55 * 100) / 100 = $0.02
- Round-trip cost: $0.04. Your $0.05 gross profit becomes $0.01 net.
- If you got ONE taker fill and ONE maker fill: cost drops to $0.02. Profit: $0.03.
- If you hold to settlement (no exit fee): cost is just the entry fee.

**Implementation requirement:** The edge calculator must compute edge for THREE scenarios:
1. **Hold to settlement with maker entry** (best case): fee = $0.00. Edge = raw edge.
2. **Hold to settlement with taker entry** (likely case): fee = taker formula. Edge = raw - taker fee.
3. **Early exit with taker entry AND taker exit** (worst case): fee = 2x taker formula. Edge = raw - 2x taker fee.

The system should only trade if the **likely case** (scenario 2) is profitable. Position sizing should use the **best case** (scenario 1) since we target maker fills. The early exit fee (scenario 3) should be computed and stored so the re-evaluation logic knows the true cost of closing a position before settlement.

### 5.2 Slippage Model

**The scenario:** The ensemble says 75% for a market priced at $0.65. You place a limit BUY YES at $0.65 (the best bid). Three things can happen:
- **Best case:** Your order rests on the book, someone takes it, you get a maker fill at $0.65 with zero fees.
- **Common case:** By the time your order reaches Kalshi, another order already moved the best bid to $0.66. Your $0.65 order is now below the best bid. It rests but may never fill because the market moved.
- **Worst case:** The market reprices quickly after a model update. By the time you act, the price has moved to $0.70, eliminating most of your edge.

**Implementation requirements:**
1. **Fill rate tracking.** Track what percentage of placed orders actually fill within the 10-minute window. If fill rate drops below 40%, the system is systematically too slow or placing prices too far from the market. Log this and alert.
2. **Effective slippage computation.** For every filled order, compare the fill price against the mid-price at the time the edge was computed. The difference is realized slippage. Track the average. If average slippage exceeds 1 cent, investigate.
3. **Adaptive pricing.** If fill rate is low (orders expiring unfilled), the order executor should optionally place 1 tick above the best bid (closer to the ask) to increase fill probability. This may convert some maker fills to taker fills, so there's a tradeoff — track whether the increased fill rate outweighs the fee cost.
4. **Staleness guard.** Before placing an order, re-fetch the orderbook. If the mid-price has moved more than 2 cents since the edge was computed, re-compute the edge with the fresh price. If the edge is gone, don't trade.

### 5.3 Overfitting Protection

**The scenario:** After 80 resolved NYC markets, the system computes a bias correction of -2.1°F for GFS in July. It applies this correction, and the next 20 NYC markets happen to go well. Was the correction correct, or did 20 lucky outcomes confirm a noisy estimate? If the true bias is -0.5°F and the system is correcting by -2.1°F, it's systematically underestimating temperatures and will lose money once luck evens out.

**Implementation requirements:**
1. **Minimum sample sizes are hard rules, not guidelines:**
   - Bias correction: minimum 30 resolved markets per station per season before applying
   - Isotonic calibration: minimum 80 resolved markets per city before fitting
   - Parameter changes (edge thresholds, Kelly fraction): minimum 200 total resolved markets before any optimization
   - No exceptions. If the sample is too small, use the default (no correction, no calibration).

2. **Holdout validation for calibration models.** When fitting an isotonic regression, hold out 20% of the data USING TEMPORAL SPLITS ONLY — hold out the most recent 20%, never random rows. Time-series data has temporal dependencies; random splits leak future information into the training set. If the model's Brier score on the holdout set is worse than the uncalibrated Brier score, the calibration is overfitting. Don't apply it.

3. **Walk-forward validation for parameter optimization (at 500+ markets).** Train on months 1-3, test on month 4. Then train on months 1-4, test on month 5. And so on. Never optimize parameters on the same data you evaluate them on. Only adopt a parameter change if it improves performance on the out-of-sample test set across ALL walk-forward windows, not just the most recent one.

4. **Multi-city validation.** The strategy must be profitable in at least 3 out of 5 cities that have sufficient data (N > 30 each). If it only works in NYC and fails everywhere else, the NYC performance is likely noise or an artifact of station-specific conditions, not a generalizable edge. Don't trade a city where the rolling 50-market Brier score is above 0.24.

5. **Complexity penalty — prefer simplicity.** The core probability engine is ensemble member counting. The calibration is isotonic regression. These are deliberately simple. If at any point Claude Code or a future developer proposes adding a neural network, gradient boosting, or any model with more than ~5 tunable parameters, require evidence that it outperforms the simple approach by at least 10% on out-of-sample data before adopting it. Complexity adds overfitting risk and maintenance burden. If the simple approach gets you 80% of the way there, keep it.

6. **Reality check on backtest results.** If the paper-trading period shows annualized returns exceeding 20%, flag the result as suspicious and require manual review before going live. In a thin market with small positions, returns above 20% almost certainly indicate one of: a fee calculation bug, a data leakage issue, a look-ahead bias in the backtester, or a lucky streak in a small sample. Investigate before celebrating.

7. **Track performance by prompt version and parameter version.** Every trade records which version of the bias correction, calibration model, and edge thresholds were active. This lets you isolate whether a parameter change actually helped or if the market just happened to cooperate.

### 5.4 Regime Change Detection

**The scenario:** The system works great for 3 months. Then NOAA upgrades the GFS model to a new version, and the old bias corrections are wrong for the new model. Or Kalshi introduces a volume incentive program that attracts sophisticated participants who reprice weather markets faster, shrinking the system's speed advantage. Or a heat wave generates intense media coverage, and the favorite-longshot bias temporarily reverses as retail participants start overpricing YES contracts on hot days.

**Implementation requirements:**
1. **Rolling performance monitoring.** Compute Brier score and beat-market rate on a rolling 50-market window, not just cumulative. If the rolling metrics deteriorate significantly (Brier > 0.25 or beat-market < 48% over 50 markets), trigger an alert and automatically widen edge thresholds by 2 percentage points until metrics recover.

2. **Live-vs-paper performance comparison.** Once live trading begins, continuously compare live Brier score and ROI against the paper-trading baseline. If live performance drops below 60% of paper performance (measured by Sharpe ratio or ROI) over a rolling 30-day window, trigger a full halt and alert. This catches regime changes that aren't visible in the rolling window alone — the system might still be "okay" in absolute terms but dramatically worse than what the paper period showed, indicating something fundamental has changed.

3. **Ensemble model version tracking.** Log which models contributed to each estimate. If Open-Meteo changes model names, adds new models, or retires old ones, the system should detect this (the API response structure will change) and alert rather than silently producing bad estimates.

4. **Market behavior monitoring.** Track how long it takes for Kalshi weather markets to reprice after a model run. If the average repricing time shortens from 2 hours to 30 minutes, the system's speed advantage is eroding. Track this and alert if the trend is negative over 30+ observations.

5. **Recency-weighted bias corrections.** Instead of weighting all historical data equally, apply exponential decay weighting to the bias correction computation. Recent observations (last 30 days) should count 3x more than observations from 6 months ago. This ensures the bias correction tracks gradual changes like model updates, station equipment changes, or urbanization effects near the station. Implementation: when computing station bias, weight each historical observation by `exp(-lambda * days_since_observation)` where lambda = 0.01 (half-life of ~70 days).

6. **Automatic conservatism on degradation.** If any monitored metric degrades beyond its threshold for 3 consecutive measurement periods, the system should automatically:
   - Increase all minimum edge thresholds by 2 percentage points
   - Reduce Kelly fraction from 0.25 to 0.15
   - Send a detailed alert with the degraded metrics
   - Continue trading at reduced size rather than stopping entirely (you need continued data to diagnose the problem)

---

## PART 6: PITFALLS TO AVOID

1. **Don't call Claude for weather probabilities.** The ensemble is cheaper, faster, and more accurate. Claude is for edge cases only.

2. **Don't trade markets resolving in <6 hours.** Forecast accuracy drops sharply at very short lead times, and execution risk dominates.

3. **Don't ignore the station mapping.** If your forecast is for the wrong lat/lon, your probability is meaningless. Verify every station.

4. **Don't assume integer prices.** The March 2026 migration means all prices are dollar strings. Parse them carefully.

5. **Don't forget to track maker vs taker fills.** Your edge calculation assumes maker fills (zero fees). If you're getting mostly taker fills, your actual P&L will be worse than projected.

6. **Don't skip the bias correction.** GFS overpredicts temperatures at some stations by 1-3°F. That systematic error, if not corrected, flows directly into your probability estimates.

7. **Don't overtrade.** 2-5 trades per day is the target, not 50. Each trade must have a documented edge above the minimum threshold.

8. **Don't go live without 200+ paper-traded resolutions.** Statistical significance requires sample size. 14 winning trades in a row proves nothing.

9. **Don't hold positions you can't justify.** If a new model run eliminates the edge, cancel the order or exit the position. Don't hope.

10. **Don't track trading P&L without subtracting ALL costs.** Net P&L = trading gains - Kalshi fees - Claude API costs - VPS hosting. If net is negative, the system is failing.

---

## APPENDIX A: Python Dependencies

```
# requirements.txt
kalshi-python>=2.1.4           # Official Kalshi SDK (handles RSA-PSS auth)
anthropic>=0.39.0              # Claude API client
httpx>=0.27.0                  # Async HTTP for Open-Meteo, NOAA
pyyaml>=6.0                    # Config files
pandas>=2.2.0                  # Data manipulation
numpy>=1.26.0                  # Numerical computing
scipy>=1.13.0                  # Statistical functions
scikit-learn>=1.5.0            # Isotonic regression calibration
sqlite-utils>=3.36             # SQLite convenience
python-dotenv>=1.0.0           # Env loading
click>=8.1.0                   # CLI framework
rich>=13.7.0                   # Terminal formatting
aiofiles>=24.1.0               # Async file I/O
pytest>=8.0.0                  # Testing
pytest-asyncio>=0.23.0         # Async test support
```

## APPENDIX B: Key URLs (Verified March 25, 2026)

| Service | URL | Auth Required |
|---|---|---|
| Kalshi Demo API | `https://demo-api.kalshi.co/trade-api/v2` | Yes (RSA-PSS) |
| Kalshi Production API | `https://api.elections.kalshi.com/trade-api/v2` | Yes (RSA-PSS) |
| Kalshi Demo WebSocket | `wss://demo-api.kalshi.co/trade-api/ws/v2` | Yes |
| Kalshi Production WebSocket | `wss://api.elections.kalshi.com/trade-api/ws/v2` | Yes |
| Open-Meteo Forecast | `https://api.open-meteo.com/v1/forecast` | No |
| Open-Meteo Ensemble | `https://api.open-meteo.com/v1/ensemble` | No |
| Open-Meteo Previous Runs | `https://previous-runs-api.open-meteo.com/v1/forecast` | No |
| NOAA Weather API | `https://api.weather.gov` | No (User-Agent required) |
| Anthropic Claude API | `https://api.anthropic.com/v1/messages` | Yes (API key) |
| Telegram Bot API | `https://api.telegram.org/bot{token}/sendMessage` | Yes (bot token) |

---

*End of specification v2.2. Changes from v2.1: Added Claude Code interaction instructions (persona, questioning, expert perspectives). Added Part 5: Hidden Risks covering transaction cost modeling (three-scenario fee analysis), slippage model with fill rate tracking and adaptive pricing, overfitting protection (minimum sample sizes, holdout validation, walk-forward testing), and regime change detection with automatic conservatism. Added slippage tracking, fill rate metrics, and version tracking to TradeRecord. Added early exit fee to EdgeAssessment.*