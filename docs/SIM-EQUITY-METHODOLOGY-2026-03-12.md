# Sim Portfolio Equity Methodology (2026-03-12)

## Purpose

This document records the current understanding of how simulated portfolio equity should be computed, why previous fixes were incomplete, how `tick_collector` fits into the system, and what future agents should verify before claiming the chart is "fixed."

This is not just a change log. It is the working methodology.

## Short Version

The portfolio equity chart is only trustworthy when:

1. Holdings are reconstructed from watch entry/exit history.
2. Realized PnL comes from correct exit prices.
3. Unrealized PnL is computed using a single, shared price-selection rule.
4. Live snapshots and backfill snapshots use the same valuation rule.
5. A validation pass shows zero diffs, or only explicitly accepted diffs.

The main failure so far has not been "math is hard." It has been that different parts of the system used different price sources and different timestamp semantics.

## Source of Truth

For simulated portfolios, the source of truth is:

1. `watches.watch_json`
   - Entry time
   - Entry price
   - Exit time
   - Exit price
   - Realized PnL percent
   - Status transitions
2. `live_configs.config_json`
   - `starting_capital`
   - allocation model / max positions
3. Market data used to mark open positions at a given timestamp

The chart itself is not source-of-truth data. It is a derived view.

## Correct Valuation Model

At any snapshot timestamp `T`, simulated portfolio equity should be:

```text
equity(T) = starting_capital + realized_dollar(T) + unrealized_dollar(T)
```

Where:

1. `realized_dollar(T)` is the sum of dollar PnL for all watches exited on or before `T`.
2. `unrealized_dollar(T)` is the sum of mark-to-market PnL for all watches still open at `T`.
3. Position sizing must use the same config allocation logic used when the portfolio was run.

The implementation pattern in `scripts/backfill_equity_full.py` is directionally correct:

1. Replay watch events in chronological order.
2. Maintain `holdings` keyed by `watch_id`.
3. At each snapshot timestamp, mark each open holding using the latest usable market price at-or-before that timestamp.

## What Went Wrong Historically

Several separate bugs compounded:

1. Replacement exits were often written with the entry price, producing fake `0.0%` realized PnL.
2. Sim equity snapshots were written with unrealized PnL missing or zero.
3. Early backfill logic used the wrong historical lookup semantics.
4. After backfill was corrected, the running app continued appending fresh `source="live"` rows using a different pricing path.

This is why the chart appeared "fixed" more than once and then later looked wrong again.

## Tables And Chart Must Agree

The `Positions`, `Recently Exited`, and `Exited` tables are not independent from the chart.

They are all different views over the same underlying watch state:

1. Open holdings table
   - current mark price
   - unrealized PnL percent
   - quantity
   - dollar PnL / value column
   - peak / trough PnL
2. Recently exited table
   - entry price
   - exit price
   - realized PnL percent
   - quantity
   - dollar PnL / value column
   - peak / trough PnL
3. Equity chart
   - starting capital
   - realized dollar PnL from exited watches
   - unrealized dollar PnL from open watches

If these views use different quantity logic or different price-selection logic, the UI will contradict itself even if each individual component appears plausible.

Future agents should treat this as one integrity problem, not a chart-only problem.

## Current Code Paths

### Live snapshots

`trader/online/live_monitor.py` writes periodic simulated equity snapshots in `_snapshot_equity()`.

Important details:

- It reconstructs current holdings by scanning watches.
- It computes realized PnL from exited watches.
- It computes unrealized PnL using `monitor.market.get_quotes(...)`.
- It writes rows with `source="live"`.

This means live valuation currently depends on quote snapshots, not on historical candle lookup.

Relevant code:

- `trader/online/live_monitor.py` around `_snapshot_equity()`

### Backfill

`scripts/backfill_equity_full.py` recomputes stored rows from watch history.

Important details:

- It loads existing rows from `portfolio_equity_snapshots`.
- It replays watch events up to each stored timestamp.
- It fetches Schwab 1-minute candles by date range.
- It marks holdings using the latest candle at-or-before each snapshot timestamp.

Relevant code:

- `scripts/backfill_equity_full.py`

### Positions / Recently Exited / Exited tables

`trader/web/app.py` enriches watches before rendering the tables.

Important details:

1. Open holdings
   - fetch current prices from `market.get_quotes(...)`
   - compute `current_price`
   - compute `unrealized_pnl`
   - backfill `qty` if missing
2. Exited / cooling rows
   - use stored `watch_json.exit.price`
   - use stored `watch_json.exit.realized_pnl_pct`
   - backfill `qty` if missing
   - display stored `peak_pnl_pct` and `trough_pnl_pct`

