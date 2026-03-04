# yfinance Data Source

> **Status**: Active (free, no API key required)
> **Package**: `yfinance>=0.2.0` + `stockstats>=0.6.0`
> **Client**: [`trader/market/yfinance_client.py`](trader/market/yfinance_client.py)
> **Env vars**: None (completely free, no authentication)
> **Demo**: [`demo/yfinance_demo.py`](demo/yfinance_demo.py) — `uv run python demo/yfinance_demo.py [SYMBOL]`

---

## Overview

yfinance is our **free fallback market data source**. It provides comprehensive financial data from Yahoo Finance without any API key or authentication. It serves two roles:

1. **Fallback** for Schwab — when Schwab is unavailable or disabled, yfinance provides quotes, fundamentals, and price history
2. **Primary source** for data Schwab doesn't offer — insider transactions, financial statements, company news, and technical indicators

---

## What We Currently Pull

### 1. Company Fundamentals (`ticker.info`)

**Our method**: `YFinanceClient.get_fundamentals(symbol)`
**Tool**: `get_fundamentals` (via `MarketDataService` fallback)

| Field | yfinance Key | Description |
|-------|-------------|-------------|
| `name` | `longName` | Company name |
| `sector` | `sector` | Sector classification |
| `industry` | `industry` | Industry classification |
| `market_cap` | `marketCap` | Market capitalization |
| `pe_ratio` | `trailingPE` | P/E ratio (TTM) |
| `forward_pe` | `forwardPE` | Forward P/E |
| `eps` | `trailingEps` | EPS (TTM) |
| `forward_eps` | `forwardEps` | Forward EPS |
| `dividend_yield` | `dividendYield` | Dividend yield |
| `beta` | `beta` | Beta coefficient |
| `week_52_high` / `week_52_low` | `fiftyTwoWeekHigh/Low` | 52-week range |
| `avg_50d` / `avg_200d` | `fiftyDayAverage` / `twoHundredDayAverage` | Moving averages |
| `debt_to_equity` | `debtToEquity` | D/E ratio |
| `current_ratio` | `currentRatio` | Current ratio |
| `profit_margin` | `profitMargins` | Profit margin |
| `return_on_equity` | `returnOnEquity` | ROE |
| `free_cash_flow` | `freeCashflow` | Free cash flow |
| `revenue` | `totalRevenue` | Total revenue |

**Note**: yfinance provides `sector` and `industry` which Schwab does not — these are used to backfill Schwab's fundamentals.

### 2. Insider Transactions (`ticker.insider_transactions`)

**Our method**: `YFinanceClient.check_insider_activity(symbol)`
**Tool**: `check_insider_activity` (modality: `fundamentals`)

| Field | Source Key | Description |
|-------|-----------|-------------|
| `insider_name` | `Insider` | Person's name |
| `title` | `Position` | CEO, Director, etc. |
| `action` | Parsed from `Text` | Buy, Sale, Gift, Option Exercise |
| `shares` | `Shares` | Number of shares |
| `value` | `Value` | Dollar value |
| `date` | `Start Date` | Transaction date |
| `ownership_type` | `Ownership` | Direct/Indirect |

**Computed summary**: `total_transactions`, `buy_count`, `sell_count`, `net_buy_value`, `signal` (net_buying/net_selling/neutral)

**Note**: This is yfinance-only — Schwab doesn't provide insider data. Finnhub has `/stock/insider-transactions` (free) but we use yfinance.

### 3. Price History (`ticker.history()`)

**Our method**: `YFinanceClient.get_price_history(symbol, period, interval)`
**Tool**: `get_price_history` (modality: `price`)

| Field | Type | Description |
|-------|------|-------------|
| `date` | str | ISO timestamp or date string |
| `o`, `h`, `l`, `c` | float | OHLC (rounded to 4 decimals) |
| `v` | int | Volume |

**Supported params**:
- `period`: "1d", "5d", "1mo", "3mo", "6mo", "1y"
- `interval`: "1m", "5m", "15m", "1h", "1d"

**Fallback role**: Schwab handles intraday 1-min candles; yfinance handles all other periods/intervals.

### 4. Financial Statements

**Our method**: `YFinanceClient.get_financial_statements(symbol, statement, freq, periods)`
**Tool**: `get_financial_statements` (modality: `fundamentals`)

Three statement types via `ticker.get_income_stmt()`, `ticker.get_balance_sheet()`, `ticker.get_cash_flow()`:

**Income Statement** fields: TotalRevenue, CostOfRevenue, GrossProfit, OperatingIncome/EBIT, EBITDA, NetIncome, DilutedEPS

**Balance Sheet** fields: TotalAssets, CurrentAssets, Cash, TotalLiabilities, CurrentLiabilities, TotalDebt, StockholdersEquity, WorkingCapital

**Cash Flow** fields: OperatingCashFlow, CapitalExpenditure, FreeCashFlow, RepurchaseOfCapitalStock, CashDividendsPaid

**Params**: `freq` ("quarterly" or "yearly"), `periods` (default 4)

### 5. Company News (`ticker.get_news()`)

**Our method**: `YFinanceClient.get_company_news(symbol, max_articles)`
**Tool**: `get_company_news` (modality: `news`)

| Field | Type | Description |
|-------|------|-------------|
| `title` | str | Article title |
| `summary` | str | Article summary |
| `publisher` | str | Provider display name |
| `url` | str | Article URL |
| `published_at` | str | ISO timestamp |

