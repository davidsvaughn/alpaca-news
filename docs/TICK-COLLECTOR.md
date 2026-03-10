# Tick Collector: Raw Trade Data Collection Service

> Standalone service for streaming Schwab TIMESALE_EQUITY data, storing raw
> individual trades in TimescaleDB, and providing tick-level volume delta
> to the trader app.
>
> Hub document: plan, roadmap, progress, issues, decisions.
>
> Started: 2026-03-10
>
> See also:
> - [VOLUME-DELTA-REALTIME.md](VOLUME-DELTA-REALTIME.md) — Shadow collector architecture (predecessor)
> - [VDD-COMPARISON.md](VDD-COMPARISON.md) — Bar-based vs tick-level comparison (blocked on data)

---

## Table of Contents

1. [Motivation](#motivation)
2. [Goals](#goals)
3. [Architecture](#architecture)
4. [Data Source: TIMESALE_EQUITY](#data-source-timesale_equity)
5. [Resilient WebSocket Connection](#resilient-websocket-connection)
6. [Storage: TimescaleDB](#storage-timescaledb)
7. [Collector ↔ Trader Communication](#collector--trader-communication)
8. [Deployment](#deployment)
9. [Roadmap](#roadmap)
10. [Decisions Log](#decisions-log)
11. [Open Questions](#open-questions)
12. [Issues / Blockers](#issues--blockers)

---

## Motivation

The shadow collector (LEVELONE_EQUITIES → 1-minute bars) has two fatal problems:

1. **Schwab WebSocket drops during market hours** with no reconnection logic.
   The collector captures almost no trading-hour data — only extended hours.
   (See [VDD-COMPARISON.md § Root Cause](VDD-COMPARISON.md#root-cause-schwab-stream-drops-during-market-hours))

2. **LEVELONE_EQUITIES provides aggregated snapshots**, not individual trades.
   `total_volume` is cumulative; we infer per-tick volume by differencing. This
   means we can't filter by trade size, can't compute sub-minute VDD, and can't
   distinguish institutional from retail flow.

We need a new approach:
- **TIMESALE_EQUITY** for per-trade data (price, size, timestamp, exchange)
- **Separate always-on service** decoupled from the trader app (which restarts frequently)
- **Robust storage** for 50-100+ symbols of raw trade data
- **Reliable reconnection** so we never lose market-hours data

---

## Goals

### Must Have
- Stream TIMESALE_EQUITY for 50-100+ symbols simultaneously
- Persist every individual trade (price, size, timestamp, exchange)
- Auto-reconnect on WebSocket disconnect with exponential backoff
- Run independently of trader app (survives trader restarts)
- Trader app can query aggregated bars with tick-level volume delta
- Trade-size filtering capability (e.g., only trades > 1,000 shares)

### Should Have
- Auto-aggregated 1-minute bars with uptick/downtick splits (continuous aggregates)
- Compression and retention policies (raw ticks → compressed after N days)
- Health monitoring / status endpoint
- Dynamic symbol management (add/remove without restart)
- Graceful shutdown (flush buffers, close connections cleanly)

### Nice to Have
- Sub-minute VDD computation (5-second, 15-second bars)
- Multiple aggregation windows simultaneously
- Dashboard integration (collector status, symbol count, data rates)
- Docker deployment option (in addition to systemd)

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Same Machine (for now)                      │
│                                                                    │
│  ┌───────────────────────────┐    ┌─────────────────────────────┐ │
│  │  tick-collector            │    │  TimescaleDB (Docker)        │ │
│  │  (systemd service)        │    │  ├── trades (hypertable)     │ │
│  │                           │    │  ├── bars_1m (cont. agg)     │ │
│  │  ├── Schwab WebSocket     │───►│  ├── compression (7d)        │ │
│  │  │   ├── TIMESALE_EQUITY  │    │  └── retention (30d raw)     │ │
│  │  │   └── Auto-reconnect   │    └─────────────────────────────┘ │
│  │  │       (exp. backoff)   │                 ▲                   │
│  │  │                        │                 │                   │
│  │  ├── Write buffer         │                 │                   │
│  │  │   └── Batch inserts    │                 │                   │
│  │  │       (every N sec)    │                 │                   │
│  │  │                        │                 │                   │
│  │  ├── Symbol manager       │                 │                   │
│  │  │   └── Config file or   │                 │                   │
│  │  │       Unix socket API  │                 │                   │
│  │  │                        │                 │                   │
│  │  └── Health/status        │                 │                   │
│  │      └── /health endpoint │                 │                   │
│  └───────────────────────────┘                 │                   │
│                                                │                   │
│  ┌───────────────────────────┐                 │                   │
│  │  trader app (start/stop)  │─── SQL reads ───┘                   │
│  │  ├── VDD from bars_1m     │                                     │
│  │  ├── Trade-size filtering │                                     │
│  │  ├── Sub-minute analysis  │                                     │
│  │  └── Fallback: bar-based  │                                     │
│  └───────────────────────────┘                                     │
└────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
Schwab WebSocket
  └── TIMESALE_EQUITY messages
        └── Per-trade: {timestamp, symbol, price, size, exchange}
              └── Write buffer (in-memory, batched)
                    └── Batch INSERT into TimescaleDB `trades` table
                          └── Continuous aggregate → `bars_1m` view
                                └── Trader app queries bars_1m for VDD
```

### Key Design Decisions

- **Separate process, not a thread** — must survive trader app restarts
- **TimescaleDB, not SQLite** — handles concurrent read/write, time-series
  optimized, built-in compression and aggregation
- **Batch writes** — buffer trades in memory, flush every 1-5 seconds to
  reduce DB write overhead
- **TIMESALE_EQUITY, not LEVELONE_EQUITIES** — individual trades vs aggregated
  snapshots

---

## Data Source: TIMESALE_EQUITY

### What It Provides

Each message is a single trade execution:

| Field | Description |
|-------|-------------|
| `timestamp` | Trade time (millisecond precision) |
| `symbol` | Ticker symbol |
| `price` | Execution price |
| `size` | Number of shares traded |
| `exchange` | Exchange code (e.g., "Q" = NASDAQ) |

### How to Subscribe

TIMESALE_EQUITY uses the **same Schwab WebSocket** as LEVELONE_EQUITIES.
The schwabdev library doesn't expose a `timesale_equity()` method, but we
can send the subscription JSON directly on the existing connection:

```json
{
  "requests": [{
    "service": "TIMESALE_EQUITY",
    "requestid": "timesale_1",
    "command": "ADD",
    "SchwabClientCustomerId": "...",
    "SchwabClientCorrelId": "...",
    "parameters": {
      "keys": "AAPL,NVDA,TSLA,...",
      "fields": "0,1,2,3,4"
    }
  }]
}
```

Fields: 0=symbol, 1=trade_time, 2=last_price, 3=last_size, 4=last_sequence

### Capacity

- Schwab supports **up to 500 symbols** per WebSocket subscription
- Can run TIMESALE_EQUITY and LEVELONE_EQUITIES on the **same connection**
- One connection per Schwab account

### Data Volume Estimates

| Scenario | Trades/min/symbol | Symbols | Trades/day | Raw Size/day |
|----------|-------------------|---------|------------|--------------|
| Low (mid-cap) | ~50 | 50 | ~975K | ~30 MB |
| Medium (mixed) | ~100 | 75 | ~2.9M | ~90 MB |
| High (large-cap) | ~200 | 100 | ~7.8M | ~240 MB |

With TimescaleDB compression (10-20x): **~5-25 MB/day compressed**.

**Note**: Actual trade frequency from TIMESALE_EQUITY needs to be measured.
Schwab may throttle or aggregate differently than raw exchange feeds. This
is a key Phase 1 deliverable.

---

## Resilient WebSocket Connection

### Current Problem

`schwab_client.py` calls `schwabdev.Stream().start()` once. No heartbeat,
no reconnection. If the WebSocket drops during market hours, streaming
silently stops.

### Reconnection Strategy

```
┌─────────┐     ┌───────────┐     ┌──────────────┐
│ CONNECT │────►│ STREAMING │────►│ DISCONNECTED │
└─────────┘     └───────────┘     └──────────────┘
     ▲               │                    │
     │               │ heartbeat          │ backoff
     │               │ timeout            │ delay
     │               ▼                    ▼
     │          ┌───────────┐     ┌──────────────┐
     └──────────│ RECONNECT │◄────│   WAITING    │
                └───────────┘     └──────────────┘
```

**Heartbeat monitor**: During market hours, if no message received for
`HEARTBEAT_TIMEOUT` seconds (e.g., 10s), assume disconnected.

**Exponential backoff**: Reconnect delays: 1s → 2s → 5s → 10s → 30s (cap).
Reset backoff on successful reconnection + first message received.

**Re-subscribe on reconnect**: Maintain in-memory set of active symbols.
On reconnect, re-send TIMESALE_EQUITY subscription for all symbols.

**schwabdev `start_auto()`**: The library has an auto-scheduling method.
Needs testing to determine if it handles mid-session reconnection or only
handles daily start/stop scheduling. (See [Open Questions](#open-questions).)

### Failure Modes to Handle

| Failure | Detection | Recovery |
|---------|-----------|----------|
| WebSocket close frame | `on_close` callback | Reconnect immediately |
| Silent disconnect | Heartbeat timeout | Reconnect with backoff |
| Auth token expired | 401/error message | Refresh OAuth token, reconnect |
| Schwab API outage | Repeated reconnect failures | Back off to 60s, log alerts |
| Network down | Connection refused | Back off, keep retrying |
| Process crash | systemd detects exit | systemd auto-restart |

---

## Storage: TimescaleDB

### Why TimescaleDB

- PostgreSQL extension — standard SQL, mature ecosystem
- **Hypertables**: automatic time-based partitioning (old data doesn't slow queries)
- **Continuous aggregates**: auto-materialized 1-min, 5-min bars from raw trades
- **Compression**: 10-20x for time-series data
- **Retention policies**: auto-drop raw ticks after N days, keep aggregates forever
- **Concurrent read/write**: trader app queries while collector writes

### Schema

```sql
-- Raw individual trades
CREATE TABLE trades (
    time        TIMESTAMPTZ    NOT NULL,
    symbol      TEXT           NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    size        INTEGER        NOT NULL,
    exchange    TEXT,
    direction   SMALLINT       -- +1 uptick, -1 downtick, 0 zero-tick
);

-- Convert to hypertable (automatic time-based partitioning)
SELECT create_hypertable('trades', 'time');

-- Index for symbol + time range queries
CREATE INDEX idx_trades_symbol_time ON trades (symbol, time DESC);

-- Auto-aggregate 1-minute bars with tick-level volume delta
CREATE MATERIALIZED VIEW bars_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    symbol,
    first(price, time)  AS open,
    max(price)          AS high,
    min(price)          AS low,
    last(price, time)   AS close,
    sum(size)           AS volume,
    sum(size) FILTER (WHERE direction = 1)  AS uptick_vol,
    sum(size) FILTER (WHERE direction = -1) AS downtick_vol,
    sum(size * direction)                   AS net_delta,
    count(*)            AS trade_count
FROM trades
GROUP BY bucket, symbol;

-- Compress raw trades older than 7 days
ALTER TABLE trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'time'
);
SELECT add_compression_policy('trades', INTERVAL '7 days');

-- Drop raw trades older than 30 days (aggregated bars kept forever)
SELECT add_retention_policy('trades', INTERVAL '30 days');
```

### Query Examples

```sql
-- Get 1-minute bars for VDD computation (same interface as current shadow data)
SELECT bucket, open, high, low, close, volume, uptick_vol, downtick_vol, net_delta
FROM bars_1m
WHERE symbol = 'AAPL'
  AND bucket >= NOW() - INTERVAL '80 minutes'
ORDER BY bucket;

-- Institutional flow: only trades >= 1000 shares
SELECT
    time_bucket('1 minute', time) AS bucket,
    sum(size) FILTER (WHERE direction = 1)  AS big_uptick,
    sum(size) FILTER (WHERE direction = -1) AS big_downtick
FROM trades
WHERE symbol = 'AAPL'
  AND size >= 1000
  AND time >= NOW() - INTERVAL '80 minutes'
GROUP BY bucket
ORDER BY bucket;

-- 5-second bars for sub-minute VDD
SELECT
    time_bucket('5 seconds', time) AS bucket,
    first(price, time) AS open, last(price, time) AS close,
    sum(size * direction) AS net_delta
FROM trades
WHERE symbol = 'NVDA' AND time >= NOW() - INTERVAL '10 minutes'
GROUP BY bucket ORDER BY bucket;

-- Data health: trades per minute per symbol (monitoring)
SELECT symbol, count(*) / 390.0 AS avg_trades_per_min
FROM trades
WHERE time::date = CURRENT_DATE
GROUP BY symbol ORDER BY avg_trades_per_min DESC;
```

### Docker Setup

```yaml
# docker-compose.yml (database only, for now)
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg17
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: tickdata
      POSTGRES_PASSWORD: ${TIMESCALE_PASSWORD}
      POSTGRES_DB: tickdata
    volumes:
      - timescale_data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  timescale_data:
```

---

## Collector ↔ Trader Communication

### Primary: Shared Database (SQL)

The trader app connects to the same TimescaleDB instance and queries
`bars_1m` (or raw `trades`) directly. This is the simplest approach and
sufficient for VDD computation on every check cycle.

```python
# In trader app — query tick-level 1-min bars for VDD
import psycopg2  # or asyncpg

def get_tick_bars(symbol: str, lookback: int = 80) -> pd.DataFrame:
    query = """
        SELECT bucket as time, open, high, low, close, volume,
               uptick_vol, downtick_vol, net_delta
        FROM bars_1m
        WHERE symbol = %s AND bucket >= NOW() - INTERVAL '%s minutes'
        ORDER BY bucket
    """
    return pd.read_sql(query, conn, params=[symbol, lookback])
```

### Optional: Lightweight API (Unix Socket)

For real-time state queries (e.g., "is the collector alive?", "what symbols
are streaming?", "add AAPL to the stream"), the collector can expose a
simple Unix domain socket or TCP API.

```
Commands:
  STATUS          → {"connected": true, "symbols": 87, "trades_today": 1234567}
  ADD AAPL,NVDA   → {"ok": true, "symbols": 89}
  REMOVE AAPL     → {"ok": true, "symbols": 88}
  HEALTH          → {"uptime": 3600, "last_trade": "2026-03-10T15:30:01Z"}
```

This lets the trader app dynamically manage the symbol list without
restarting the collector.

---

## Deployment

### Phase 1: systemd Service

```ini
# /etc/systemd/system/tick-collector.service
[Unit]
Description=Schwab TIMESALE_EQUITY Tick Collector
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=david
WorkingDirectory=/home/david/code/davidsvaughn/alpaca-news
ExecStart=/home/david/.local/bin/uv run python -m tick_collector
Restart=always
RestartSec=5
Environment=TIMESCALE_DSN=postgresql://tickdata:password@localhost:5432/tickdata

[Install]
WantedBy=multi-user.target
```

### Future: Full Docker Compose

```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg17
    # ... (see above)

  tick-collector:
    build: .
    depends_on:
      - timescaledb
    environment:
      - TIMESCALE_DSN=postgresql://tickdata:password@timescaledb:5432/tickdata
      - SCHWAB_APP_KEY=${SCHWAB_APP_KEY}
      - SCHWAB_APP_SECRET=${SCHWAB_APP_SECRET}
    restart: unless-stopped

  trader:
    build: .
    depends_on:
      - timescaledb
    # ... started/stopped independently
```

---

## Roadmap

### Phase 1: Prove TIMESALE_EQUITY Works
> **Goal**: Verify data quality, frequency, and subscription mechanics.

- [ ] Write standalone test script: connect to Schwab WebSocket, subscribe
      to TIMESALE_EQUITY for 5 symbols, log raw trades for 30 minutes
- [ ] Measure actual trade frequency per symbol (compare to estimates)
- [ ] Verify fields available (timestamp precision, exchange codes, etc.)
- [ ] Test: can TIMESALE_EQUITY and LEVELONE_EQUITIES coexist on same connection?
- [ ] Test schwabdev `start_auto()` reconnection behavior
- [ ] Document findings in this file

### Phase 2: Resilient Collector Service
> **Goal**: Always-on process with automatic reconnection.

- [ ] Build `tick_collector/` package (separate from trader app)
- [ ] Implement resilient WebSocket wrapper (heartbeat + exponential backoff)
- [ ] Symbol management (config file for initial list, API for dynamic changes)
- [ ] Write buffer with batch inserts (flush every 1-5 seconds)
- [ ] Graceful shutdown (SIGTERM handler, flush buffers)
- [ ] systemd unit file
- [ ] Integration test: kill WebSocket, verify reconnection + no data loss

### Phase 3: TimescaleDB + Schema
> **Goal**: Production storage with auto-aggregation.

- [ ] docker-compose.yml for TimescaleDB
- [ ] Schema: trades hypertable + indexes
- [ ] Continuous aggregate: bars_1m
- [ ] Compression policy (7 days)
- [ ] Retention policy (30 days raw, bars forever)
- [ ] Verify query performance with realistic data volume
- [ ] Migrate collector from SQLite/file writes to TimescaleDB

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
- [ ] Trade-size filtering experiments (isolate institutional flow)
- [ ] Sub-minute VDD signal analysis
- [ ] Determine optimal lookback for tick-level VDD
- [ ] Document results in [VDD-COMPARISON.md](VDD-COMPARISON.md)

### Future
- [ ] Docker Compose deployment (collector + DB + trader)
- [ ] Dashboard: collector status, data rates, symbol health
- [ ] Multiple aggregation windows (5s, 15s, 1m, 5m)
- [ ] Alerting: stream disconnect notifications
- [ ] Remote deployment option (separate server)

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-10 | Use TIMESALE_EQUITY over LEVELONE_EQUITIES | Per-trade data enables size filtering, sub-minute VDD, more accurate tick classification |
| 2026-03-10 | Separate process, not thread in trader app | Trader restarts frequently during development; collector must stay up |
| 2026-03-10 | TimescaleDB over SQLite/DuckDB/Supabase | Time-series optimized, continuous aggregates, compression, concurrent R/W, standard SQL |
| 2026-03-10 | systemd first, Docker later | Simplest path to always-on; containerize after core logic is proven |
| 2026-03-10 | Schwab free tier (not Alpaca SIP) | Already integrated, free, 500 symbol capacity, sufficient for current needs |
| 2026-03-10 | 50-100+ symbols target | Broad watchlist beyond just portfolio holdings for research and pre-screening |
| 2026-03-10 | Shared DB for collector↔trader comm | Simplest; trader already does SQL. Optional Unix socket API for real-time control |
| 2026-03-10 | Raw subscription bypass (not schwabdev fork) | Less maintenance; send TIMESALE_EQUITY JSON directly on existing WebSocket |

---

## Open Questions

- [ ] **schwabdev `start_auto()` behavior**: Does it handle mid-session reconnection,
      or only daily start/stop scheduling? Needs testing (Phase 1).
- [ ] **TIMESALE_EQUITY throttling**: Does Schwab throttle or aggregate trade data
      at the API level? Need to measure actual vs theoretical frequency.
- [ ] **OAuth token refresh during stream**: Does schwabdev auto-refresh the OAuth
      token while the WebSocket is open? If not, we need to handle token expiry
      (tokens expire every 30 minutes).
- [ ] **Direction classification**: Compute `direction` (uptick/downtick) in the
      collector before DB insert, or in the continuous aggregate query? In-collector
      is simpler (per-symbol state tracking); in-query requires window functions.
- [ ] **Tick direction for first trade of day**: No previous price to compare against.
      Use previous day's close? Classify as zero-tick? Needs a rule.
- [ ] **Symbol list management**: Static config file? Dynamic API? Both?
      For 100+ symbols, a config file makes sense as the base, with API for
      temporary additions from the trader app.
- [ ] **Buffer flush strategy**: Time-based (every N seconds) vs size-based
      (every N trades) vs hybrid? Tradeoff: latency vs write efficiency.
- [ ] **Existing shadow collector**: Keep running in parallel during transition?
      Or disable once tick-collector is proven reliable?

---

## Issues / Blockers

_None yet — project just started._

<!-- Template for new issues:
### [ISSUE-N] Title
**Status**: open | investigating | resolved
**Severity**: blocker | high | medium | low
**Description**: ...
**Resolution**: ...
-->