This means the tables depend partly on live quote enrichment at render time and partly on stored watch fields.

## Why Live And Backfill Still Disagree

They are not using the same market-price rule.

Today:

1. Live snapshots use quote snapshots.
2. Backfill uses Schwab 1-minute candles.

Those are similar, but they are not identical.

Example failure mode:

1. Live snapshot at `22:16:00` marks a position using the latest quote seen at that moment.
2. Backfill later reconstructs `22:16:00` using the close of the latest 1-minute candle at-or-before `22:16:00`.
3. The two values differ.
4. The chart tail changes after recompute.

That is exactly what was observed.

## Evidence From Audit

### Dry-run recompute still changed recent rows

On 2026-03-12, `scripts/backfill_equity_full.py --dry-run` was run against:

- `lc_026e07c6a0d8`
- `lc_c45e48da2b27`

Result:

- `lc_026e07c6a0d8`: `Updated 11 of 293 snapshots`
- `lc_c45e48da2b27`: `Updated 11 of 186 snapshots`

The mismatches were in the recent tail of `source="live"` rows.

This is direct evidence that the current stored chart is not yet fully consistent under one valuation methodology.

### Final validation state

After:

1. introducing shared valuation helpers in `trader/valuation.py`
2. aligning `trader/online/live_monitor.py` and `trader/web/app.py` to those helpers
3. changing minute marks to use the last completed minute in `trader/market/data_service.py`
4. restarting the app so the live writer picked up the new code
5. rewriting both portfolios with `scripts/backfill_equity_full.py`

the final validator results were:

- `lc_026e07c6a0d8`: `DRY RUN — Updated 0 of 330 snapshots`
- `lc_c45e48da2b27`: `DRY RUN — Updated 0 of 223 snapshots`

That is the first validated state where the stored chart history for both portfolios matched the current reconstruction rule exactly.

### Tick-vs-Schwab alignment audit

A direct audit compared recent minute-level prices from:

1. `tick_collector` Timescale `trades` data, aggregated to 1-minute last price
2. Schwab 1-minute candles

Observed:

- `symbols_compared`: 72
- `overlap_points`: 12,271
- `best_lag_nonzero_count`: 0
- `>0.5%` diff points: `0.5297%`
- `>1.0%` diff points: `0.1956%`
- `>2.0%` diff points: `0.0570%`

Interpretation:

1. The streams are aligned in time.
2. Most minute prices are very close.
3. A small number of isolated `tick_collector` last-trade values are obvious outliers.

Raw examples showed cases where the final trade in a minute was a tiny odd print far away from the Schwab candle close.

Conclusion:

- `tick_collector` is useful.
- Raw "last trade in minute" is not safe as a canonical equity-marking price without filtering.

## Status Of The Backfill Scripts

### `scripts/backfill_equity_full.py`

Status: improved, but not a full end-state by itself.

What is good:

1. Uses at-or-before lookup, not nearest-with-lookahead.
2. Tracks holdings by `watch_id`.
3. Reconstructs realized and unrealized PnL from transaction history.

What is still weak:

1. It recomputes existing rows, but live code can later append inconsistent rows.
2. It reported many stale lookups (`>60m`) for thin or after-hours names.
3. It uses Schwab-only minute candles and does not yet share a common valuation function with the live path.

Conclusion:

- It is a useful verifier and repair tool.
- It is not enough, by itself, to guarantee the chart stays correct.

### `scripts/backfill_exit_prices.py`

Status: still risky.

Current behavior:

1. It prefers `tick_collector` last trade at-or-before exit.
2. It only falls back to Schwab if tick lookup fails.

Why this is risky:

1. The tick audit found isolated bad last-trade prints.
2. The script does not currently apply a sanity filter before trusting that tick.

Conclusion:

- This script should not be considered fully safe until tick prices are filtered or cross-checked.
- A small number of replacement exits may remain unresolved if both tick and Schwab evidence near the exit time are too stale to justify a repair.

## Canonical Rule Recommendation

The system needs one canonical valuation rule used in both live and backfill.

The simplest acceptable rule is:

1. Snapshots are valued at minute resolution.
2. For each holding, use the latest usable price at-or-before the snapshot minute.
3. Use the same source priority in both live and backfill.
4. Apply the same outlier filter in both live and backfill.

That does not mean all trading decisions must be minute-based. It only means the equity chart must use one consistent marking rule.

As of 2026-03-12, shared valuation math now lives in:

- `trader/valuation.py`

That module centralizes:

1. quote price extraction
2. config-based position sizing
3. direction-aware PnL calculation
4. watch qty backfill
5. simulated portfolio rollup

