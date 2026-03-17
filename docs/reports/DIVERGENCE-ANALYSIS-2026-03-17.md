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

Note: Pair 2 has a **config mismatch** — the simulated portfolio has no `live_overrides`, while the Alpaca one enables tick-based VDD with different `bucket_s`, `min_trades_per_bucket`, and `poll_interval_s`. This was acknowledged as intentional by the operator and not treated as a bug, but it does make Pair 2 supporting evidence rather than a clean apples-to-apples comparison.

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
| **`sell_fill` / sell-state handoff failure** | 34 | +0.285% | -0.107% |
| **Different reason** (genuinely different exit path) | 7 | -1.071% | -3.516% |

Even when exit reasons matched (21 trades), the Alpaca side underperformed by 0.57% per trade on average. That is the best estimate of "real execution tax" on trades where the portfolios still followed the same path.

The 34 `sell_fill` cases do point to a stream/monitor race, but the broader issue is that sell ownership is split across multiple paths:

- `live_monitor.py` submits the sell and then waits for fill
- `alpaca_stream.py` can record the exit first as `sell_fill`
- `_exit_victim()` treats the stale status update as failure and aborts the replacement buy
- reconciliation can later adopt and force-exit residual positions created by these interrupted flows

So `sell_fill` is not just a reporting artifact. It is a visible symptom of a larger sell-state coordination defect.

### Cases where exit paths diverged

7 trades had genuinely different exit reasons (not just `sell_fill`):

| Symbol | Sim Reason | Sim P&L | Alpaca Reason | Alpaca P&L | Gap |
|---|---|---|---|---|---|
| NGS | guard_stop | -5.00% | alpaca_stop_fill | -9.48% | -4.48 |
| VACH | replaced | -1.12% | alpaca_stop_fill | -7.37% | -6.25 |
| VG | replaced | +0.69% | guard_stop | -5.00% | -5.69 |
| STRZ | replaced | -1.44% | guard_stop | -5.00% | -3.56 |
| MS | replaced | -0.96% | signal | +0.18% | +1.14 |
| ISRG | replaced | +0.81% | signal | +1.87% | +1.06 |
| PTEN | replaced | -0.47% | signal | +0.19% | +0.66 |

The top 4 rows are the killers: positions where entry slippage and stop behavior pushed the Alpaca side into a harsher exit path than the sim.

`E` is worth calling out separately even though the exit reason matched (`replaced` on both sides): it still showed a large P&L gap (-0.45% sim vs -3.26% Alpaca), which is a reminder that same-reason trades can diverge materially from execution alone.

### Worst entry slippage cases

| Symbol | Sim Entry | Alpaca Entry | Slippage | Session | Notes |
|---|---|---|---|---|---|
| VACH | 10.67 | 11.05 | +3.56% | Regular | Hit stop at -7.37% |
| E | 51.25 | 52.75 | +2.93% | Extended | 2% limit buffer filled high |
| RL | 330.73 | 339.00 | +2.50% | Extended | Whole shares, thin liquidity |
| PSNY | 16.555 | 16.87 | +1.90% | Regular | |
| ZUMZ | 21.42 | 21.66 | +1.12% | Regular | First extended buy timed out |

Extended hours buys (RL, E) used limit orders with a 2-4% buffer above ask price and filled high within that allowed range.

However, entry slippage is **not** purely an extended-hours problem. The worst closed matched case in Pair 1 was a **regular-hours** buy (`VACH`, +3.56%), and other regular-session names (`PSNY`, `ZUMZ`) also showed meaningful slippage. The issue is broader: thin liquidity and aggressively marketable execution can hurt entries in both sessions.

## Unmatched trades

### Sim-only trades (Pair 1): 23 trades, +8.81% total P&L

These are trades the simulated portfolio entered but Alpaca did not. They include profitable positions like FUTU (+5.97%), CRM (+2.03%), ABX (+1.90%), and BMRC (+1.79%).

The best explanation is not simply "Alpaca had no room." It is a mix of:

- replacement exits that did not complete the replacement buy after the sell path broke
- temporary under-allocation after failed sell handoff
- composition drift from prior sell/reconcile anomalies
- ordinary buy failures on some Alpaca names

### Alpaca-only trades (Pair 1): 7 trades, -14.09% total P&L

These include 2 reconciliation forced-exits (STRZ at -6.49%, VG at -5.68%) and other trades that diverged early in portfolio composition. The reconciliation losses are especially important because they show that the sell-path defect can cascade into orphan adoption and later forced exits, not just mislabeled `sell_fill` records.

## Directional divergence accounting (Pair 1)

This table is directional, not a perfect additive decomposition. Some categories overlap causally, and it does **not** fully reconcile to the total closed-trade P&L gap on its own.

