-- Tick Collector: TimescaleDB schema
-- Auto-runs on first container start via docker-entrypoint-initdb.d
--
-- Supports two data sources:
--   Tier 2 (primary): LEVELONE_EQUITIES with extended fields (9, 35, 41)
--   Tier 3 (optional): TIMESALE_EQUITY per-trade feed
--
-- For Tier 2 (L1), each update with a volume change becomes a row.
-- `last_size` is the reported size of the most recent trade, but
-- `volume_delta` (from total_volume differencing) captures ALL volume
-- that moved between L1 updates. The gap (volume_delta - last_size)
-- represents trades we can't see individually.

-- Raw trade/update records
CREATE TABLE trades (
    time           TIMESTAMPTZ      NOT NULL,
    symbol         TEXT             NOT NULL,
    price          DOUBLE PRECISION NOT NULL,
    size           INTEGER          NOT NULL,   -- last_size (L1) or trade size (TIMESALE)
    exchange       TEXT,                        -- last_mic_id (L1) or exchange (TIMESALE)
    direction      SMALLINT,                    -- +1 uptick, -1 downtick, 0 zero-tick
    source         CHAR(2)          NOT NULL DEFAULT 'L1', -- 'L1' or 'TS' (timesale)
    volume_delta   INTEGER,                     -- total volume change since previous update (L1 only)
    total_volume   BIGINT                       -- running total_volume snapshot (L1 only)
);

-- Convert to hypertable (automatic time-based partitioning)
SELECT create_hypertable('trades', 'time');

-- Index for symbol + time range queries
CREATE INDEX idx_trades_symbol_time ON trades (symbol, time DESC);

-- Auto-aggregate 1-minute bars with tick-level volume delta
-- For L1 data: `size` is last_size (visible trade), `volume_delta` is true volume moved.
-- Bars use volume_delta when available (L1) for accurate total volume,
-- and size for uptick/downtick splits (best approximation from L1).
CREATE MATERIALIZED VIEW bars_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    symbol,
    first(price, time)  AS open,
    max(price)          AS high,
    min(price)          AS low,
    last(price, time)   AS close,
    -- Use volume_delta for total volume (captures all trades, not just visible ones)
    sum(COALESCE(volume_delta, size))           AS volume,
    -- Uptick/downtick splits use `size` (the visible trade we can classify)
    sum(size) FILTER (WHERE direction = 1)      AS uptick_vol,
    sum(size) FILTER (WHERE direction = -1)     AS downtick_vol,
    sum(size * direction)                       AS net_delta,
    count(*)                                    AS trade_count,
    -- L1-specific: how much volume we couldn't classify (gap between updates)
    sum(COALESCE(volume_delta, size) - size)    AS unclassified_vol
FROM trades
GROUP BY bucket, symbol;

-- Refresh continuous aggregate every 1 minute for recent data
SELECT add_continuous_aggregate_policy('bars_1m',
    start_offset    => INTERVAL '10 minutes',
    end_offset      => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute'
);

-- Compress raw trades older than 7 days
ALTER TABLE trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'time'
);
SELECT add_compression_policy('trades', INTERVAL '7 days');

-- Drop raw trades older than 30 days (aggregated bars kept forever)
SELECT add_retention_policy('trades', INTERVAL '30 days');
