# Live Trading Implementation Plan

> From backtest simulation to paper trading via Alpaca API.
>
> Last updated: 2026-03-04

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Best-Performing Backtest Configuration](#best-performing-backtest-configuration)
3. [Architecture: Backtest vs Live Trading](#architecture-backtest-vs-live-trading)
4. [Volume Delta: Backtest vs Real-Time Analysis](#volume-delta-backtest-vs-real-time-analysis)
5. [Alpaca Trading API Integration](#alpaca-trading-api-integration)
6. [Implementation Plan](#implementation-plan)
7. [Risk Controls](#risk-controls)
8. [Data Source Differences](#data-source-differences)
9. [Testing Strategy](#testing-strategy)
10. [Open Questions](#open-questions)

---

## Executive Summary

This document outlines the plan to add live paper trading to the alpaca-news system
using the Alpaca Trading API. The goal is to faithfully replicate the backtest's
best-performing configuration in a real-time environment, starting with paper trading
to validate before switching to live money.

**Key findings:**

- The existing Watch system already tracks position lifecycles — we need to add
  **order execution** and **real-time exit monitoring** on top of it
- Volume delta (critical for the VDD exit strategy) uses an **inter-bar tick rule**
  approximation in backtests. Real-time tick data is more accurate but produces
  **different signal timing** (~60% of signals align within ±5 bars, ~75% within ±15 bars)
- Alpaca's API supports all needed order types: market, limit, stop, trailing stop,
  and bracket orders (combined entry + stop + target)
- Paper trading uses the **same API** as live — only a flag change to switch

---

## Best-Performing Backtest Configuration

From the screenshot of the best-performing parameter set (updated 2026-03-05):

### Exit Strategy: Volume Delta Divergence

| Parameter | Value | Description |
|-----------|-------|-------------|
| Strategy | `volume_delta_divergence` | Exit on price-high + delta-decline divergence |
| Lookback | **80 bars** | Rolling window for divergence detection |

### Guard System

| Parameter | Value | Description |
|-----------|-------|-------------|
| Guard Stop % | **5%** | Hard stop loss below entry |
| Guard Target % | **0%** (disabled) | No automatic take-profit |

### Global Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| Market Close | **16:00** | Regular hours only (extended hours OFF) |
| Min Hold | **5 bars** | 5 minutes before exit checks |
| Price Delay | **5 min** | Entry price captured 5 min after signal |
| Cost BPS | **0.5** | 0.005% transaction cost estimate |

### Allocation: Max Positions

| Parameter | Value | Description |
|-----------|-------|-------------|
| Strategy | `max_positions` | Limit concurrent positions |
| Max Concurrent | **20** | Maximum simultaneous positions |
| When Full | **Replace weakest** | Replace position with lowest unrealized P&L |
| Rank Method | **Unrealized P&L** | Rank by momentum (current P&L) |
| Starting Capital | **$100,000** | Portfolio simulation starting amount |

### Snapshot Filters

| Filter | Value |
|--------|-------|
| Confidence Min | **+85** (bullish ≥ 85%) |

### Performance Metrics (from backtest)

These are the targets our live trading should approximate:
- Win rate, average P&L, Sharpe ratio from the backtest
- The backtest computes annualized returns via two methods (time-weighted and equity-curve)

---

## Architecture: Backtest vs Live Trading

### What the Backtest Does (offline)

```
Snapshot entries ──→ Load 1-min OHLCV bars ──→ Walk bars forward ──→ Check exit conditions
                                                    │                       │
                                                    ├── Guard stop?         ├── VDD signal?
                                                    ├── Guard target?       ├── Time limit?
                                                    └── Min hold elapsed?   └── Still open?
```

All data is historical. The entire walk happens in milliseconds.

### What Live Trading Must Do (real-time)

```
News trigger ──→ Pipeline ──→ BUY signal ──→ Submit BUY order ──→ Monitor position
                                                                        │
                                              ┌─────────────────────────┘
                                              │
                                              ▼
                                    Every 1-min bar (or tick):
                                    ├── Check guard stop (price < entry * 0.90)
                                    ├── Check VDD signal (new high + delta declining)
                                    ├── Check max hold (240 min / configurable)
                                    └── If exit triggered → Submit SELL order
```

### Key Differences

| Aspect | Backtest | Live Trading |
|--------|----------|-------------|
| **Data** | Historical OHLCV bars | Real-time bars + optional tick data |
| **Execution** | Instantaneous, exact prices | Latency, slippage, partial fills |
| **Entry price** | Bar close at entry_time + delay | Actual fill price from Alpaca |
| **Exit price** | Bar close/low/high at signal | Actual fill price from Alpaca |
| **Position sizing** | Simulated capital allocation | Real account buying power |
| **Concurrency** | Walk-forward simulation | Actual concurrent positions |
| **Guards** | Checked per bar retroactively | Alpaca stop orders OR monitored per bar |
| **Cost** | Flat BPS deduction | Actual commission + spread |
| **Volume delta** | Inter-bar tick rule on OHLCV | Same (1-min bars) OR tick-level accumulation |

---

## Volume Delta: Backtest vs Real-Time Analysis

### Test Results (2026-03-04)

We ran a comprehensive comparison of volume delta computation methods on recent
5-day data for SPY, AAPL, and TSLA.

#### Method Comparison

The backtest uses the **inter-bar tick rule**: each bar's entire volume is classified
as uptick or downtick based on whether close > or < previous bar's close.

Alternative methods (proxies for what tick-level real-time data would show):
- **Close Position Formula**: `buy_vol = V * (C-L)/(H-L)` — distributes volume
  continuously based on where close falls in the bar range
- **Body Delta**: `delta = V * (C-O)/(H-L)` — uses candle body

#### Key Findings

**1. Per-bar direction agreement: ~81-86%**

| Symbol | Inter-bar vs Close-Position | Inter-bar vs Body Delta |
|--------|----------------------------|------------------------|
| SPY | 81.2% | 96.7% |
| AAPL | 85.0% | 87.4% |
| TSLA | 85.6% | 89.8% |

About **15-19% of bars** get classified in the opposite direction by the close-position
formula vs the inter-bar tick rule. These are bars where the close is in the opposite
half of the H-L range from the close-to-close direction.

**2. Rolling imbalance correlation: 0.64-0.79**

| Symbol | Correlation | Direction Agreement |
|--------|-------------|-------------------|
| SPY | 0.6412 | 73.1% |
| AAPL | 0.6589 | 76.0% |
| TSLA | 0.7882 | 78.2% |

The rolling 30-bar imbalance between methods correlates moderately. Direction
(bullish vs bearish) agrees about 73-78% of the time.

**3. VDD signal overlap: ~40-85% of signals shared**

| Symbol | Both Agree | Only Inter-bar | Only Close-Position |
|--------|-----------|---------------|-------------------|
| SPY | 12 | 15 | 28 |
| AAPL | 12 | 16 | 23 |
| TSLA | 38 | 7 | 22 |

The close-position formula generates **more** VDD signals (because its cumulative
delta curve is smoother and more sensitive to small divergences).

**4. VDD signal timing: median 0 bars, but outliers up to ±115 bars**

When signals do overlap, the median timing difference is 0 bars (same bar).
But ~40% of inter-bar signals don't have a matching close-position signal within
±5 bars, suggesting real-time tick data could trigger exits at meaningfully
different times.

**5. Cumulative delta curve divergence: significant**

| Symbol | Correlation | Normalized MAE |
|--------|-------------|---------------|
| SPY | 0.753 | 0.338 |
| AAPL | 0.285 | 0.733 |

For AAPL, the two cumulative delta curves are quite different (correlation only 0.29),
meaning the VDD strategy would behave very differently with tick-level data.

**6. Bar direction changes: ~48-52% of consecutive bars flip**

About half of all consecutive bars change direction. This is a fundamental property
of 1-minute data — it's very noisy. The inter-bar tick rule's binary classification
amplifies this noise.

#### Implications for Live Trading

1. **Start with bar-based approach** (same as backtest) for signal parity
2. **Run tick-level accumulation in shadow mode** to gather comparison data
3. **The guards are unaffected** — they use price levels, not volume delta
4. **The VDD lookback parameter (30)** was tuned on bar data — it may need
   recalibration for tick-level data
5. **TSLA** (high volatility) shows better method agreement than **AAPL** —
   the approximation error varies by symbol characteristics

#### Recommended Approach: Phased Volume Delta

**Phase 1 (Launch):** Use 1-minute bars + inter-bar tick rule (identical to backtest).
Data source: Alpaca or Schwab 1-min bars via existing infrastructure.

**Phase 2 (Shadow):** Add Alpaca `StockDataStream` tick-level accumulation running
in parallel. Log tick-level VDD signals alongside bar-based signals.

**Phase 3 (Calibrate):** After 2+ weeks of shadow data, analyze:
- How often do tick-level signals fire before bar-level signals?
- Does tick-level data improve exit timing (better P&L)?
- Does the lookback parameter need adjustment?

**Phase 4 (Switch):** If tick-level signals show improvement, switch primary
signal source. Keep bar-level as fallback.

---

## Alpaca Trading API Integration

### SDK & Authentication

```python
# Already in pyproject.toml: "alpaca-py"
from alpaca.trading.client import TradingClient
from alpaca.data.live import StockDataStream
from alpaca.trading.stream import TradingStream

# Paper trading (start here)
client = TradingClient(api_key, secret_key, paper=True)
# Live trading (later): paper=False + different credentials
```

Environment variables needed:
```
ALPACA_API_KEY=...          # Already used for news
ALPACA_SECRET_KEY=...       # Already used for news
ALPACA_PAPER=true           # New: toggle paper/live
```

### Order Types We Need

#### 1. Entry: Market Order

```python
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

order = MarketOrderRequest(
    symbol="AAPL",
    notional=position_size,       # Dollar amount (5% of capital)
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
)
result = client.submit_order(order_data=order)
# Record: result.filled_avg_price as actual entry price
```

#### 2. Guard Stop: Stop Order (Server-Side)

Two approaches for the 10% guard stop:

**Option A: Alpaca stop order (set-and-forget)**
```python
from alpaca.trading.requests import StopOrderRequest

stop_order = StopOrderRequest(
    symbol="AAPL",
    qty=position.qty,
    side=OrderSide.SELL,
    time_in_force=TimeInForce.GTC,
    stop_price=round(entry_price * 0.90, 2),  # 10% below entry
)
client.submit_order(order_data=stop_order)
```

**Option B: Bracket order (entry + stop combined)**
```python
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
from alpaca.trading.enums import OrderClass

order = MarketOrderRequest(
    symbol="AAPL",
    notional=position_size,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
    order_class=OrderClass.OTO,
    stop_loss=StopLossRequest(stop_price=round(entry_price * 0.90, 2)),
)
```

**Recommendation:** Option A (separate stop order) gives us more control.
We can cancel/replace it if VDD fires first.

#### 3. VDD Exit: Market Sell Order

When VDD signal fires:
```python
client.close_position(symbol)  # Close entire position at market
```

#### 4. Real-Time Monitoring

```python
# Market data stream (1-min bars for VDD computation)
data_stream = StockDataStream(api_key, secret_key)

async def on_bar(bar):
    # Update VDD computation for this symbol
    # Check if VDD signal fires → exit

data_stream.subscribe_bars(on_bar, *watched_symbols)

# Trade update stream (order fills, rejects)
trade_stream = TradingStream(api_key, secret_key, paper=True)

async def on_trade_update(data):
    if data.event == 'fill':
        # Record actual fill price
        # Update Watch entry/exit accordingly

trade_stream.subscribe_trade_updates(on_trade_update)
```

### Alpaca Data Feed

| Plan | Data | Cost |
|------|------|------|
| Basic (free) | IEX only (~2-5% of trades) | $0 |
| Algo Trader Plus | Full SIP (all exchanges) | ~$99/mo |

**For paper trading:** IEX data is sufficient for testing.
**For live trading:** Full SIP recommended for accurate volume data.

**Important:** IEX-only data means volume numbers will differ from Schwab/yfinance
(which aggregate from all exchanges). This affects VDD signal accuracy.

---

## Implementation Plan

### Phase 0: Infrastructure Setup

| Task | Description | Files |
|------|-------------|-------|
| 0.1 | Add Alpaca paper trading credentials to `.env` | `.env` |
| 0.2 | Create `AlpacaTradingClient` wrapper | `trader/market/alpaca_client.py` |
| 0.3 | Add config: `ALPACA_PAPER=true`, position size params | `trader/config.py` |

### Phase 1: Order Execution Layer

| Task | Description | Files |
|------|-------------|-------|
| 1.1 | Implement `enter_position(symbol, dollars)` | `trader/trading/executor.py` |
| 1.2 | Implement `exit_position(symbol, reason)` | `trader/trading/executor.py` |
| 1.3 | Implement `set_guard_stop(symbol, stop_price)` | `trader/trading/executor.py` |
| 1.4 | Order fill tracking + slippage logging | `trader/trading/executor.py` |
| 1.5 | Integration tests with paper trading | `tests/test_alpaca_trading.py` |

### Phase 2: Exit Strategy Monitor

| Task | Description | Files |
|------|-------------|-------|
| 2.1 | Real-time 1-min bar collector (Alpaca or Schwab stream) | `trader/trading/bar_collector.py` |
| 2.2 | Live VDD signal detector (reuse backtest math) | `trader/trading/exit_monitor.py` |
| 2.3 | Guard stop management (cancel on exit) | `trader/trading/exit_monitor.py` |
| 2.4 | Min-hold enforcement (5 bars after fill) | `trader/trading/exit_monitor.py` |
| 2.5 | Max holding period enforcement | `trader/trading/exit_monitor.py` |

### Phase 3: Portfolio Manager

| Task | Description | Files |
|------|-------------|-------|
| 3.1 | Position tracking synced with Alpaca account | `trader/trading/portfolio.py` |
| 3.2 | Capital allocation (5% per position, max 20) | `trader/trading/portfolio.py` |
| 3.3 | Momentum ranking for slot replacement | `trader/trading/portfolio.py` |
| 3.4 | Reinvestment delay enforcement | `trader/trading/portfolio.py` |

### Phase 4: Integration with Existing Pipeline

| Task | Description | Files |
|------|-------------|-------|
| 4.1 | Hook into orchestrator: signal → order | `trader/online/orchestrator.py` |
| 4.2 | Hook Watch lifecycle: fill → holding, exit → exited | `trader/online/orchestrator.py` |
| 4.3 | Dashboard: show live positions, P&L, orders | `trader/web/app.py` |
| 4.4 | SSE events for order fills, exits | `trader/web/app.py` |

### Phase 5: Shadow Mode & Validation

| Task | Description | Files |
|------|-------------|-------|
| 5.1 | Run live trading in parallel with existing Watch system | — |
| 5.2 | Compare: simulated Watch exits vs actual trading exits | — |
| 5.3 | Slippage tracking: expected vs actual fill prices | — |
| 5.4 | Volume delta shadow: tick-level vs bar-level comparison | — |

### Phase 6: Paper → Live Transition

| Task | Description |
|------|-------------|
| 6.1 | Validate paper trading P&L matches backtest expectations |
| 6.2 | Add circuit breakers (daily loss limit, position size caps) |
| 6.3 | Switch `ALPACA_PAPER=false` with live credentials |
| 6.4 | Start with reduced position size (1% instead of 5%) |

---

## Risk Controls

### Backtest Assumptions vs Live Reality

| Assumption | Backtest | Live Reality | Mitigation |
|------------|----------|-------------|------------|
| **Fill price** | Exact bar price | Slippage (~1-5 bps) | Use limit orders for large positions |
| **Fill certainty** | Always fills | May reject/partial fill | Handle partial fills, retry logic |
| **Execution timing** | Instantaneous | 50-500ms latency | Price delay already 10 min, absorbs this |
| **Volume data** | All exchanges | IEX only (free plan) | Use Schwab for VDD, Alpaca for orders |
| **Guard stop** | Checked per bar | Alpaca stop order | Server-side stop is more reliable |
| **Concurrent positions** | Simulated slots | Real buying power | Track account.buying_power |
| **Transaction cost** | 10 bps flat | Varies (spread + commission) | Log actual costs, compare to 10 bps |
| **Market hours** | Filtered to 9:30-16:00 | Orders can fill extended hours | Set `time_in_force=DAY`, no extended_hours |

### Circuit Breakers (must implement)

| Breaker | Threshold | Action |
|---------|-----------|--------|
| Daily loss limit | -2% of portfolio | Stop opening new positions |
| Single position loss | -10% (guard stop) | Exit immediately (Alpaca stop order) |
| Max positions | 20 concurrent | Skip new entries |
| Max daily trades | 50 (PDT-safe buffer) | Stop trading for the day |
| API error rate | 3 consecutive failures | Pause trading, alert |
| Account equity floor | -5% from starting | Halt all trading |

### PDT (Pattern Day Trader) Considerations

- PDT rule applies to accounts under $25,000
- 3 day trades in a rolling 5-business-day period
- Our trades are typically held for hours (not day trades if held overnight)
- **Mitigation:** Track day trade count, pause if approaching limit

---

## Data Source Differences

### 1-Minute Bars: Source Comparison

| Source | Coverage | Update Frequency | Cost | Best For |
|--------|----------|-----------------|------|----------|
| **Schwab** | All exchanges, 10-day window | Real-time during market hours | Free (with account) | Accurate volume for VDD |
| **yfinance** | All exchanges, 7-day window | ~15 min delay (free) | Free | Fallback, backtesting |
| **Alpaca IEX** | IEX only (~2-5% volume) | Real-time | Free | Low-volume signals only |
| **Alpaca SIP** | All exchanges | Real-time | ~$99/mo | Most accurate for everything |

### Recommended Data Strategy

```
Entry signals:    Existing pipeline (Alpaca news → LLM agents)
VDD monitoring:   Schwab 1-min bars (already integrated, full volume)
Guard stops:      Alpaca stop orders (server-side, always active)
Order execution:  Alpaca Trading API
Account/positions: Alpaca Trading API
```

**Why not use Alpaca for VDD bars?**
- Free plan = IEX only = unreliable volume data
- Schwab gives full-exchange volume, already integrated
- Keep VDD computation identical to backtest (Schwab data)

### Real-Time Streaming Options

For tick-level data (Phase 2+):

| Stream | Data | Update Rate | Notes |
|--------|------|------------|-------|
| yfinance WebSocket | Price + day volume | ~1/sec | Free, easy, already tested |
| Schwab LEVELONE_EQUITIES | Price + volume + last size | ~multiple/sec | Already integrated |
| Alpaca StockDataStream | Trades, quotes, bars | Real-time | Requires Algo Trader Plus for SIP |

**Recommendation:** Use Schwab streaming (already have code in `schwab_client.py`)
for tick-level VDD shadow mode.

---

## Testing Strategy

### Unit Tests

| Test | Description |
|------|-------------|
| Order submission mock | Test `enter_position`, `exit_position` with mocked Alpaca client |
| Guard stop calculation | Verify stop price = entry * 0.90 |
| VDD signal detection | Verify same signals as backtest on identical data |
| Position sizing | Verify 5% allocation math with various equity levels |
| Ranking/replacement | Verify momentum-based slot replacement |

### Integration Tests (Paper Trading)

| Test | Description |
|------|-------------|
| Round-trip trade | BUY → monitor → SELL, verify P&L tracking |
| Guard stop trigger | Enter position, price drops 10%, verify stop executes |
| VDD exit trigger | Enter position during VDD-favorable setup, verify exit |
| Max hold exit | Enter position, hold beyond max_hold, verify exit |
| Concurrent positions | Open 5+ positions simultaneously, verify accounting |
| Slot replacement | Fill all 20 slots, new signal arrives, verify weakest replaced |

### Shadow Mode Validation

Run the live trading system **without real orders** for 1-2 weeks:
- Record all entry/exit decisions with timestamps
- Compare against what the backtest would have done on the same data
- Measure: timing differences, price differences, signal agreement

### Volume Delta Comparison Test

Already implemented: `tests/test_volume_delta_comparison.py`

Run periodically to track:
- Bar-based vs tick-level signal agreement over time
- VDD signal timing differences
- Cumulative delta curve correlation

---

## Open Questions

### Must Resolve Before Implementation

1. **Alpaca account setup:** Do we already have paper trading credentials, or need
   to create an account? (Currently only news API keys exist)

2. **Data feed plan:** Free (IEX) or Algo Trader Plus (SIP)? VDD accuracy depends
   on volume data quality. Schwab can be primary data source, but Alpaca bars
   would be simpler for integrated monitoring.

3. **Guard stop implementation:** Server-side Alpaca stop order vs client-side
   monitoring? Server-side is safer (works even if our process crashes), but
   requires managing stop order lifecycle (cancel on VDD exit, replace on
   position change).

4. **Position sizing:** Fixed dollar amount or percentage of equity? The backtest
   uses 5% of starting capital — live should use 5% of current equity.

5. **Short selling:** The backtest's TradingSignal includes `direction: "bearish"`.
   Do we want to short-sell on bearish signals? Alpaca supports shorting, but adds
   complexity (margin, locate fees, borrow availability).

### Can Defer

6. **Tick-level VDD:** When to switch from bar-based to tick-level? (After
   sufficient shadow data — 2+ weeks)

7. **Extended hours:** The best backtest uses `market_close: 16:00`. Should we
   eventually trade extended hours?

8. **Multiple exit strategies:** The backtest shows VDD as best, but other
   strategies (ATR trailing, RSI) could be used as additional signals.

9. **Entry filtering improvements:** Better news triggers, pre-filtering,
   confidence threshold tuning.

---

## Appendix: File Map

### Existing Files (to modify)

| File | Change |
|------|--------|
| `trader/online/orchestrator.py` | Add order execution hook on BUY signal |
| `trader/online/watcher.py` | Connect Watch lifecycle to Alpaca positions |
| `trader/config.py` | Add trading config (paper mode, position size, etc.) |
| `trader/web/app.py` | Add trading dashboard endpoints |

### New Files (to create)

| File | Purpose |
|------|---------|
| `trader/trading/__init__.py` | Trading module |
| `trader/trading/executor.py` | Order submission + fill tracking |
| `trader/trading/exit_monitor.py` | Real-time VDD + guard monitoring |
| `trader/trading/portfolio.py` | Position tracking + allocation |
| `trader/trading/alpaca_client.py` | Alpaca SDK wrapper |
| `tests/test_alpaca_trading.py` | Integration tests |
| `tests/test_volume_delta_comparison.py` | Already created |

### Existing Infrastructure We Leverage

| Component | File | What We Reuse |
|-----------|------|--------------|
| Volume delta math | `trader/market/backtest.py:1263-1305` | `_compute_volume_delta()`, `_compute_vdd_signal_indices()` |
| Guard logic | `trader/market/backtest.py:1346-1411` | Guard price calculations |
| Watch lifecycle | `trader/models/watch.py` | Position state machine |
| Market data | `trader/market/data_service.py` | 1-min bar fetching |
| Schwab streaming | `trader/market/schwab_client.py` | Real-time price/volume |
| Event bus | `trader/web/app.py` | SSE notifications |
| Config | `trader/config.py` | Environment-driven settings |
