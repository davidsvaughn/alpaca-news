# Schwab Data Source (schwabdev)

> **Status**: Active (free with brokerage account)
> **Package**: `schwabdev>=3.0.1` (wrapper around Charles Schwab Trader API)
> **Client**: [`trader/market/schwab_client.py`](trader/market/schwab_client.py)
> **Env vars**: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_DISABLED` (optional)
> **Demo**: [`demo/schwab_demo.py`](demo/schwab_demo.py) — `uv run python demo/schwab_demo.py [SYMBOL]`

---

## Overview

Schwab is our **primary real-time market data source**. The `schwabdev` package wraps the [Charles Schwab Trader API](https://developer.schwab.com/) with automatic OAuth token management and WebSocket streaming. Access is **free** with any Schwab brokerage account — no monthly API fees.

When Schwab is unavailable or disabled, we fall back to yfinance (see [YFINANCE.md](YFINANCE.md)).

---

## What We Currently Pull

### 1. Real-Time Quotes (`client.quote()` / `client.quotes()`)

**Our method**: `SchwabMarketClient.get_quote(symbol)` / `get_quotes(symbols)`
**Returns**: `QuoteSnapshot` dataclass

| Field | Type | Description |
|-------|------|-------------|
| `last_price` | float | Last trade price |
| `bid` / `ask` | float | Current bid/ask |
| `total_volume` | int | Session total volume |
| `high` / `low` | float | Session high/low |
| `open_price` | float | Session open |
| `close_price` | float | Previous close |
| `net_change` | float | Dollar change from close |
| `net_pct_change` | float | Percent change |
| `mark` | float | Mark price |
| `timestamp_ms` | int | Trade time (epoch ms) |

**Used by**: `check_price` tool, `build_price_context()`, `check_price_spike()`, `prefetch_market_data()` Price & Trend section

### 2. Price History / Candles (`client.price_history()`)

Two methods wrap this endpoint:

**`SchwabMarketClient.get_intraday_candles(symbol)`** — 1-day of 1-minute bars
**Default params**: `periodType="day"`, `period=1`, `frequencyType="minute"`, `frequency=1`, `needExtendedHoursData=True`

**`SchwabMarketClient.get_candles_by_date_range(symbol, start, end, freq_type, freq)`** — arbitrary date range
**Used by**: `price_10min.py` for backtesting 10-minute candles

**Returns**: `list[Candle]`

| Field | Type | Description |
|-------|------|-------------|
| `t` | str | ISO timestamp |
| `o`, `h`, `l`, `c` | float | OHLC |
| `v` | int | Volume |

**Used by**: `check_price_spike()`, `check_volume_regime()`, `compute_volume_delta()`, `get_price_history` tool, `price_10min.py`

**Period/Frequency matrix** (from Schwab Trader API docs):

| `periodType` | Valid `period` | Valid `frequencyType` | Valid `frequency` |
|-------------|---------------|----------------------|-------------------|
| `"day"` | 1, 2, 3, 4, 5, **10** | `"minute"` | 1, 5, 10, 15, 30 |
| `"month"` | 1, 2, 3, 6 | `"daily"`, `"weekly"` | 1 |
| `"year"` | 1, 2, 3, 5, 10, 15, 20 | `"daily"`, `"weekly"`, `"monthly"` | 1 |
| `"ytd"` | 1 | `"daily"`, `"weekly"`, `"monthly"` | 1 |

Alternatively, use `startDate`/`endDate` (datetime or UNIX epoch) instead of period — our `get_candles_by_date_range()` does this.

**Key limits**: Intraday minute data is limited to **10 trading days** max (`periodType="day"`, `period=10`). Beyond that, only daily/weekly/monthly bars are available. Within the 10-day window, Schwab provides real-time data including extended hours.

**Key advantage over yfinance**: Schwab's 10-day intraday data includes **extended hours** (pre-market + after-hours), and the bars are cached locally so data collected within this window remains available for backtesting months later.

### 3. Option Chains (`client.option_chains()`)

**Our method**: `SchwabMarketClient.check_options_activity(symbol)`
**Returns**: `OptionsActivity` dataclass

| Field | Type | Description |
|-------|------|-------------|
| `atm_iv_call` / `atm_iv_put` | float | ATM implied volatility |
| `atm_iv_avg` | float | Average ATM IV |
| `put_call_volume_ratio` | float | Put/call volume ratio |
| `put_call_oi_ratio` | float | Put/call open interest ratio |
| `total_call_volume` / `total_put_volume` | int | Aggregate volumes |
| `total_call_oi` / `total_put_oi` | int | Aggregate open interest |
| `nearest_expiry` | str | Nearest expiration date |
| `underlying_price` | float | Current underlying price |

**Params**: `contractType="ALL"`, `strikeCount=10`, `includeUnderlyingQuote=True`

**Used by**: `check_options_activity` tool

### 4. Company Fundamentals (`client.instruments()`)

**Our method**: `SchwabMarketClient.get_fundamentals(symbol)`
**Returns**: `SchwabFundamentals` dataclass

| Field | Type | Schwab Key | Description |
|-------|------|-----------|-------------|
| `market_cap` | float | `marketCap` | Market capitalization |
| `pe_ratio` / `forward_pe` | float | `peRatio` / `forwardPeRatio` | P/E ratios |
| `eps` | float | `epsTTM` | Earnings per share (TTM) |
| `dividend_yield` / `dividend_amount` | float | `dividendYield` / `dividendAmount` | Dividend info |
| `beta` | float | `beta` | Beta coefficient |
| `week_52_high` / `week_52_low` | float | `high52` / `low52` | 52-week range |
| `avg_10d_volume` / `avg_1y_volume` | float | `vol10DayAvg` / `vol1YrAvg` | Volume averages |
| `pb_ratio` | float | `pbRatio` | Price-to-book |
| `net_profit_margin` | float | `netProfitMarginTTM` | Net profit margin (TTM) |
| `return_on_equity` | float | `returnOnEquity` | ROE |
| `revenue` | float | `revenueTTM` | Revenue (TTM) |
| `shares_outstanding` | float | `sharesOutstanding` | Shares outstanding |
| `debt_to_equity` | float | `totalDebtToEquity` | Total debt-to-equity ratio |
| `short_int_to_float` | float | `shortIntToFloat` | Short interest as % of float |
| `short_int_days_to_cover` | float | `shortIntDayToCover` | Days to cover short interest |
| `eps_change_pct_ttm` | float | `epsChangePercentTTM` | EPS growth rate (TTM YoY) |
| `rev_change_pct_ttm` | float | `revChangeTTM` | Revenue growth rate (TTM) |

**Note**: Schwab doesn't provide `sector`/`industry` — these are backfilled from yfinance.

**Displayed in prompt** (via `prompt_builder.py` fundamentals field_map): Market Cap, P/E, P/B, EPS, Beta, Div Yield, Net Margin, ROE, Debt/Equity, EPS Growth, Rev Growth, Short % Float, Short Days, Sector, Industry.

**Used by**: `get_fundamentals` tool (with yfinance fallback), `prefetch_market_data()` Price & Trend section (52W range), fundamentals section

### 5. Market Movers (`client.movers()`)

**Our method**: `SchwabMarketClient.get_movers(index, direction)`
**Returns**: `list[Mover]`

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | str | Ticker |
| `description` | str | Company name |
| `direction` | str | "up" or "down" |
| `change` | float | Dollar change |
| `pct_change` | float | Percent change |
| `volume` | int | Trading volume |
| `last_price` | float | Last price |

**Indices**: `$DJI`, `$COMPX`, `$SPX`, `NYSE`, `NASDAQ`

**Used by**: `get_movers` tool, `check_market_context()`

### 6. Market Hours (`client.market_hour()`)

**Our method**: `SchwabMarketClient.get_market_hours(market_id)`

Returns session times (pre-market, regular, post-market) and open/closed status. Includes fallback to rough EST calculation if API unavailable.

**Used by**: `check_market_context()`, X stream burst gating

### 7. Level-1 Streaming (WebSocket)

**Our method**: `SchwabMarketClient.start_stream(symbols)` / `stop_stream()`
**Protocol**: `schwabdev.Stream` → WebSocket → `LEVELONE_EQUITIES`

Streams real-time updates (~1/sec per symbol, ~55 updates/min):

| Field ID | Name | Type | Currently Subscribed |
|----------|------|------|---------------------|
| 0 | symbol | str | Yes |
| 1 | bid | float | Yes |
| 2 | ask | float | Yes |
| 3 | last_price | float | Yes |
| 4 | bid_size | int | Yes |
| 5 | ask_size | int | Yes |
| 8 | total_volume | int | Yes |
| **9** | **last_size** | **long** | **No** — should add |
| 10 | high | float | Yes |
| 11 | low | float | Yes |
| 12 | close | float | Yes |
| **16** | **last_id** | **char** | **No** — exchange of last trade |
| 17 | open | float | Yes |
| 18 | net_change | float | Yes |
| 33 | mark | float | Yes |
| **35** | **trade_time_ms** | **long** | **No** — ms-precision trade time |
| **41** | **last_mic_id** | **str** | **No** — 4-char MIC exchange code |
| 42 | net_pct_change | float | Yes |

**Missing fields (9, 16, 35, 41)**: These are available in the API but not
currently subscribed. Adding them would enable "Tier 2" volume delta — tracking
the size and exchange of each observed last-trade, enabling approximate
trade-size filtering. See [TICK-COLLECTOR.md](../TICK-COLLECTOR.md) for the
three-tier volume delta accuracy model.

**Important limitation**: LEVELONE_EQUITIES is **not a per-trade feed**. Updates
arrive ~1/sec. If 20 trades happen between updates, only the last trade's
price/size is reported. The `total_volume` field captures aggregate volume, but
`last_size` only reflects the most recent trade — so `total_volume` may jump by
far more than `last_size`. This means trade-size filtering from L1 is approximate
(you see ~30-50% of individual trades, not all of them).

**Thread safety**: `StreamState` uses `threading.Lock` for concurrent access.

**Reconnection**: schwabdev has built-in reconnection with exponential backoff
(2s → 4s → ... → 120s cap) and automatically re-subscribes all recorded
subscriptions on reconnect. This was discovered 2026-03-10 but needs market-hours
testing to confirm it fixes the observed trading-hour dropouts.

**Used by**: Real-time volume delta computation, uptick/downtick analysis

### 8. TIMESALE_EQUITY (Per-Trade Feed — Not Yet Integrated)

**Status**: Testing (see [TICK-COLLECTOR.md](../TICK-COLLECTOR.md))
**Protocol**: Same Schwab WebSocket, different service subscription

TIMESALE_EQUITY provides **individual trade prints** — each message is one
trade with its price and size. This would enable:
- Exact trade-size filtering (institutional flow isolation)
- Sub-minute VDD computation
- More accurate uptick/downtick classification

| Field ID | Name | Type | Description |
|----------|------|------|-------------|
| 0 | symbol | str | Ticker symbol |
| 1 | trade_time | long | Trade time (ms since epoch) |
| 2 | last_price | double | Execution price |
| 3 | last_size | int | Number of shares traded |
| 4 | last_sequence | int | Trade sequence number |

**Subscription**: Uses `stream.basic_request("TIMESALE_EQUITY", "ADD", ...)`
since schwabdev doesn't have a dedicated wrapper method. Coexists on the same
WebSocket connection as LEVELONE_EQUITIES.

**Known issue**: Returns `code=11: Service not available` outside US trading
sessions (tested 2 AM ET 2026-03-10). Needs market-hours testing.

**Full reference**: See [`docs/refs/SchwabStreamerAPI_LEVELONE.md`](../refs/SchwabStreamerAPI_LEVELONE.md)
for all 52 LEVELONE_EQUITIES fields.

---

## Derived Computations

Built on top of raw Schwab data:

| Method | Description | Source Data | Used By |
|--------|-------------|-------------|---------|
| `check_price_spike()` | Detects >0.5% moves in last 5 min | Intraday candles | `check_price_spike` tool, prefetch |
| `check_volume_regime()` | Detects volume >2x session average | Intraday candles | `check_volume_regime` tool, prefetch |
| `compute_volume_delta()` | Uptick/downtick volume using inter-bar tick rule | Intraday 1-min candles | `compute_volume_delta` tool, prefetch |
| `build_price_context()` | Quotes + recent candles per symbol | Quotes + candles | Snapshot builder (`orchestrator.py`) |
| `build_market_context()` | SPY, VIX, session info | Multiple quotes + market hours | `prefetch_market_data()`, `check_market_context` tool |
| `get_quotes_with_fundamentals()` | Batch quotes + fundamentals in one API call | `client.quotes(fields="all")` | Available but not actively used |

---

## How It's Wrapped

### Initialization

```python
import schwabdev

