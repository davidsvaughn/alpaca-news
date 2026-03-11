# Tick Collector: Schwab Streaming → TimescaleDB

> Standalone service for streaming Schwab LEVELONE_EQUITIES (with extended
> fields) into TimescaleDB, providing tick-level volume delta to the trader app.
> TIMESALE_EQUITY is subscribed opportunistically but may not be available.
>
> Hub document: plan, roadmap, progress, issues, decisions.
>
> Started: 2026-03-10
>
> See also:
> - [VOLUME-DELTA-REALTIME.md](VOLUME-DELTA-REALTIME.md) — Shadow collector architecture (superseded by tick_collector)
> - [VDD-COMPARISON.md](VDD-COMPARISON.md) — Bar-based vs tick-level comparison (blocked on data)
> - [SchwabStreamerAPI_LEVELONE.md](refs/SchwabStreamerAPI_LEVELONE.md) — L1 field reference
> - [docker-compose.yml](../docker-compose.yml) — TimescaleDB container
> - [tick_collector/](../tick_collector/) — Collector package source

---

## Table of Contents

1. [Motivation](#motivation)
2. [Three Tiers of Volume Delta Accuracy](#three-tiers-of-volume-delta-accuracy)
3. [Architecture](#architecture)
4. [Package Structure](#package-structure)
5. [Data Sources](#data-sources)
6. [Storage: TimescaleDB](#storage-timescaledb)
7. [Volume Gap Tracking](#volume-gap-tracking)
8. [Trade Classification: Lee-Ready Algorithm](#trade-classification-lee-ready-algorithm)
9. [Collector ↔ Trader Communication](#collector--trader-communication)
10. [Running the Collector](#running-the-collector)
11. [Deployment](#deployment)
12. [Roadmap](#roadmap)
13. [Decisions Log](#decisions-log)
14. [Open Questions](#open-questions)
15. [Issues / Blockers](#issues--blockers)

---

## Motivation

The shadow collector (LEVELONE_EQUITIES → 1-minute bars) has two problems:

1. **Schwab WebSocket drops during market hours** with no reconnection logic.
   The collector captures almost no trading-hour data — only extended hours.
   (See [VDD-COMPARISON.md § Root Cause](VDD-COMPARISON.md#root-cause-schwab-stream-drops-during-market-hours))
   **Update**: schwabdev has built-in reconnection (discovered 2026-03-10) — needs
   market-hours testing to confirm it fixes this.

2. **Current L1 subscription uses only basic fields** (`last_price` + `total_volume`
   differencing). By adding fields 9, 35, and 41, we get approximate trade-size
   visibility for institutional flow detection.

We need:
- **Extended L1 fields** for approximate trade-size filtering (Tier 2)
- **TIMESALE_EQUITY** if available for exact per-trade data (Tier 3)
- **Separate always-on service** decoupled from the trader app
- **Robust storage** for 50-100+ symbols
- **Reliable reconnection** so we never lose market-hours data

---

## Three Tiers of Volume Delta Accuracy

| Tier | Source | Trade-Size Filtering | Status |
|------|--------|---------------------|--------|
| **1** (legacy) | L1: `last_price` + `total_volume` differencing | None — all trades lumped | Shadow collector (superseded by tick_collector) |
| **2** (primary) | L1 + `last_size` (9), `trade_time` (35), `last_mic_id` (41) | Approximate — visible trade + volume gap tracking | **Collector built, needs market-hours test** |
| **3** (ideal) | `TIMESALE_EQUITY` per-trade feed | Exact — every trade visible | May be unavailable (code=11 outside hours) |

**Tier 2 detail**: LEVELONE_EQUITIES updates ~1/sec. Between updates, multiple
trades may occur. Only the last trade's `last_size` is reported, but
`total_volume` differencing captures the total volume that moved. The difference
(`volume_delta - last_size`) is "unclassified volume" — trades we can see in
aggregate but can't individually classify as uptick/downtick.

**Why L1-primary**: TIMESALE_EQUITY returned `code=11` (service unavailable)
during testing at 2 AM. It may only work during market hours, or it may not
be available at all on Schwab's current API. The collector subscribes to both
but is designed to work with L1 alone.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Same Machine                                │
│                                                                    │
│  ┌───────────────────────────┐    ┌─────────────────────────────┐ │
│  │  tick_collector            │    │  TimescaleDB (Docker:5433)   │ │
│  │  (python -m tick_collector)│    │  ├── trades (hypertable)     │ │
│  │                           │    │  ├── bars_1m (cont. agg)     │ │
│  │  Schwab WebSocket         │    │  ├── compression (7d)        │ │
│  │  ├── LEVELONE_EQUITIES    │    │  └── retention (30d raw)     │ │
│  │  │   (primary, always)    │    └─────────────────────────────┘ │
│  │  ├── TIMESALE_EQUITY      │                 ▲                   │
│  │  │   (optional, if avail) │                 │                   │
│  │  │                        │                 │                   │
│  │  ├── TickClassifier       │                 │                   │
│  │  │   └── uptick/downtick  │                 │                   │
│  │  │                        │                 │                   │
│  │  ├── TradeBuffer          │                 │                   │
│  │  │   └── thread-safe      │    asyncpg      │                   │
│  │  │       deque            │───batch INSERT──┘                   │
│  │  │       (flush every 2s) │                                     │
│  │  │                        │                                     │
│  │  └── Config               │                                     │
│  │      └── symbols.txt      │                                     │
│  │          or TICK_SYMBOLS   │                                     │
│  └───────────────────────────┘                                     │
│                                                                    │
│  ┌───────────────────────────┐                                     │
│  │  trader app               │─── SQL reads (asyncpg) ────────────┘│
│  │  ├── VDD from bars_1m     │                                     │
│  │  ├── Trade-size filtering │                                     │
│  │  └── Fallback: bar-based  │                                     │
│  └───────────────────────────┘                                     │
└────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
schwabdev Stream (background thread)
  ├── LEVELONE_EQUITIES message
  │     └── Parse: last_price(3), total_volume(8), last_size(9),
  │               trade_time(35), last_mic_id(41)
  │           └── Volume differencing (total_volume - prev_total_volume)
  │                 └── Skip if volume_delta <= 0 (no new trades)
  │
  └── TIMESALE_EQUITY message (if available)
        └── Parse: time(1), price(2), size(3)

  Both produce Trade objects:
        └── TickClassifier → direction (+1/-1/0)
              └── TradeBuffer.append() (thread-safe)
                    └── Async flush loop (every 2s)
                          └── Batch INSERT → TimescaleDB `trades` table
                                └── Continuous aggregate → `bars_1m` view
                                      └── Trader app queries bars_1m
```

---

## Package Structure

```
tick_collector/
  __init__.py
  __main__.py        # Entry point: python -m tick_collector
  config.py          # CollectorConfig from env vars + symbols.txt
  symbols.txt        # Default 28-symbol watchlist (editable)
  classifier.py      # TickClassifier: uptick/downtick/zero-tick
  buffer.py          # TradeBuffer: thread-safe deque, drain in batches
  db.py              # Trade dataclass, asyncpg connect + batch insert
  collector.py       # TickCollector: schwabdev → parse → buffer → flush
  init.sql           # TimescaleDB schema (auto-run on container init)
```

### Key Components

| Component | File | Description |
|-----------|------|-------------|
| `CollectorConfig` | `config.py` | Loads DSN, Schwab creds, symbols from env + file |
| `TickClassifier` | `classifier.py` | Lee-Ready algorithm: classifies trades as buyer/seller-initiated using bid/ask midpoint, tick rule fallback. See [Trade Classification](#trade-classification-lee-ready-algorithm). |
| `TradeBuffer` | `buffer.py` | Thread-safe deque. schwabdev thread appends, async loop drains. |
| `Trade` | `db.py` | Dataclass with `source` ("L1"/"TS"), `volume_delta`, `total_volume` |
| `TickCollector` | `collector.py` | Main orchestrator. Subscribes to both L1 and TIMESALE, parses messages, manages buffer + flush loop. |

---

## Data Sources

### Primary: LEVELONE_EQUITIES (Tier 2)

Subscribed fields:

| Field | Name | Type | Use |
|-------|------|------|-----|
| 0 | Symbol | String | Key |
| 3 | Last Price | double | Price for tick classification |
| 8 | Total Volume | long | Volume differencing (total moved between updates) |
| 9 | Last Size | long | Size of most recent trade (visible trade) |
| 16 | Last ID | char | Exchange of last trade |
| 35 | Trade Time in Long | Long | Millisecond-precision trade timestamp |
| 41 | Last MIC ID | String | 4-char Market Identifier Code (exchange) |

Full L1 field subscription string:
```
0,1,2,3,4,5,8,9,10,11,12,16,17,18,33,35,41,42
```

See [SchwabStreamerAPI_LEVELONE.md](refs/SchwabStreamerAPI_LEVELONE.md) for all field definitions.

### Optional: TIMESALE_EQUITY (Tier 3)

Subscribed via `stream.basic_request()`. Provides individual trade executions:

| Field | Name |
|-------|------|
| 0 | Symbol |
| 1 | Trade Time (ms) |
| 2 | Last Price |
| 3 | Last Size |
| 4 | Last Sequence |

When available, TIMESALE trades are stored with `source='TS'` and no
`volume_delta`/`total_volume` (not needed — every trade is individually visible).

### Schwab WebSocket Capacity

- Up to **500 symbols** per subscription
- L1 and TIMESALE can coexist on the **same connection**
- One connection per Schwab account

---

## Storage: TimescaleDB

### Setup

```bash
# Start TimescaleDB (port 5433, system Postgres uses 5432)
docker compose up -d

# Schema auto-applied from tick_collector/init.sql on first start
# DSN: postgresql://tickdata:tickdata_dev@localhost:5433/tickdata
```

### Schema

```sql
CREATE TABLE trades (
    time           TIMESTAMPTZ      NOT NULL,   -- see "Timestamps" below
    received_at    TIMESTAMPTZ      NOT NULL,   -- see "Timestamps" below
    symbol         TEXT             NOT NULL,
    price          DOUBLE PRECISION NOT NULL,
    size           INTEGER          NOT NULL,   -- last_size (L1) or trade size (TS)
    exchange       TEXT,                        -- last_mic_id (L1) or exchange (TS)
    direction      SMALLINT,                    -- +1 uptick, -1 downtick, 0 zero-tick
    source         CHAR(2)          NOT NULL DEFAULT 'L1',  -- 'L1' or 'TS'
    volume_delta   INTEGER,                     -- total volume change since prev update (L1)
    total_volume   BIGINT                       -- running total_volume snapshot (L1)
);

-- Hypertable + index
SELECT create_hypertable('trades', 'time');
CREATE INDEX idx_trades_symbol_time ON trades (symbol, time DESC);
```

### Timestamps

Each trade row records **two** timestamps:

| Column | Source | Description |
|--------|--------|-------------|
| `time` | Schwab field 35 (`trade_time_ms`) | When the trade actually occurred, per Schwab. Millisecond precision. Falls back to `data.timestamp` (Schwab stream message time) if field 35 is missing, or `received_at` as last resort. |
| `received_at` | Local `datetime.now(UTC)` | When our collector received the WebSocket message. Always present. |

**Why both?**
- `time` is the authoritative trade time for bucketing and ordering.
- `received_at` captures pipeline latency (`received_at - time`).
- When `time` is missing (field 35 absent on ~11-36% of L1 updates), the gap
  between `received_at` and real trade time can be estimated from rows that
  have both values — enabling better interpolation for missing timestamps.
- Comparing the two over time reveals if Schwab's reported times drift relative
  to wall clock, or if there are systematic delays by exchange (`last_mic_id`).

```sql
-- Measure pipeline latency (Schwab trade_time → our receipt)
SELECT symbol,
       avg(EXTRACT(EPOCH FROM (received_at - time))) AS avg_latency_sec,
       max(EXTRACT(EPOCH FROM (received_at - time))) AS max_latency_sec,
       count(*) FILTER (WHERE time = received_at) AS missing_trade_time
FROM trades
WHERE time >= NOW() - INTERVAL '1 hour'
GROUP BY symbol;
```

### Continuous Aggregate: bars_1m

```sql
CREATE MATERIALIZED VIEW bars_1m WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    symbol,
    first(price, time) AS open, max(price) AS high,
    min(price) AS low, last(price, time) AS close,
    -- volume: uses volume_delta (true total) when available, else size
    sum(COALESCE(volume_delta, size))           AS volume,
    -- uptick/downtick: uses size (the visible trade we can classify)
    sum(size) FILTER (WHERE direction = 1)      AS uptick_vol,
    sum(size) FILTER (WHERE direction = -1)     AS downtick_vol,
    sum(size * direction)                       AS net_delta,
    count(*)                                    AS trade_count,
    -- how much volume we couldn't classify (L1 gap)
    sum(COALESCE(volume_delta, size) - size)    AS unclassified_vol
FROM trades GROUP BY bucket, symbol;
```

Key columns in bars_1m:
- **`volume`**: True total volume (from `total_volume` differencing)
- **`uptick_vol`/`downtick_vol`**: Classified by visible `last_size` trades
- **`unclassified_vol`**: Volume that moved between L1 updates but couldn't be
  individually classified. High values = L1 missing many trades.
- **`trade_count`**: Number of L1 updates (not actual trades)

### Policies

| Policy | Setting |
|--------|---------|
| Continuous aggregate refresh | Every 1 minute (10-min start offset, 1-min end offset) |
| Compression | Raw trades compressed after 7 days |
| Retention | Raw trades dropped after 30 days (bars kept forever) |

### Query Examples

```sql
-- 1-minute bars for VDD computation
SELECT bucket, open, high, low, close, volume,
       uptick_vol, downtick_vol, net_delta
FROM bars_1m
WHERE symbol = 'AAPL' AND bucket >= NOW() - INTERVAL '80 minutes'
ORDER BY bucket;

-- Institutional flow: only visible trades >= 500 shares
SELECT time_bucket('1 minute', time) AS bucket,
       sum(size) FILTER (WHERE direction = 1) AS big_uptick,
       sum(size) FILTER (WHERE direction = -1) AS big_downtick
FROM trades
WHERE symbol = 'AAPL' AND size >= 500
  AND time >= NOW() - INTERVAL '80 minutes'
GROUP BY bucket ORDER BY bucket;

-- Data quality: what % of volume is unclassified?
SELECT symbol,
       sum(volume) AS total_vol,
       sum(unclassified_vol) AS unclassified,
       round(sum(unclassified_vol)::numeric / NULLIF(sum(volume), 0) * 100, 1)
           AS pct_unclassified
FROM bars_1m
WHERE bucket >= NOW() - INTERVAL '1 day'
GROUP BY symbol ORDER BY pct_unclassified DESC;

-- 5-second bars for sub-minute VDD (query raw trades directly)
SELECT time_bucket('5 seconds', time) AS bucket,
       first(price, time) AS open, last(price, time) AS close,
       sum(size * direction) AS net_delta
FROM trades
WHERE symbol = 'NVDA' AND time >= NOW() - INTERVAL '10 minutes'
GROUP BY bucket ORDER BY bucket;
```

---

## Volume Gap Tracking

The key insight for L1 data: **`total_volume` differencing captures ALL volume**,
but `last_size` only shows the most recent trade. The gap tells us how much we're
missing.

```
L1 Update #1: total_volume = 1,000,000  last_size = 200  last_price = 225.10
L1 Update #2: total_volume = 1,000,800  last_size = 100  last_price = 225.15
              ─────────────────────────
              volume_delta = 800  (true volume that moved)
              last_size    = 100  (visible trade, classified as uptick)
              gap          = 700  (hidden trades, unclassified)
```

In `bars_1m`:
- `volume = 800` (accurate total from volume_delta)
- `uptick_vol = 100` (only the visible trade was uptick)
- `unclassified_vol = 700` (we know it moved but can't assign direction)

This is an inherent limitation of Tier 2. If TIMESALE_EQUITY becomes available,
all trades are individually visible and `unclassified_vol` drops to 0.

---

## Trade Classification: Lee-Ready Algorithm

The collector classifies each trade as buyer-initiated (+1), seller-initiated (-1),
or neutral (0) using the **Lee-Ready algorithm**, a well-known academic method for
inferring trade direction from quote data.

**Implementation**: `tick_collector/classifier.py` (`TickClassifier`)

### How it works

1. **Quote rule (primary)**: Compare the trade price to the bid/ask midpoint.
   - Price > midpoint → buyer-initiated (+1)
   - Price < midpoint → seller-initiated (-1)
   - Price = midpoint → fall through to tick rule

2. **Tick rule (fallback)**: Compare the trade price to the previous trade price
   for the same symbol.
   - Price > previous → uptick (+1)
   - Price < previous → downtick (-1)
   - Price = previous or no previous trade → neutral (0)

The tick rule is also used when bid/ask data is unavailable (common on L1 updates
where quote fields are missing).

### Where classification flows

- The classified `direction` is stored in the `direction` column of the `trades` table.
- The `bars_1m` continuous aggregate uses direction to compute:
  - **`uptick_vol`**: `sum(size) FILTER (WHERE direction = 1)` — buyer-initiated volume
  - **`downtick_vol`**: `sum(size) FILTER (WHERE direction = -1)` — seller-initiated volume
  - **`net_delta`**: `sum(size * direction)` — net buying/selling pressure
- These feed directly into the trader app's VDD (Volume Delta Divergence) computation.

### Reference

Lee, C. M. C., & Ready, M. J. (1991). "Inferring Trade Direction from Intraday Data."
*Journal of Finance*, 46(2), 733-746.

---

## Collector ↔ Trader Communication

### Primary: Shared Database (SQL)

The trader app connects to the same TimescaleDB instance and queries
`bars_1m` (or raw `trades`) directly via asyncpg.

```python
# In trader app — query tick-level 1-min bars for VDD
async def get_tick_bars(pool, symbol: str, lookback: int = 80):
    return await pool.fetch("""
        SELECT bucket as time, open, high, low, close, volume,
               uptick_vol, downtick_vol, net_delta, unclassified_vol
        FROM bars_1m
        WHERE symbol = $1 AND bucket >= NOW() - make_interval(mins => $2)
        ORDER BY bucket
    """, symbol, lookback)
```

### Future: Lightweight Control API (Unix Socket)

For real-time state queries and dynamic symbol management without restarting:

```
STATUS     → {"connected": true, "symbols": 28, "trades_today": 123456}
ADD AAPL   → {"ok": true, "symbols": 29}
REMOVE XOM → {"ok": true, "symbols": 27}
```

---

## Running the Collector

### Prerequisites

```bash
# TimescaleDB must be running
docker compose up -d

# Schwab credentials in .env
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
```

### Start / Stop / Restart

```bash
# Start (only one instance allowed — duplicate launches exit immediately)
uv run python -m tick_collector

# Stop
pkill -f "tick_collector"

# Restart
pkill -f "tick_collector"; sleep 1; uv run python -m tick_collector
```

A file lock (`logs/.tick_collector.lock`) prevents duplicate instances. If a second instance is launched while one is already running, it prints an error and exits immediately.

### Configuration (env vars)

| Var | Default | Description |
|-----|---------|-------------|
| `TIMESCALE_DSN` | `postgresql://tickdata:tickdata_dev@localhost:5433/tickdata` | TimescaleDB connection |
| `SCHWAB_APP_KEY` | (required) | Schwab API key |
| `SCHWAB_APP_SECRET` | (required) | Schwab API secret |
| `TICK_SYMBOLS` | (from `symbols.txt`) | Comma-separated symbol override |
| `TICK_FLUSH_INTERVAL` | `2.0` | Seconds between buffer flushes |
| `TICK_PORTFOLIO_SYNC_INTERVAL` | `30` | Seconds between portfolio sync polls (reflect current tickers) |
| `TICK_PORTFOLIO_COOLOFF` | `300` | Minutes to keep streaming after position exits |

### Test (no Schwab connection needed)

```bash
# Synthetic data → buffer → DB → bars_1m pipeline test
uv run python scripts/test_collector_db.py
```

---

## Deployment

### Current: Manual / Development

```bash
# Terminal 1: TimescaleDB
docker compose up -d

# Terminal 2: Collector
uv run python -m tick_collector
```

### Phase 2: systemd Service

```ini
# /etc/systemd/system/tick-collector.service
[Unit]
Description=Schwab Tick Collector (L1 + TIMESALE → TimescaleDB)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=david
WorkingDirectory=/home/david/code/davidsvaughn/alpaca-news
ExecStart=/home/david/.local/bin/uv run python -m tick_collector
Restart=always
RestartSec=5
EnvironmentFile=/home/david/code/davidsvaughn/alpaca-news/.env

[Install]
WantedBy=multi-user.target
```

---

## Roadmap

### Phase 1: Streaming Data Quality
> **Goal**: Verify L1 extended fields and TIMESALE availability during market hours.

- [x] Write standalone test script: `scripts/test_timesale.py` + `scripts/run_timesale_test.sh`
- [x] Add L1 extended fields (9=LastSize, 35=TradeTime, 41=LastMICID) to test script
- [x] Add volume gap analysis (L1 total_volume jump vs last_size) to test script
- [x] **Run during pre-market hours** (2026-03-10 ~9 AM ET, 120s test)
- [x] Measure L1 update frequency and volume gaps per symbol (see findings below)
- [x] Determine if TIMESALE_EQUITY is available during market hours → **NO** (code=11)
- [x] Test: can TIMESALE + L1 coexist? → Yes, same connection, but TIMESALE returns code=11
- [ ] Test schwabdev built-in reconnection during market hours
- [x] Document findings in this file (see Phase 1 Findings below)

#### Phase 1 Findings (2026-03-10, pre-market ~9 AM ET)

**TIMESALE_EQUITY**: Confirmed unavailable (`code=11`). Not just an after-hours
limitation — also fails during pre-market. Likely not exposed on Schwab's API.
**L1-primary design validated.**

**L1 Extended Fields**: Working well. Results from 120-second pre-market test:

| Symbol | Updates/min | Has last_size | Has trade_time | Has MIC ID | Missed volume % |
|--------|-------------|---------------|----------------|------------|-----------------|
| AAPL   | 18          | 83%           | 86%            | 47%        | 54%             |
| AMD    | 18          | 75%           | 89%            | 61%        | 98%             |
| NVDA   | 50          | 76%           | 87%            | 50%        | 77%             |
| SPY    | 58          | 54%           | 64%            | 43%        | 94%             |
| TSLA   | 50          | 75%           | 86%            | 45%        | 69%             |

Key observations:
- **`last_size` present ~54-83%** of updates. Missing = quote-only update (no trade).
- **`trade_time_ms` present ~64-89%**. Millisecond precision, reliable.
- **`last_mic_id` present ~43-61%**. Common values: XADF (dark pool), ARCX (Arca),
  EDGX, XNAS (Nasdaq), BATS, MEMX. Missing on ~50% of updates.
- **Missed volume: 54-98%** — significant. Most trades happen between L1 updates.
  Pre-market volume is low; regular session will likely have higher gaps.
- **`last_size: 0` updates** occur (quote-only) — collector skips these.
- **Update frequency**: 18-58/min during pre-market. Expect higher during regular hours.
- **Large trades visible**: 1700-share NVDA, 4000-share TSLA, 800-share TSLA — when
  a large trade IS the last trade, we catch it. Good for institutional flow detection.

### Phase 2: Collector Service ← **current**
> **Goal**: Always-on process with L1-primary streaming into TimescaleDB.

- [x] Build `tick_collector/` package (separate from trader app)
- [x] L1 message parser with extended fields (9, 35, 41)
- [x] TIMESALE message parser (optional, graceful fallback)
- [x] Volume differencing (total_volume tracking per symbol)
- [x] Tick classifier (uptick/downtick/zero-tick)
- [x] Thread-safe write buffer with batch drain
- [x] asyncpg batch inserts to TimescaleDB
- [x] Graceful shutdown (SIGTERM/SIGINT handler, final flush)
- [x] Config from env vars + symbols.txt file
- [x] Entry point: `python -m tick_collector`
- [ ] **Run during market hours** with live Schwab connection
- [ ] Implement heartbeat monitoring (detect silent disconnects)
- [ ] systemd unit file (tested)
- [ ] Integration test: kill WebSocket, verify reconnection + no data loss

### Phase 3: TimescaleDB + Schema ← **done**
> **Goal**: Production storage with auto-aggregation.

- [x] docker-compose.yml for TimescaleDB (port 5433)
- [x] Schema: trades hypertable + indexes
- [x] L1-aware schema: `source`, `volume_delta`, `total_volume` columns
- [x] Continuous aggregate: bars_1m with `unclassified_vol`
- [x] Compression policy (7 days)
- [x] Retention policy (30 days raw, bars forever)
- [x] Verify pipeline with synthetic data (`scripts/test_collector_db.py`)
- [ ] Verify query performance with realistic data volume (after market-hours collection)

### Phase 4: Trader Integration
> **Goal**: Trader app consumes tick-level bars from TimescaleDB.

- [ ] `get_tick_bars()` function: query bars_1m for VDD computation
- [ ] Fallback: if TimescaleDB unavailable, use bar-based VDD (current behavior)
- [ ] Trade-size filtered VDD queries (institutional flow)
- [ ] Replace shadow collector usage with TimescaleDB queries
- [ ] Sub-minute VDD experiments (5-second, 15-second bars)

### Phase 5: Analysis & Comparison
> **Goal**: Re-run VDD comparison with reliable, complete data.

- [ ] Collect at least 1 full trading week of continuous data
- [ ] Re-run `scripts/vdd_comparison.py` with TimescaleDB bars
- [ ] Measure L1 unclassified_vol percentage across symbols
- [ ] Trade-size filtering experiments (isolate institutional flow)
- [ ] Compare L1-based vs TIMESALE-based VDD (if both available)
- [ ] Document results in [VDD-COMPARISON.md](VDD-COMPARISON.md)

### Future
- [ ] Docker Compose deployment (collector + DB + trader)
- [ ] Dashboard: collector status, data rates, symbol health
- [ ] Multiple aggregation windows (5s, 15s, 1m, 5m)
- [ ] Alerting: stream disconnect notifications
- [ ] Dynamic symbol management via Unix socket API
- [ ] Remote deployment option (separate server)

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-10 | L1-primary design (Tier 2), TIMESALE optional (Tier 3) | TIMESALE returned code=11; may not be available. L1 extended fields provide useful approximation. |
| 2026-03-10 | Track `volume_delta` and `unclassified_vol` | L1 `total_volume` differencing captures true volume; gap between that and `last_size` = hidden trades. Transparency about data quality. |
| 2026-03-10 | `source` column ("L1" vs "TS") in trades table | When both sources active, allows quality comparison. Future: prefer TS rows over L1 for same symbol. |
| 2026-03-10 | Separate process, not thread in trader app | Trader restarts frequently during development; collector must stay up |
| 2026-03-10 | TimescaleDB over SQLite/DuckDB | Time-series optimized, continuous aggregates, compression, concurrent R/W, standard SQL |
| 2026-03-10 | Port 5433 (not 5432) | System PostgreSQL 14 already on 5432 |
| 2026-03-10 | systemd first, Docker later | Simplest path to always-on; containerize after core logic is proven |
| 2026-03-10 | Schwab free tier (not Alpaca SIP) | Already integrated, free, 500 symbol capacity |
| 2026-03-10 | Shared DB for collector↔trader comm | Simplest; trader already does SQL. Optional Unix socket API later. |
| 2026-03-10 | Three-tier accuracy model | L1 basic (Tier 1) → L1 extended (Tier 2) → TIMESALE (Tier 3). Each tier improves on the previous. |
| 2026-03-10 | asyncpg for DB access | Async-native, matches collector's asyncio flush loop. Batch `executemany` for inserts. |
| 2026-03-10 | Thread-safe buffer (deque + lock) | schwabdev callback runs on its own thread; async flush loop runs on asyncio. Buffer bridges the two. |
| 2026-03-10 | Direction classified in collector, not DB | Per-symbol price state tracking is simpler in Python than SQL window functions. First trade of session = 0 (zero-tick). |
| 2026-03-10 | Lee-Ready algorithm for trade classification | Well-established academic method (Lee & Ready, 1991). Quote rule (bid/ask midpoint) is more accurate than simple tick rule; tick rule used as fallback when quotes unavailable. |

---

## Open Questions

- [x] **schwabdev `start_auto()` behavior**: Only does daily start/stop scheduling.
      Does NOT add heartbeat or mid-session reconnection. However, `_run_streamer()`
      has built-in reconnection with exponential backoff (2s → 4s → ... → 120s cap)
      and auto re-subscribes all recorded subscriptions. Needs market-hours testing.
- [x] **Direction classification**: Classify in collector before DB insert. Simple
      price comparison per symbol. First trade of session = zero-tick (direction 0).
- [x] **Buffer flush strategy**: Time-based (every 2 seconds). Simple, predictable
      latency. Can tune via `TICK_FLUSH_INTERVAL` env var.
- [x] **Symbol list management**: Config file (`symbols.txt`) as base, env var
      (`TICK_SYMBOLS`) for override. Dynamic API deferred to future phase.
- [x] **TIMESALE_EQUITY availability**: Does NOT work. Returns code=11 both at
      2 AM and during pre-market (~9 AM). Likely not exposed on Schwab's current API.
      L1-primary design is the correct approach.
- [x] **L1 update frequency**: 18-58 updates/min during pre-market (varies by symbol).
      NVDA/SPY/TSLA ~50/min, AAPL/AMD ~18/min. Roughly 1 update per 1-3 seconds.
- [x] **Unclassified volume percentage**: 54-98% during pre-market. AMD and SPY
      worst (94-98%), AAPL best (54%). Regular session will likely be similar or higher.
- [ ] **OAuth token refresh during stream**: Does schwabdev auto-refresh the OAuth
      token while the WebSocket is open? Tokens expire every 30 minutes.
- [x] **Existing shadow collector**: tick_collector supersedes the shadow collector.
      Disable shadow collector once tick_collector is proven reliable during market hours.

---

## Issues / Blockers

### [ISSUE-1] TIMESALE_EQUITY not available on Schwab API
**Status**: resolved (confirmed unavailable)
**Severity**: low (L1-primary design works well)
**Description**: TIMESALE_EQUITY returns `code=11: Service not available` at all
times tested: 2 AM ET (after hours), ~9 AM ET (pre-market). Not just a timing
issue — Schwab likely does not expose TIMESALE services on their current streaming API.
**Resolution**: L1-primary design (Tier 2) is the production approach. TIMESALE
subscription attempt kept in collector for future-proofing but not expected to work.

<!-- Template for new issues:
### [ISSUE-N] Title
**Status**: open | investigating | resolved
**Severity**: blocker | high | medium | low
**Description**: ...
**Resolution**: ...
-->
