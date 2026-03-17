# Portfolio Divergence Analysis Report — 2026-03-17

## Objective

Investigate why Alpaca paper-trading portfolios consistently underperform their paired simulated portfolios. Both portfolios in each pair receive the same signals and run the same exit strategies — the only difference is that one executes real orders on Alpaca while the other tracks prices without a broker. The goal is to determine whether the performance gap is purely "expected slippage" or whether there are bugs or design flaws in the Alpaca execution path that can be fixed.

## Portfolios analyzed

Two pairs were examined, each consisting of a simulated (non-Alpaca) portfolio and an Alpaca paper-trading portfolio created at the same time with the same config.

### Pair 1 (created 2026-03-13)

| | Simulated | Alpaca Paper2 |
|---|---|---|
| **Config ID** | `lc_cbb9d749ff2f` | `lc_3fbfd88b8077` |
| **Name** | volume_delta_divergence (live) | volume_delta_divergence (AlpacaPaper2) |
| **Alpaca account** | — | PA3FZ6VBEC6I |
| **Max positions** | 20 | 20 |
| **Replace min margin** | 0.05 | 0.05 |
| **Exit strategy** | volume_delta_divergence (lookback=40) | volume_delta_divergence (lookback=40) |
| **Guard stop** | 5.0% | 5.0% |
| **VDD tick overrides** | bucket_s=30, visible_only | bucket_s=30, visible_only |
| **Rank method** | tech_score | tech_score |

Configs are **identical** — any divergence is attributable to execution differences.

### Pair 2 (created 2026-03-16)

| | Simulated | Alpaca Paper3 |
|---|---|---|
| **Config ID** | `lc_51faaf1bc4c9` | `lc_ffc57a3f493f` |
| **Name** | volume_delta_divergence (live) | volume_delta_divergence (AlpacaPaper3) |
| **Alpaca account** | — | PA3KQMHEBKAK |
| **Max positions** | 10 | 10 |
| **Replace min margin** | 0.2 | 0.2 |
| **Exit strategy** | volume_delta_divergence (lookback=60) | volume_delta_divergence (lookback=60) |
| **Guard stop** | 4.0% | 4.0% |
| **VDD tick overrides** | `{}` (none) | bucket_s=60, visible_only |

Note: Pair 2 has a **config mismatch** — the simulated portfolio has no `live_overrides` (no tick-based VDD), while the Alpaca one does. This was acknowledged as intentional by the operator and not treated as a bug, though it does mean Pair 2's exit paths can diverge for non-slippage reasons.

## Methodology

1. **Config comparison** — Diffed `config_json` from the `live_configs` table for all four portfolios.
2. **Trade history summary** — Counted total trades, exits by reason, and current holdings per portfolio.
3. **Matched trade analysis** — Joined watches on `entry_snapshot_id + symbol` to compare the same trade across paired portfolios. This is the core technique: same signal, same symbol, same entry time — how did outcomes differ?
4. **Entry slippage measurement** — Compared entry prices on matched trades (sim uses fresh Schwab quote; Alpaca uses actual fill price).
5. **Exit reason concordance** — Categorized matched trades by whether exit reasons matched, differed, or were lost to `sell_fill`.
6. **Stop fill analysis** — Examined Alpaca stop orders that filled at prices far below the trigger.
7. **Unmatched trade analysis** — Identified trades that existed in only one portfolio and computed their P&L contribution.
8. **Alpaca transaction log review** — Cross-referenced `alpaca_transactions` for buy/sell order details, fill prices, and order types.
9. **Code review** — Read the exit paths in `live_monitor.py`, the stream handler in `alpaca_stream.py`, and the order execution in `alpaca_broker.py` to trace root causes.

## Trade history overview

### Pair 1

| | Simulated | Alpaca Paper2 |
|---|---|---|
| **Total trades** | 105 | 84 |
| **Closed** | 88 | 70 |
| **Current holdings** | 17 / 20 | 14 / 20 |
| **Replaced** | 57 | 12 |
| **Signal exits** | 24 | 11 |
| **Signal tick exits** | 4 | 2 |
| **Guard stop** | 2 | 2 |
| **Guard target** | 1 | 0 |
| **`sell_fill`** | 0 | 38 |
| **`alpaca_stop_fill`** | 0 | 3 |
| **Reconciliation** | 0 | 2 |

The Alpaca portfolio has 21 fewer total trades and 38 exits categorized as `sell_fill` — an exit reason that should not normally appear in large numbers. It indicates the real exit reason was lost.

### Pair 2

| | Simulated | Alpaca Paper3 |
|---|---|---|
| **Total trades** | 55 | 44 |
| **Closed** | 46 | 35 |
| **Current holdings** | 9 / 10 | 9 / 10 |
| **Replaced** | 38 | 5 |
| **Signal exits** | 7 | 0 |
| **`sell_fill`** | 0 | 30 |

Same pattern: 30 out of 35 Alpaca exits are `sell_fill`.