**Note**: yfinance news comes from Yahoo's aggregated feed. Structure is nested: `article.content.title`, `article.content.provider.displayName`, etc.

### 6. Analyst Price Targets (`ticker.analyst_price_targets`)

**Used directly in**: `prefetch_market_data()` in `prompt_builder.py`

| Field | Type | Description |
|-------|------|-------------|
| `mean` / `current` | float | Mean analyst target price |
| `high` | float | Highest analyst target |
| `low` | float | Lowest analyst target |

**Computed**: Upside/downside to mean target vs current price.

**Note**: Called directly via `yf.Ticker(sym).analyst_price_targets` in `prompt_builder.py`, not via `YFinanceClient`.

### 7. Ownership Summary (`ticker.major_holders`)

**Used directly in**: `prefetch_market_data()` in `prompt_builder.py`

Returns a DataFrame with rows like:
- "% of Shares Held by All Insider"
- "% of Shares Held by Institutions"
- "% of Float Held by Institutions"
- "Number of Institutions Holding Shares"

**Note**: Called directly via `yf.Ticker(sym).major_holders` in `prompt_builder.py`, not via `YFinanceClient`.

### 8. Technical Indicators (via `stockstats`)

**Our module**: [`trader/market/indicators.py`](trader/market/indicators.py)
**Tool**: `get_technical_indicators` (modality: `technicals`)

Computed from `ticker.history(period="1y")` data using `stockstats.wrap()`:

| Indicator | stockstats Key | Description |
|-----------|---------------|-------------|
| SMA 50 | `close_50_sma` | 50-day simple moving average |
| SMA 200 | `close_200_sma` | 200-day SMA |
| EMA 10 | `close_10_ema` | 10-day exponential MA |
| MACD | `macd` | MACD line |
| MACD Signal | `macds` | Signal line |
| MACD Histogram | `macdh` | Histogram |
| RSI | `rsi` | Relative Strength Index (14) |
| Bollinger Middle | `boll` | 20-day SMA |
| Bollinger Upper | `boll_ub` | Upper band |
| Bollinger Lower | `boll_lb` | Lower band |
| ATR | `atr` | Average True Range |
| VWMA | `vwma` | Volume-Weighted MA |
| MFI | `mfi` | Money Flow Index |

---

## How It's Wrapped

### Data Flow

```
yfinance (yahoo finance API)
       ↓
YFinanceClient (trader/market/yfinance_client.py)
       ↓
MarketDataService (trader/market/data_service.py) — fallback role
       ↓
tool_core.py tool functions → JSON strings for agents
```

### Error Handling

All methods follow this pattern:
```python
try:
    # fetch data
    return result_dict
except Exception as e:
    if DEBUG: raise
    return {"symbol": symbol, "error": str(e), "fetched_at": _now_iso()}
```

Agents can handle error dicts gracefully.

---

## Pricing

**yfinance is completely free.** No API key, no authentication, no rate limits (beyond Yahoo's implicit throttling).

| Aspect | Details |
|--------|---------|
| **Cost** | $0 |
| **API key** | Not required |
| **Rate limits** | Implicit (Yahoo may throttle heavy usage) |
| **Data quality** | 15-min delayed quotes; historical data is accurate |
| **Coverage** | US stocks, international, crypto, ETFs, indices |
| **Reliability** | Unofficial API — Yahoo can change without notice |

### Risks

- **Unofficial**: yfinance scrapes Yahoo Finance — it's not a supported API. Yahoo could break it at any time.
- **No SLA**: No guaranteed uptime or data quality
- **Rate limiting**: Heavy usage may get temporarily blocked by Yahoo
- **Data gaps**: Some fields may be `None` for smaller companies

This is why we use yfinance as a **fallback** rather than primary source.

---

## What Else yfinance Offers (Not Currently Used)

| Method | Description | Notes |
|--------|-------------|-------|
| `ticker.options` | Options expiration dates | Available |
| `ticker.option_chain(date)` | Full options chain (calls + puts) | We use Schwab for this |
| `ticker.calendar` | Earnings/dividend calendar | Available |
| `ticker.recommendations` | Analyst recommendations | We use Finnhub instead |
| `ticker.institutional_holders` | Detailed institutional holdings | Available |
| `ticker.sustainability` | ESG scores | Available |
| `yf.Sector` / `yf.Industry` | Sector/industry overviews | Available |
| `yf.Search(query)` | Search for tickers/news | Used in legacy code only |
| `ticker.dividends` | Dividend history | Available |
| `ticker.splits` | Stock split history | Available |
| `ticker.earnings_dates` | Historical earnings dates | Available |

### Potential Additions

- **`institutional_holders`** — detailed institutional ownership changes as a signal
- **`earnings_dates`** — alternative to Finnhub earnings calendar

---

## Configuration

No configuration needed. Just install the package:

```toml
# pyproject.toml
yfinance>=0.2.0
stockstats>=0.6.0
```

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/market/yfinance_client.py`](trader/market/yfinance_client.py) | `YFinanceClient` — all yfinance API calls |
| [`trader/market/indicators.py`](trader/market/indicators.py) | Technical indicator computation via stockstats |
| [`trader/market/data_service.py`](trader/market/data_service.py) | `MarketDataService` — Schwab-first with yfinance fallback |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | Tool functions that delegate to `MarketDataService` |

---

## References

- [yfinance GitHub](https://github.com/ranaroussi/yfinance)
- [yfinance Documentation](https://ranaroussi.github.io/yfinance/)
- [stockstats GitHub](https://github.com/jealous/stockstats)
