# Schwab Data Source (schwabdev)

> **Status**: Active (free with brokerage account)
> **Package**: `schwabdev>=3.0.1` (wrapper around Charles Schwab Trader API)
> **Client**: [`trader/market/schwab_client.py`](trader/market/schwab_client.py)
> **Env vars**: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_DISABLED` (optional)

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

**Used by**: `check_price` tool, `build_price_context()`, `check_price_spike()`

### 2. Intraday Price Candles (`client.price_history()`)

**Our method**: `SchwabMarketClient.get_intraday_candles(symbol)`
**Returns**: `list[Candle]`

| Field | Type | Description |
|-------|------|-------------|
| `t` | str | ISO timestamp |
| `o`, `h`, `l`, `c` | float | OHLC |
| `v` | int | Volume |

**Default params**: `periodType="day"`, `period=1`, `frequencyType="minute"`, `frequency=1`, `needExtendedHoursData=True`

**Used by**: `check_price_spike()`, `check_volume_regime()`, `compute_volume_delta()`, `get_price_history` tool

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

| Field | Type | Description |
|-------|------|-------------|
| `market_cap` | float | Market capitalization |
| `pe_ratio` / `forward_pe` | float | P/E ratios |
| `eps` | float | Earnings per share (TTM) |
| `dividend_yield` / `dividend_amount` | float | Dividend info |
| `beta` | float | Beta coefficient |
| `week_52_high` / `week_52_low` | float | 52-week range |
| `avg_10d_volume` / `avg_1y_volume` | float | Volume averages |
| `pb_ratio` | float | Price-to-book |
| `net_profit_margin` | float | Net profit margin (TTM) |
| `return_on_equity` | float | ROE |
| `revenue` | float | Revenue (TTM) |
| `shares_outstanding` | float | Shares outstanding |

**Note**: Schwab doesn't provide `sector`/`industry` — these are backfilled from yfinance.

**Used by**: `get_fundamentals` tool (with yfinance fallback)

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

Streams real-time tick-by-tick updates:

| Field ID | Name | Type |
|----------|------|------|
| 0 | symbol | str |
| 1 | bid | float |
| 2 | ask | float |
| 3 | last_price | float |
| 4 | bid_size | int |
| 5 | ask_size | int |
| 8 | total_volume | int |
| 10 | high | float |
| 11 | low | float |
| 12 | close | float |
| 17 | open | float |
| 18 | net_change | float |
| 33 | mark | float |
| 42 | net_pct_change | float |

**Thread safety**: `StreamState` uses `threading.Lock` for concurrent access.

**Used by**: Real-time volume delta computation, uptick/downtick analysis

---

## Derived Computations

Built on top of raw Schwab data:

| Method | Description | Source Data |
|--------|-------------|-------------|
| `check_price_spike()` | Detects >0.5% moves in last 5 min | Intraday candles |
| `check_volume_regime()` | Detects volume >2x session average | Intraday candles |
| `compute_volume_delta()` | Uptick/downtick volume using inter-bar tick rule | Intraday 1-min candles |
| `build_price_context()` | Quotes + recent candles per symbol | Quotes + candles |
| `build_market_context()` | SPY, VIX, session info | Multiple quotes + market hours |

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

- No historical options pricing data
- No level-2 (depth of market) data
- Some data feeds may have 15-minute delays
- OAuth token refresh requires periodic re-authentication
- No sector/industry in fundamental data projection (we backfill from yfinance)

---

## What Else Schwab Offers (Not Currently Used)

| Endpoint | Description | Status |
|----------|-------------|--------|
| `accounts()` | Account balances, positions | Available (trading) |
| `orders()` | Place/manage orders | Available (trading) |
| `transactions()` | Transaction history | Available |
| `instruments()` with `search` | Instrument search by name | Available |
| Level-2 data | Depth of market | Not available via API |
| Options streaming | Real-time options quotes | Available |
| Futures data | Futures quotes/history | Available |

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