| Root Cause | Trades | P&L Divergence (% points) |
|---|---|---|
| Stop fill gaps + different exit paths | 7 | -17.11 |
| Alpaca-only exits (reconciliation, stop fills) | 7 | -14.09 |
| Sell-state handoff failure (`sell_fill`, missed replacement continuation) | 34 | -13.32 |
| Entry slippage (on same-reason trades) | 21 | -12.02 |
| Sim-only trades (under-allocation + composition drift) | 23 | -8.81 |
| **TOTAL** | | **-65.35** |

## Pair 2 summary

Pair 2 has only 1 day of data (created 2026-03-16). Results are directionally consistent:

- 30 matched trades, avg entry slippage +0.093%, total divergence -5.06 percentage points
- Same `sell_fill` dominance pattern (30/35 exits overall; 25/30 in matched closed trades)
- Both portfolios are negative (sim -4.48%, Alpaca -9.54%)
- The config mismatch makes direct comparison less clean, so Pair 2 should be treated as corroborating evidence rather than primary proof

## Root causes

### 1. Sell-side state fragmentation (CRITICAL)

**Impact: ~22 percentage points** (13.32 direct + 8.81 from under-allocation)

The WebSocket stream handler (`alpaca_stream.py:_handle_sell_fill`) does race with the monitor's `wait_for_fill()` polling loop (0.5s interval), but that is only one part of the defect.

The broader problem is that sell execution and sell state are owned by multiple disconnected code paths:

- `live_monitor.py:_check_holding()` and `_exit_victim()` submit sells directly and then poll
- `alpaca_stream.py:_handle_sell_fill()` can exit the watch first with reason `"sell_fill"`
- the later monitor update fails because status is no longer `"holding"`
- for replacement exits, `_exit_victim()` returns `False`, so the replacement buy never executes
- reconciliation can then adopt leftover positions and later force-exit them, creating extra losses (`STRZ`, `VG`)

This affected **49% of all Alpaca exits** in Pair 1 (34 out of 70) and appears to be the single biggest fixable source of divergence.

### 2. Stop-market order gap fills

**Impact: ~7 percentage points** on the 3 worst fills

The `set_stop()` method uses `StopOrderRequest` — a stop-market order. When triggered during a gap or volatile moment, it becomes a market order and can fill far below the stop price. NGS filled at -9.48% against a -5.0% intended stop — nearly doubling the loss.

### 3. Entry price slippage

**Impact: ~12 percentage points** on same-reason matched trades

Average slippage was +0.35% per entry on matched Pair 1 trades. Extended-hours entries are clearly vulnerable because the 2% buffer above ask (or 4% above last trade when ask is unavailable) is generous for thin books.

But the data does not support blaming this only on extended-hours logic. Several large slippage events happened in regular hours too, so the root cause is broader than one buffer constant: live execution is paying a real liquidity tax that the simulated path does not model.

### 4. Cascading stop triggers from slippage

**Impact: included in "different exit paths" above**

Higher entry prices move the stop-loss trigger to a higher absolute price. Two positions (STRZ, VG) hit the -5% stop on Alpaca that the sim avoided entirely — sim exited via replacement at small losses (-1.44%, +0.69%). VACH and NGS show the same dynamic plus stop-market gap risk.

### 5. Chronic under-allocation and composition drift

**Impact: ~8.81 percentage points** of missed profitable trades

