# Portfolio Divergence Analysis

> How to investigate why two parallel portfolios with the same config have diverged. Use this when comparing Alpaca vs simulated, or any two LiveConfigs that should behave identically.

## Quick reference

| What you need | Where to look | How |
|---------------|---------------|-----|
| Live config parameters | `live_configs` table | [Compare configs](#step-1-confirm-configs-match) |
| Trade history per portfolio | `watches` table (data in `watch_json`) | [Compare trades](#step-2-compare-trade-histories) |
| Alpaca fill details | `alpaca_transactions` table | [Check fills](#step-3-check-alpaca-fill-prices) |
| Replacement exits | `watch_json → $.exit.reason` | [Identify churn](#step-4-identify-replacement-churn) |
| Current holdings | `watches WHERE status='holding'` | [Compare holdings](#step-5-compare-current-holdings) |
| Entry price slippage | `watches` matched by `entry_snapshot_id` | [Measure slippage](#step-6-measure-entry-slippage) |
| Matched trade comparison | `watches` joined on `entry_snapshot_id + symbol` | [Compare trade outcomes](#step-7-matched-trade-comparison) |

## Important: watches table structure

Watch data lives in the `watch_json` JSON column, NOT in top-level columns. Use `json_extract()`:

```sql
-- Key fields in watch_json:
json_extract(watch_json, '$.live_config_id')   -- e.g. 'lc_cbb9d749ff2f'
json_extract(watch_json, '$.entry.price')      -- entry price
json_extract(watch_json, '$.exit.price')       -- exit price
json_extract(watch_json, '$.exit.reason')      -- signal, replaced, guard_stop, sell_fill, etc.
json_extract(watch_json, '$.exit.time')        -- exit timestamp (UTC ISO 8601)
json_extract(watch_json, '$.entry.time')       -- entry timestamp
json_extract(watch_json, '$.qty')              -- position quantity
json_extract(watch_json, '$.peak_pnl_pct')     -- best unrealized P&L during hold
json_extract(watch_json, '$.trough_pnl_pct')   -- worst unrealized P&L during hold
json_extract(watch_json, '$.alpaca_stop_price') -- server-side stop price
```

The `live_configs` table uses `config_id` (not `id`) for the LiveConfig identifier:
```sql
SELECT config_id, name, active, config_json FROM live_configs WHERE config_id = 'lc_XXXX';
```

## The process

### Step 1: Confirm configs match

Before assuming a bug, verify both LiveConfigs have identical parameters.

```sql
SELECT config_id, name, active, config_json FROM live_configs
WHERE config_id IN ('lc_AAAA', 'lc_BBBB');
```

Parse `config_json` and diff key fields: `filters`, `exit_strategy`, `exit_params`, `allocation`, `allocation_params` (especially `max_pos`, `when_full`, `rank_method`, `replace_min_margin`), `guard_stop_pct`, `guard_target_pct`, `guard_trail_pct`, `min_hold`, `live_overrides`, `alpaca_account_id`. If they differ, the divergence may be intentional.

**Watch for `live_overrides`** — this controls tick-based VDD. If one config has `vdd_tick: true` and the other has `{}`, they will exit differently.

### Step 2: Compare trade histories

```sql
SELECT
    json_extract(watch_json, '$.live_config_id') AS config_id,
    COUNT(*) AS total,
    SUM(CASE WHEN status IN ('exited','cooling_off','sealed') THEN 1 ELSE 0 END) AS closed,
    SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) AS holding,
    SUM(CASE WHEN json_extract(watch_json, '$.exit.reason') = 'replaced' THEN 1 ELSE 0 END) AS replacements,
    SUM(CASE WHEN json_extract(watch_json, '$.exit.reason') LIKE '%stop%' THEN 1 ELSE 0 END) AS stops,
    SUM(CASE WHEN json_extract(watch_json, '$.exit.reason') LIKE '%signal%' THEN 1 ELSE 0 END) AS signals,
    SUM(CASE WHEN json_extract(watch_json, '$.exit.reason') = 'sell_fill' THEN 1 ELSE 0 END) AS sell_fills
FROM watches
WHERE json_extract(watch_json, '$.live_config_id') IN ('lc_AAAA', 'lc_BBBB')
GROUP BY config_id;
```

**Red flags:**
- **Many `sell_fill` exits on Alpaca side** — indicates the [stream handler race condition](#4-stream-handler-race-condition-sell_fill). The real exit reasons are being lost.
- One config has significantly more total trades (churn)
- Alpaca portfolio has fewer holdings than `max_pos` — under-allocation from failed replacement buys
- Exit reason `replaced` uses the value `"replaced"` (not `"replacement"`)

### Step 3: Check Alpaca fill prices

```sql
SELECT symbol, event, status,
       json_extract(detail_json, '$.filled_avg_price') AS fill_price,
       json_extract(detail_json, '$.notional') AS intended_notional,
       json_extract(detail_json, '$.stop_price') AS stop_price,
       created_at
FROM alpaca_transactions
WHERE account_id = 'ACCOUNT_ID'
  AND event IN ('buy_confirmed', 'sell_confirmed', 'stop_submit')
  AND created_at >= '2026-03-11'
ORDER BY created_at;
```

### Step 4: Identify replacement churn

```sql
SELECT symbol,
       json_extract(watch_json, '$.entry.price') AS entry_price,
       json_extract(watch_json, '$.exit.price') AS exit_price,
       ROUND((json_extract(watch_json, '$.exit.price') - json_extract(watch_json, '$.entry.price'))
             / json_extract(watch_json, '$.entry.price') * 100, 2) AS pnl_pct,
       json_extract(watch_json, '$.exit.reason') AS exit_reason,
       json_extract(watch_json, '$.exit.time') AS exited_at
FROM watches
WHERE json_extract(watch_json, '$.live_config_id') = 'lc_XXXX'
  AND json_extract(watch_json, '$.exit.reason') = 'replaced'
ORDER BY exited_at;
```

**Churn death spiral pattern:** Many small negative replacement exits (-0.5% to -2%) in rapid succession. This means the replacement scoring threshold is too aggressive.

### Step 5: Compare current holdings

```sql
SELECT
    json_extract(watch_json, '$.live_config_id') AS config_id,
    COUNT(*) AS current_holdings
FROM watches
WHERE json_extract(watch_json, '$.live_config_id') IN ('lc_AAAA', 'lc_BBBB')
  AND status = 'holding'
GROUP BY config_id;
```

If the Alpaca portfolio has fewer holdings than `max_pos` while the sim is near capacity, this indicates **under-allocation from the sell_fill race condition** — replacement buys failed silently.

### Step 6: Measure entry slippage

For matched trades (same symbol + snapshot), compare entry prices:

```sql
SELECT s.symbol,
       json_extract(s.watch_json, '$.entry.price') AS sim_entry,
       json_extract(a.watch_json, '$.entry.price') AS alp_entry,
       ROUND((json_extract(a.watch_json, '$.entry.price') - json_extract(s.watch_json, '$.entry.price'))
             / json_extract(s.watch_json, '$.entry.price') * 100, 3) AS slippage_pct
FROM watches s
JOIN watches a ON s.entry_snapshot_id = a.entry_snapshot_id AND s.symbol = a.symbol
WHERE json_extract(s.watch_json, '$.live_config_id') = 'lc_SIM'
  AND json_extract(a.watch_json, '$.live_config_id') = 'lc_ALP'
  AND json_extract(a.watch_json, '$.entry.price') > 0
ORDER BY slippage_pct DESC;
```

Systematic positive slippage on the Alpaca side means limit price buffers may be too generous, or market orders are getting poor fills during volatility.

### Step 7: Matched trade comparison

The most powerful query. Join on `entry_snapshot_id + symbol` to see how the same trade played out in both portfolios:

```sql
SELECT
    s.symbol,
    json_extract(s.watch_json, '$.exit.reason') AS sim_reason,
    json_extract(a.watch_json, '$.exit.reason') AS alp_reason,
    ROUND((json_extract(s.watch_json, '$.exit.price') - json_extract(s.watch_json, '$.entry.price'))
          / json_extract(s.watch_json, '$.entry.price') * 100, 2) AS sim_pnl,
    ROUND((json_extract(a.watch_json, '$.exit.price') - json_extract(a.watch_json, '$.entry.price'))
          / json_extract(a.watch_json, '$.entry.price') * 100, 2) AS alp_pnl,
    ROUND((json_extract(a.watch_json, '$.entry.price') - json_extract(s.watch_json, '$.entry.price'))
          / json_extract(s.watch_json, '$.entry.price') * 100, 3) AS entry_slip
FROM watches s
JOIN watches a ON s.entry_snapshot_id = a.entry_snapshot_id AND s.symbol = a.symbol
WHERE json_extract(s.watch_json, '$.live_config_id') = 'lc_SIM'
  AND json_extract(a.watch_json, '$.live_config_id') = 'lc_ALP'
  AND json_extract(s.watch_json, '$.exit.price') IS NOT NULL
  AND json_extract(a.watch_json, '$.exit.price') IS NOT NULL
ORDER BY json_extract(s.watch_json, '$.entry.time');
```

Categorize divergence:
```sql
-- Aggregate by divergence category
SELECT
    CASE
        WHEN json_extract(s.watch_json, '$.exit.reason') = json_extract(a.watch_json, '$.exit.reason') THEN 'same_reason'
        WHEN json_extract(a.watch_json, '$.exit.reason') = 'sell_fill' THEN 'sell_fill_race'
        ELSE 'diff_reason'
    END AS category,
    COUNT(*) AS cnt,
    ROUND(SUM(...sim_pnl...), 2) AS sim_pnl,
    ROUND(SUM(...alp_pnl...), 2) AS alp_pnl
FROM ...
GROUP BY category;
```

## Known divergence causes

### 1. Replacement scoring asymmetry (found 2026-03-12, FIXED)

**Symptom:** Alpaca portfolio churns heavily (many replacement exits), simulated portfolio makes zero replacements.

**Root cause:** In `_score_holding_watches` ([live_monitor.py](../../trader/online/live_monitor.py)), non-Alpaca portfolios had no `current_price`, so all positions scored 0.0. New signals also scored 0.0. Since `new_score > worst_score` was never true (0.0 > 0.0 is false), replacements never fired.

**Fix (2026-03-12):**
1. Non-Alpaca portfolios now fetch fresh quotes via `MarketDataService.get_quotes()` for `unreal_pl`/`composite` scoring
2. Simulated entry prices now use fresh Schwab quotes at decision time (not stale snapshot prices)
3. Added `replace_min_margin` anti-churn parameter (default 0.0) — new signal must score this much better than worst
4. Added 4 forward-looking ranking methods (`trailing_slope`, `volume_trend`, `rsi_current`, `tech_score`) that score symmetrically using bar data

**Key files:** [live_monitor.py](../../trader/online/live_monitor.py) `_score_holding_watches`, `_find_replacement_victim`, `_fetch_ranking_features_for_symbol`

### 2. Fill slippage compounding

**Symptom:** Alpaca portfolio consistently enters positions at higher prices than simulated.

**Root cause:** Market/limit order fills during volatile moments. Extended hours fills are worse (thin liquidity + wider spreads).

**Measured impact (2026-03-17 analysis):** +0.35% average slippage per entry across 62 matched trades. Extended hours entries are worst offenders — RL (+2.5%), E (+2.9%), VACH (+3.6%). The 2-4% limit buffer in `_buy_extended()` allows very aggressive fills during thin liquidity.

**Cascading effect:** Higher entry prices push the stop-loss trigger to a higher absolute price, making stops more likely to fire. In observed data, two positions (STRZ, VG) hit -5% stops on Alpaca that the sim avoided entirely (sim exited via replacement at -1.4% and +0.7% respectively).

### 3. Stop-loss trigger differences

**Symptom:** Alpaca portfolio hits stop losses that the simulated portfolio avoids.

**Root cause:** Higher entry prices from slippage mean the -5% stop is at a higher absolute price. The simulated portfolio, with its lower entry, may exit via VDD signal or replacement before the stop is reached.

### 4. Stream handler race condition (`sell_fill`) (found 2026-03-17, OPEN)

**Symptom:** Alpaca portfolio shows many exits with reason `"sell_fill"` instead of the real reason (`"replaced"`, `"signal"`, `"signal_tick"`). Alpaca portfolio is chronically under-allocated (fewer holdings than `max_pos`).

**Root cause:** WebSocket stream handler in [alpaca_stream.py:178-225](../../trader/market/alpaca_stream.py) races with the monitor's exit recording in [live_monitor.py](../../trader/online/live_monitor.py).

The flow:
1. Monitor decides to exit (signal/replacement) → calls `broker.close_position(symbol)`
2. Sell order submitted → `alpaca_sell_order_id` saved on watch via `update_watch()`
3. WebSocket stream handler receives fill **instantly** (< 100ms)
4. Stream handler: `alpaca_sell_order_id` matches, but `exit` not yet recorded → records exit as `"sell_fill"`, changes status from `"holding"` to `"exited"`
5. Monitor's `wait_for_fill()` (polls every 0.5s) gets confirmation
6. Monitor calls `builder.record_exit(reason=real_reason)` locally
7. Monitor calls `update_watch_if_current_status(expected_status="holding")` → **FAILS** (status already `"exited"`)
8. For replacement exits (`_exit_victim`): returns `False` → **replacement buy never happens**

**Measured impact (2026-03-17 analysis, Pair 1):**
- 34 out of 70 Alpaca exits (49%) recorded as `sell_fill` instead of real reason
- Of those, 23 were intended replacements — only ~7 completed the replacement buy
- Portfolio ran at 14/20 holdings vs sim's 17/20
- Direct P&L divergence from affected trades: -13.32 percentage points
- Indirect impact (missed trades from under-allocation): -8.81 additional points

**Affected code paths:**
- [live_monitor.py](../../trader/online/live_monitor.py) `_check_holding()` lines 377-431 (signal/stop exits)
- [live_monitor.py](../../trader/online/live_monitor.py) `_exit_victim()` lines 1207-1259 (replacement exits)
- [alpaca_stream.py](../../trader/market/alpaca_stream.py) `_handle_sell_fill()` lines 178-225

**Proposed fix:** Store a `pending_exit_reason` field on the watch before submitting the sell. The stream handler uses it if present. In `_exit_victim`, if `update_watch_if_current_status` fails, check if the watch was already exited by the stream handler and return `True` (exit succeeded) so the replacement buy proceeds.

### 5. Stop-market order gap risk (found 2026-03-17, OPEN)

**Symptom:** Alpaca stop-loss fills at prices far worse than the intended -5% loss.

**Root cause:** `set_stop()` in [alpaca_broker.py:535-561](../../trader/market/alpaca_broker.py) uses `StopOrderRequest` — a **stop-market** order. When the stop price is triggered, it becomes a market order and fills at the next available price, which can be far below the stop during gaps or volatile moments.

**Observed cases (2026-03-17):**

| Symbol | Entry | Stop Price | Fill Price | Intended Loss | Actual Loss |
|--------|-------|-----------|-----------|--------------|-------------|
| NGS | 36.75 | 34.91 | 33.27 | -5.0% | -9.48% |
| VACH | 11.05 | 10.50 | 10.24 | -5.0% | -7.37% |
| STOK | 34.50 | 32.77 | 32.58 | -5.0% | -5.58% |

NGS nearly **doubled** the intended loss. These 3 stop fills alone cost -22.43 percentage points vs the -15.0% that was intended.

**Proposed fix:** Switch to `StopLimitOrderRequest` with a limit price slightly below the stop (e.g., stop - 2%). This caps the worst-case fill while still allowing some slippage tolerance. Risk: limit may not fill if the gap is too large. Consider making the limit spread configurable.

## Divergence quantification (2026-03-17 analysis)

Full analysis of Pair 1 (`lc_cbb9d749ff2f` sim vs `lc_3fbfd88b8077` Paper2, created 2026-03-13):

| Root Cause | Matched Trades | P&L Divergence (% points) |
|---|---|---|
| Stop fill gaps + different exit reasons | 7 | -17.11 |
| Alpaca-only exits (reconciliation, stop fills) | 7 | -14.09 |
| Stream handler race condition (`sell_fill`) | 34 | -13.32 |
| Entry slippage (even on same-reason trades) | 21 | -12.02 |
| Missed trades (sim-only, from under-allocation) | 23 | -8.81 |
| **TOTAL** | | **-65.35** |

**Key metrics:**
- Average entry slippage: +0.35% (Alpaca pays more)
- Worst entry slippage: +3.56% (VACH, regular hours)
- Sim avg P&L per matched trade: +0.36%
- Alpaca avg P&L per matched trade: -0.32%
- Holdings: sim 17/20, Alpaca 14/20

## Improvement opportunities (prioritized)

### High impact

1. **Fix sell_fill race condition** — Estimated recapture: ~22 percentage points (13.32 direct + 8.81 from under-allocation). This is the single highest-impact fix.
2. **Switch to stop-limit orders** — Estimated recapture: ~7 percentage points. Prevents catastrophic gap fills.

### Medium impact

3. **Tighten extended hours limit buffers** — Current 2-4% buffer is too generous. Consider 1-2%, or defer buys to market open. Extended hours entries had the worst slippage (2-3.5% on some stocks).
4. **Monitor under-allocation** — Add a dashboard metric or notification when a portfolio has fewer holdings than `max_pos` for extended periods. This symptom indicates the race condition or buy failures.

### Low impact / diagnostic

5. **Track exit reason concordance** — Periodic query comparing sim vs Alpaca exit reasons. High `sell_fill` rate indicates the race condition is still active.
6. **Track entry slippage distribution** — Flag entries with > 1% slippage for review.

## Gotchas

### Don't compare raw equity curves alone

The equity chart shows the combined effect of all trades. To find the root cause, you need to compare **individual trade outcomes** — which trades were the same, which were different, and why.

### Check `exit_reason` carefully

Exit reasons tell you whether a trade was closed by signal, stop, replacement, or reconciliation. If the Alpaca portfolio has many `sell_fill` exits, those are exits where the real reason was lost to the stream handler race condition. Cross-reference with the matched sim trade to see what the reason _should_ have been.

Known exit reason values: `signal`, `signal_tick`, `replaced`, `guard_stop`, `guard_target`, `guard_trail`, `sell_fill`, `alpaca_stop_fill (stop_price=X)`, `reconcile_confirmed_sell (order=X)`.

### Simulated portfolios aren't truly simulated

The "non-Alpaca" portfolio still tracks real prices for exit evaluation. The key difference is: no broker connection means no fill slippage, no order failures, and (critically) no `current_price` from broker for scoring. This can make features like replacement scoring silently non-functional. (See [issue 1](#1-replacement-scoring-asymmetry-found-2026-03-12-fixed) — now fixed.)

### Timestamps differ between tables

- `watch_json → $.exit.time` / `$.entry.time` — UTC ISO 8601
- `alpaca_transactions.created_at` — UTC (`func.now()`)
- Dashboard charts — ET (local)

When cross-referencing events, convert to the same timezone.

### The `entry_snapshot_id` is the key for matching

To match the "same trade" across two portfolios, join on `entry_snapshot_id AND symbol`. This works because both portfolios receive the same signal from the same snapshot.

## Writing a report

Every divergence analysis should produce a written report in `docs/reports/`. This creates a permanent record of findings that can be referenced later and compared against future analyses.

**File naming:** `docs/reports/DIVERGENCE-ANALYSIS-YYYY-MM-DD.md`

**Report should include:**
1. **Objective** — What question prompted the analysis
2. **Portfolios analyzed** — Config IDs, names, Alpaca accounts, key parameters, any config mismatches
3. **Methodology** — What data was examined and how
4. **Trade history overview** — Total trades, exit reason breakdowns, current holdings per portfolio
5. **Matched trade analysis** — Aggregate and per-trade comparison (entry slippage, exit reason concordance, P&L)
6. **Unmatched trades** — Trades in only one portfolio, with P&L impact
7. **Full divergence accounting** — Table quantifying each root cause's contribution in percentage points
8. **Root causes** — Detailed explanation of each, with code references
9. **Recommendations** — Prioritized by estimated P&L impact
10. **Reference back to this skills file** — Include a line like: *"Investigation process and SQL query patterns are documented in [docs/skills/PORTFOLIO-DIVERGENCE.md](../skills/PORTFOLIO-DIVERGENCE.md)."*

**Previous reports:**
- [DIVERGENCE-ANALYSIS-2026-03-17.md](../reports/DIVERGENCE-ANALYSIS-2026-03-17.md) — First analysis, found sell_fill race condition, stop-market gap risk, entry slippage patterns

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — General debugging, log sources, SQL patterns
- [ALPACA.md](ALPACA.md) — Order lifecycle, fill confirmation, reconciliation
- [TRADE-ANALYSIS.md](TRADE-ANALYSIS.md) — Performance analysis queries
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading implementation details
- [live_monitor.py](../../trader/online/live_monitor.py) — Exit strategies, replacement logic, portfolio manager
- [alpaca_broker.py](../../trader/market/alpaca_broker.py) — Order execution, fill slippage source
- [alpaca_stream.py](../../trader/market/alpaca_stream.py) — WebSocket fill handler, sell_fill race condition