### P&L by exit reason

**Pair 1 — Alpaca Paper2:**

| Exit Reason | Count | Avg P&L % | Total P&L % |
|---|---|---|---|
| sell_fill | 38 | -0.145 | -5.51 |
| replaced | 12 | -0.722 | -8.66 |
| signal | 11 | +1.614 | +17.75 |
| signal_tick | 2 | +0.652 | +1.30 |
| guard_stop | 2 | -5.000 | -10.00 |
| alpaca_stop_fill | 3 | -7.476 | -22.43 |
| reconcile | 2 | -6.081 | -12.17 |

**Pair 1 — Simulated:**

| Exit Reason | Count | Avg P&L % | Total P&L % |
|---|---|---|---|
| replaced | 57 | +0.187 | +10.68 |
| signal | 24 | +1.112 | +26.70 |
| signal_tick | 4 | +0.736 | +2.94 |
| guard_stop | 2 | -5.000 | -10.00 |
| guard_target | 1 | +10.000 | +10.00 |

The simulated portfolio's replacements are slightly profitable on average (+0.19%), while Alpaca's are solidly negative (-0.72%). Signal exits are profitable in both, but the sim captures more of them (24 vs 11).

## Matched trade analysis (Pair 1)

62 trades were matched by `entry_snapshot_id + symbol` (both portfolios entered and exited the same position).

### Aggregate results

| Metric | Simulated | Alpaca |
|---|---|---|
| Avg entry slippage | — | +0.347% |
| Max entry slippage | — | +3.561% (VACH) |
| Avg P&L per trade | +0.362% | -0.323% |
| Total P&L (matched) | +22.41% | -20.04% |

The same 62 trades produced +22.41% in sim and -20.04% in Alpaca — a **42.45 percentage point** gap on matched trades alone.

### Exit reason concordance

| Category | Trades | Sim Avg P&L | Alpaca Avg P&L |
|---|---|---|---|
| **Same reason** (both portfolios used same exit) | 21 | +0.964% | +0.391% |
| **`sell_fill` race** (Alpaca reason lost to stream handler) | 34 | +0.285% | -0.107% |
| **Different reason** (genuinely different exit path) | 7 | -1.071% | -3.516% |

Even when exit reasons matched (21 trades), the Alpaca side underperformed by 0.57% per trade on average — this is pure entry + exit slippage.

### Cases where exit paths diverged

7 trades had genuinely different exit reasons (not just `sell_fill`):

| Symbol | Sim Reason | Sim P&L | Alpaca Reason | Alpaca P&L | Gap |
|---|---|---|---|---|---|
| NGS | guard_stop | -5.00% | alpaca_stop_fill | -9.48% | -4.48 |
| VACH | replaced | -1.12% | alpaca_stop_fill | -7.37% | -6.25 |
| VG | replaced | +0.69% | guard_stop | -5.00% | -5.69 |
| STRZ | replaced | -1.44% | guard_stop | -5.00% | -3.56 |
| E | replaced | -0.45% | replaced | -3.26% | -2.81 |
| MS | replaced | -0.96% | signal | +0.18% | +1.14 |
| ISRG | replaced | +0.81% | signal | +1.87% | +1.06 |
| PTEN | replaced | -0.47% | signal | +0.19% | +0.66 |

The top 4 rows are the killers: positions where entry slippage pushed the Alpaca price high enough that the stop-loss fired (or fired at a much worse price), while the sim exited via a milder replacement.

### Worst entry slippage cases

| Symbol | Sim Entry | Alpaca Entry | Slippage | Session | Notes |
|---|---|---|---|---|---|
| VACH | 10.67 | 11.05 | +3.56% | Regular | Hit stop at -7.37% |
| E | 51.25 | 52.75 | +2.93% | Extended | 2% limit buffer filled high |
| RL | 330.73 | 339.00 | +2.50% | Extended | Whole shares, thin liquidity |
| PSNY | 16.555 | 16.87 | +1.90% | Regular | |
| ZUMZ | 21.42 | 21.66 | +1.12% | Regular | First extended buy timed out |

Extended hours buys (RL, E) used limit orders with a 2-4% buffer above ask price and filled near the top of that buffer.

## Unmatched trades

### Sim-only trades (Pair 1): 23 trades, +8.81% total P&L

These are trades the simulated portfolio entered but Alpaca did not. They include profitable positions like FUTU (+5.97%), CRM (+2.03%), ABX (+1.90%), and BMRC (+1.79%). These were entered in sim because it had available slots — the Alpaca portfolio, running under-allocated due to the race condition, didn't have room for them when they arrived.

### Alpaca-only trades (Pair 1): 7 trades, -14.09% total P&L

These include 2 reconciliation forced-exits (STRZ at -6.49%, VG at -5.68%) and other trades that diverged early in portfolio composition. Reconciliation exits happen when the system detects an Alpaca position that doesn't match the watch DB, and force-closes it — typically at a loss.

## Full divergence accounting (Pair 1)