Because replacement buys fail silently (root cause 1), the Alpaca portfolio often runs with fewer positions than intended (14/20 vs sim's 17/20). Empty slots are eventually filled by new signals, but the portfolio misses replacement opportunities that the sim captures. On top of that, orphan adoption and reconciliation exits change the live portfolio composition in ways the sim never experiences.

### 6. Simulated portfolio still understates live execution friction

This is not a bug in the Alpaca path, but it matters for interpretation. Even when the exit reason matched, Alpaca still lagged the sim by 0.57 percentage points per trade on average. That is evidence that some portion of the divergence is genuine live execution tax and should eventually be modeled in the simulated portfolio if the goal is a fair benchmark.

## Recommendations

### Priority 1: Centralize the sell workflow and state ownership

**Approach:** Move all non-stop sells through one broker-backed path and persist explicit sell intent before submission. At minimum:

- store `pending_exit_reason` on the watch before the sell is sent
- add an `exit_in_flight` marker or equivalent persisted state
- make the stream handler use the pending reason instead of defaulting to `"sell_fill"`
- if the stream handler already exited the watch, allow `_exit_victim()` to treat that as success so the replacement buy still proceeds
- prevent reconciliation from adopting/force-exiting positions that are already in an active sell workflow

**Why this matters:** This addresses both the mislabeled exits and the bigger issue: broken replacement continuation and reconciliation damage.

### Priority 2: Re-evaluate stop order type, but do not blindly switch to stop-limit

**Approach:** Make stop behavior configurable and test carefully. `StopLimitOrderRequest` can cap catastrophic fills, but it can also fail to execute during a true gap. A safer rollout would be:

- add configurable stop-limit support behind a flag
- make the stop-limit spread symbol/session aware rather than fixed
- add explicit fallback handling if the stop triggers but does not fill

**Risk:** A missed stop execution can be worse than a bad fill. This should be treated as a risk-management change, not just a performance optimization.

### Priority 3: Add entry liquidity guards for both regular and extended hours

**Approach:** Tighten extended-hours limit buffers, but also add broader entry safeguards:

- reject entries when spread or quote quality is poor
- consider marketable-limit buys during regular hours instead of unconstrained market orders
- reduce extended-hours buy buffers or defer some names to the next open
- flag and review entries with slippage above a threshold (for example 1%)

The data shows that regular-hours execution also produces material slippage, so this should not be framed as only an after-hours tweak.

### Priority 4: Monitor under-allocation and orphan/reconcile events

**Approach:** Add metrics or alerts for:

- holdings materially below `max_pos`
- high `sell_fill` rates
- orphan adoption events
- reconciliation force exits
- entry slippage outliers

These are the operational symptoms of the same underlying defect.

### Priority 5: Make the simulated benchmark more execution-aware

**Approach:** Once the live-path bugs are fixed, consider adding modest execution friction to the simulated portfolio:

- entry slippage assumptions by session/liquidity
- worse stop fills for gap scenarios
- optional missed-fill behavior in extended hours

This will not fix live trading, but it will make the comparison more honest.

## Verification plan for proposed fixes

Any fix in this area should be judged by **before/after portfolio behavior**, not just by code review or unit tests. The following checks are the minimum bar.

### Verify Priority 1: centralized sell workflow / sell-state ownership

**What should change**

- `sell_fill` exits should collapse from "common" to "rare fallback"
- replacement exits should no longer leave empty slots when the sell itself succeeded
- orphan adoption / reconciliation force-exit events caused by interrupted sells should disappear or drop sharply

**How to verify**

1. **Unit/integration tests**
   - stream fill arrives before monitor status update: watch should exit with the intended reason, not `sell_fill`
   - replacement exit where stream wins the race: replacement flow should still proceed
   - reconciliation should skip positions marked `exit_in_flight`
2. **SQLite checks after deployment**
   - query `watches` for `exit.reason = 'sell_fill'` by day and by config
   - query `alpaca_transactions` for `reconcile_orphan_adopted` and `reconcile_force_exit` after normal sells
   - compare holdings vs `max_pos` through the day to confirm replacement slots refill immediately
3. **Portfolio comparison**
   - rerun the matched-trade analysis on the next clean portfolio pair
   - expected outcome: big drop in `sell_fill`, fewer sim-only missed trades, fewer Alpaca-only reconcile losses

### Verify Priority 2: stop behavior changes

**What should change**

- catastrophic stop overruns should become less frequent
- any new "stop triggered but not filled" failure mode must be visible and bounded

**How to verify**

1. **Tests**
   - stop-limit order uses configured spread
   - unfilled stop-limit path raises an explicit recovery/fallback signal
2. **SQLite checks**
   - compare intended stop price vs actual fill price in `alpaca_transactions`
   - count stops with slippage worse than thresholds such as 1%, 2%, 3%
3. **Operational guardrail**
   - add an alert for triggered stop orders that remain open/unfilled beyond a short timeout

### Verify Priority 3: entry liquidity guards

**What should change**

- tail slippage should improve, especially on illiquid names
- there may be more skipped buys or more deferred extended-hours entries

**How to verify**

1. **Tests**
   - poor quote quality / wide spread causes a skip or defer decision
   - extended-hours pricing uses the tighter buffer or defer rule as configured
2. **SQLite checks**
   - track entry slippage distribution before vs after
   - specifically monitor rates of entry slippage above 1% and above 2%
3. **Tradeoff check**
   - compare fewer bad fills vs increased miss rate so the fix is not judged only on slippage

### Verify Priority 4: monitoring and alerts

**What should change**

- operators should know quickly when the system drifts into the old failure pattern

**How to verify**

1. Confirm alerts fire for:
   - holdings materially below `max_pos`
   - large `sell_fill` counts
   - orphan adoption / force-exit events
   - entry slippage outliers
2. Confirm the alerts are low-noise enough to be actionable

### Verify Priority 5: execution-aware simulation

**What should change**

- the shadow benchmark should remain better than live trading, but not by an unrealistic amount on same-path trades

**How to verify**

1. Backtest or replay matched trades with the slippage model enabled
2. Compare same-reason trade gaps before vs after
3. Make sure the simulation is calibrated from observed live fills rather than arbitrary constants

---

*Analysis performed 2026-03-17. Data covers Pair 1 from 2026-03-13 to 2026-03-17 (2 trading days) and Pair 2 from 2026-03-16 to 2026-03-17 (1 trading day).*

*Investigation process and SQL query patterns are documented in [docs/skills/PORTFOLIO-DIVERGENCE.md](../skills/PORTFOLIO-DIVERGENCE.md).*