This removed one major class of drift between the web tables and live snapshot code.

## How `tick_collector` Fits In

`tick_collector` is valuable, but it serves a different role depending on the problem.

### Good use cases

1. Faster-than-1-minute signal logic
   - VDD checks already use tick-based bucketing in `trader/online/live_monitor.py`
   - `tick_collector.vdd.get_vdd_bars(...)` supports sub-minute time buckets
   - `tick_collector.vdd.get_vdd_bars_by_trades(...)` supports trade-count bars ("tick clock")
2. Ranking features for live selection
   - The code already prefers tick-derived bars for ranking features before falling back to Schwab
3. Gap filling when Schwab is sparse
   - But only after aggregation / filtering, not raw last-trade usage

### Bad use cases

1. Using raw "last trade before timestamp" as the canonical mark for equity valuation
2. Using a single tiny outlier print to define exit price or portfolio value

### Practical rule

Use `tick_collector` as a high-frequency decisioning feed and as a secondary valuation source only after applying robustness rules.

## Faster-Than-One-Minute Decisions

Yes, this fits the approach.

The key is to separate:

1. Decision clock
2. Valuation clock

They do not need to be the same.

### Decision clock

For exits, replacements, or signal checks, sub-minute logic is fine and already supported:

- 30-second buckets
- trade-count buckets
- raw tick-driven VDD logic

This is where `tick_collector` is strongest.

### Valuation clock

For a portfolio equity chart, minute resolution is usually enough and much easier to validate.

That means:

1. The strategy may make decisions every few seconds.
2. But the chart can still mark the portfolio once per minute using a stable rule.

This is normal. The chart is for portfolio valuation, not microstructure replay.

### If true sub-minute equity is desired

Then use a separate high-frequency snapshot pipeline, not the same table/logic currently used for 1-minute-ish charts.

That higher-frequency pipeline should:

1. Use tick-derived bars or filtered mark prices.
2. Snap timestamps to a fixed cadence such as 5s, 10s, or 30s.
3. Apply outlier rejection before storing marks.
4. Be validated separately from the minute chart.

## Recommended Robust Price Policy

For portfolio valuation:

1. Primary: Schwab 1-minute candle close at-or-before the last completed snapshot minute
2. Secondary: `tick_collector` aggregated minute mark only if Schwab is missing
3. Tertiary: quote fallback only for live display, not for canonical persisted history

If `tick_collector` is used for valuation fallback, do not use raw last-trade directly. Use one of:

1. filtered last trade within the bucket
2. median / VWAP-like mark within the bucket
3. last trade only if it is within a sanity band of the bucket median or Schwab proxy

## What Must Be True Before Declaring The Chart Correct

Future agents should not say "fixed" until all of the following are true:

1. Exit-price repair is rerun with robust filtering.
2. Equity history is recomputed using the current canonical rule.
3. The live snapshot writer uses the same canonical rule.
4. A dry-run validator reports zero unexpected diffs.
5. Missing and stale price counts are reviewed and accepted.

As of this document revision, the remaining known gap is mostly price-source policy in the recent/post-close tail:

1. shared valuation math is now centralized
2. but historical backfill still marks from minute bars
3. while live/table views may still benefit from fresher quote or tick-derived marks after hours

## Recommended Validation Checklist

1. Run backfill validator in dry-run mode for the target configs.
2. Confirm `Updated 0` or document exactly why remaining diffs are acceptable.
3. Check the latest `source="live"` rows separately from historical rows.
4. Compare recent minute marks against Schwab and aggregated `tick_collector`.
5. Spot-check a few symbols with open positions and a few recently exited symbols.
6. Verify that the final chart endpoint returns the same series stored in the database.

## Bottom Line

The hard part is not the arithmetic. The hard part is making every part of the system answer the same valuation question in the same way.

`tick_collector` absolutely has a place in this system:

1. It is excellent for sub-minute trading decisions.
2. It is useful for valuation fallback if aggregated carefully.
3. It is not safe as a raw last-trade source for canonical equity history.

The correct architecture is:

1. one valuation rule for persisted equity history
2. one validator that proves stored history matches that rule
3. optional faster decisioning logic that can run on richer tick data without redefining the chart methodology

## Current Known Residuals

As of the final 2026-03-12 validation pass:

1. Both target portfolio charts validated to zero diffs under the current rule.
2. Open-position table math uses the same shared qty / PnL / mark logic as the chart path.
3. Three replacement exits remained unresolved because nearby market-price evidence was too stale:
   - `DKS`
   - `NKTR`
   - `WMK`

Those exits were left unchanged on purpose. The system should prefer "unknown / unverifiable" over fabricating a clean-looking exit price from stale data.