| Root Cause | Trades | P&L Divergence (% points) |
|---|---|---|
| Stop fill gaps + different exit paths | 7 | -17.11 |
| Alpaca-only exits (reconciliation, stop fills) | 7 | -14.09 |
| Stream handler race condition (`sell_fill`) | 34 | -13.32 |
| Entry slippage (on same-reason trades) | 21 | -12.02 |
| Missed trades (sim-only, from under-allocation) | 23 | -8.81 |
| **TOTAL** | | **-65.35** |

## Pair 2 summary

Pair 2 has only 1 day of data (created 2026-03-16). Results are directionally consistent:

- 30 matched trades, avg entry slippage +0.093%, total divergence -5.06 percentage points
- Same `sell_fill` dominance pattern (30/35 exits)
- Both portfolios are negative (sim -4.48%, Alpaca -9.54%)
- The `live_overrides` config mismatch (no VDD tick on sim) makes direct comparison less clean

## Root causes

### 1. Stream handler race condition (CRITICAL)

**Impact: ~22 percentage points** (13.32 direct + 8.81 from under-allocation)

The WebSocket stream handler (`alpaca_stream.py:_handle_sell_fill`) receives fill confirmations faster than the monitor's `wait_for_fill()` polling loop (0.5s interval). When the stream handler records the exit first:

- The exit reason is recorded as `"sell_fill"` instead of the real reason
- The status changes from `"holding"` to `"exited"`
- The monitor's subsequent `update_watch_if_current_status(expected_status="holding")` fails
- For replacement exits: `_exit_victim()` returns `False`, so the replacement buy **never executes**
- The portfolio slot remains empty until the next new signal arrives (not a replacement)

This affected **49% of all Alpaca exits** in Pair 1 (34 out of 70).

### 2. Stop-market order gap fills

**Impact: ~7 percentage points** on the 3 worst fills

The `set_stop()` method uses `StopOrderRequest` — a stop-market order. When triggered during a gap or volatile moment, it becomes a market order and can fill far below the stop price. NGS filled at -9.48% against a -5.0% intended stop — nearly doubling the loss.

### 3. Entry price slippage

**Impact: ~12 percentage points** on same-reason matched trades

Average slippage of +0.35% per entry. Extended hours entries are worst (2-4% limit buffer). The 2% buffer above ask price (or 4% above last trade when ask is unavailable) is too generous — some fills occur near the buffer maximum.

### 4. Cascading stop triggers from slippage

**Impact: included in "different exit paths" above**

Higher entry prices move the stop-loss trigger to a higher absolute price. Two positions (STRZ, VG) hit the -5% stop on Alpaca that the sim avoided entirely — sim exited via replacement at small losses (-1.44%, +0.69%).

### 5. Chronic under-allocation

**Impact: ~8.81 percentage points** of missed profitable trades

Because replacement buys fail silently (root cause 1), the Alpaca portfolio runs with fewer positions than intended (14/20 vs sim's 17/20). Empty slots are eventually filled by new signals, but the portfolio misses replacement opportunities that the sim captures.

## Recommendations

### Priority 1: Fix the sell_fill race condition

**Approach:** Store a `pending_exit_reason` field on the watch before submitting the sell order. The stream handler should use this field if present instead of defaulting to `"sell_fill"`. Additionally, in `_exit_victim()`, if `update_watch_if_current_status` fails because the stream handler already recorded the exit, check for this case and return `True` so the replacement buy proceeds.

**Estimated recapture:** ~22 percentage points (the combined direct and under-allocation impact).

### Priority 2: Switch to stop-limit orders

**Approach:** Replace `StopOrderRequest` with `StopLimitOrderRequest` in `set_stop()`. Set the limit price slightly below the stop (e.g., stop - 2%) to cap worst-case fills while allowing reasonable slippage tolerance. Make the limit spread configurable.

**Risk:** If the stock gaps below the limit, the order won't fill and the position remains open. The monitor's `_check_holding()` would eventually catch this and exit via a market sell, but there could be additional losses during the gap.

**Estimated recapture:** ~7 percentage points from preventing catastrophic fills.

### Priority 3: Tighten extended hours limit buffers

**Approach:** Reduce the limit buffer from 2% (with ask) / 4% (without ask) to 1% / 2%, or defer buys to market open entirely. Extended hours fills with thin liquidity and wide spreads produced the worst slippage cases.

**Alternative:** Queue buy decisions made during extended hours and execute them at the next market open with market orders, which typically fill closer to the quoted price during regular session liquidity.

### Priority 4: Monitor under-allocation

**Approach:** Add a periodic check or dashboard metric that alerts when a portfolio has significantly fewer holdings than `max_pos` for an extended period. This is a symptom detector for the race condition and buy failures.

---

*Analysis performed 2026-03-17. Data covers Pair 1 from 2026-03-13 to 2026-03-17 (2 trading days) and Pair 2 from 2026-03-16 to 2026-03-17 (1 trading day).*