# Auto-manages OAuth tokens (stored in ~/.schwabdev/tokens.db)
client = schwabdev.Client(app_key, app_secret)
```

### Data Flow

```
schwabdev.Client → Schwab REST API / WebSocket
       ↓
SchwabMarketClient (trader/market/schwab_client.py)
       ↓
MarketDataService (trader/market/data_service.py) — adds yfinance fallback
       ↓
tool_core.py tool functions → JSON strings for agents
```

### Fallback Pattern (in `data_service.py`)

```python
def get_fundamentals(self, symbol):
    if self._schwab.available:
        try:
            result = self._schwab.get_fundamentals(symbol)
            if "error" not in result:
                result["source"] = "schwab"
                return result
        except Exception: ...
    # Fallback
    result = self._yfinance.get_fundamentals(symbol)
    result["source"] = "yfinance"
    return result
```

---

## Pricing

### Access Model

| Requirement | Details |
|-------------|---------|
| **Cost** | Free (included with any Schwab brokerage account) |
| **Account** | Standard individual brokerage account |
| **Registration** | [Schwab Developer Portal](https://developer.schwab.com/) |
| **Approval** | App must be approved (takes several days) |
| **Data quality** | Real-time for most data; some feeds have 15-min delay |

There are **no monthly fees, no per-call charges, and no minimum account balance** for API access. This is a significant advantage over paid alternatives.

### Schwab vs Alternatives

| Feature | Schwab (Free) | Interactive Brokers | Polygon.io |
|---------|---------------|--------------------:|----------:|
| Monthly cost | $0 | $0–$30 | $29–$199 |
| Real-time quotes | Yes | Yes | Paid tiers |
| Options chains | Yes | Yes | Paid tiers |
| Level-1 streaming | Yes | Yes | Paid tiers |
| API key approval | Days | Hours | Instant |

### Limitations

- **Intraday minute data capped at 10 trading days** — beyond that, only daily/weekly/monthly
- No historical options pricing data
- Some data feeds may have 15-minute delays
- OAuth token refresh requires periodic re-authentication
- No sector/industry in fundamental data projection (we backfill from yfinance)
- Short interest data (`shortIntToFloat`, `shortIntDayToCover`) may be stale or zero for some symbols

---

## What Else Schwab Offers (Not Currently Used)

| Endpoint | Description | Status |
|----------|-------------|--------|
| `accounts()` | Account balances, positions | Available (trading) |
| `orders()` | Place/manage orders | Available (trading) |
| `transactions()` | Transaction history | Available |
| `instruments()` with `search` | Instrument search by name | Available |
| TIMESALE_EQUITY | Per-trade prints (price, size, time, exchange) | Testing ([TICK-COLLECTOR.md](../TICK-COLLECTOR.md)) |
| Level-2 streaming | NYSE/NASDAQ order book depth | Available (WebSocket) |
| Options streaming | Real-time options quotes | Available (WebSocket) |
| Futures data | Futures quotes/history | Available |
| Screener streaming | Real-time screener updates | Available (WebSocket) |

### Potential Additions

- **Account data**: Could be used for position sizing and risk management
- **Order management**: Automated trade execution (when we're ready)
- **Options streaming**: Real-time IV tracking for our options activity monitoring

---

## Configuration

```bash
# .env
SCHWAB_APP_KEY=<your-app-key>
SCHWAB_APP_SECRET=<your-app-secret>
SCHWAB_DISABLED=false  # Set to "true" to disable Schwab entirely
```

OAuth tokens are automatically managed by `schwabdev` and stored in `~/.schwabdev/tokens.db`.

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/market/schwab_client.py`](trader/market/schwab_client.py) | `SchwabMarketClient` — all Schwab API calls + streaming |
| [`trader/market/data_service.py`](trader/market/data_service.py) | `MarketDataService` — unified interface with yfinance fallback |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | Tool functions that delegate to `MarketDataService` |

---

## References

- [Schwab Developer Portal](https://developer.schwab.com/)
- [schwabdev GitHub](https://github.com/tylerebowers/Schwabdev)
- [schwabdev PyPI](https://pypi.org/project/schwabdev/)
