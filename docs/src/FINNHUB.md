# Finnhub Data Source

> **Status**: Active (free tier)
> **Package**: None (raw HTTP via `httpx`)
> **Client**: [`trader/market/finnhub_client.py`](trader/market/finnhub_client.py)
> **Env var**: `FINNHUB_API_KEY` (optional — tools degrade gracefully without it)
> **Demo**: [`demo/finnhub_demo.py`](demo/finnhub_demo.py) — `uv run python demo/finnhub_demo.py [SYMBOL]`

---

## What We Currently Pull

### 1. Company News (`/company-news`)

**Our function**: `get_company_news(symbol, days_back=3)`
**Tool**: `get_finnhub_news` (modality: `news`)

Fetches recent news articles for a specific stock ticker.

**Fields extracted**:
| Field | Type | Description |
|-------|------|-------------|
| `headline` | str | Article title |
| `summary` | str | Snippet (truncated to 300 chars in tool) |
| `source` | str | Publisher (Reuters, AP, etc.) |
| `datetime` | int | Unix timestamp |
| `url` | str | Full article URL |
| `related` | str | Comma-separated related tickers |
| `category` | str | e.g. "earnings", "company event" |
| `id` | str | Article ID |

**Usage**: Pre-fetched for every agent prompt (`_fetch_finnhub_context()` in `prompt_builder.py`), and also available as an on-demand agent tool. Max 15 articles per call, max 7 days back.

### 2. Earnings Surprises (`/stock/earnings`)

**Our function**: `get_earnings_surprises(symbol, limit=4)`

Fetches historical earnings beat/miss data for the last N quarters.

**Fields extracted**:
| Field | Type | Description |
|-------|------|-------------|
| `actual` | float | Actual EPS |
| `estimate` | float | Estimated EPS |
| `surprise` | float | Difference (actual - estimate) |
| `surprisePercent` | float | Percentage surprise |
| `period` | str | e.g. "Q4 2024" |
| `quarter` | int | Quarter number (1-4) |
| `year` | int | Year |
| `symbol` | str | Ticker |

**Usage**: Pre-fetched for every agent prompt (`_fetch_earnings_context()` in `prompt_builder.py`). Formatted into narrative showing beat/miss track record.

### 3. Earnings Calendar (`/calendar/earnings`)

**Our function**: `get_earnings_calendar(symbol)`

Fetches upcoming/recent earnings dates and estimates.

**Fields extracted**:
| Field | Type | Description |
|-------|------|-------------|
| `date` | str | "YYYY-MM-DD" |
| `epsActual` | float\|null | Actual EPS (null if upcoming) |
| `epsEstimate` | float\|null | Consensus EPS estimate |
| `hour` | str | "bmo" (before open), "amc" (after close), "dmh" (during hours) |
| `quarter` | int | Quarter |
| `year` | int | Year |
| `revenueActual` | float\|null | Revenue (millions) |
| `revenueEstimate` | float\|null | Revenue estimate (millions) |
| `symbol` | str | Ticker |

**Note**: Response is wrapped in `{"earningsCalendar": [...]}`.

**Usage**: Pre-fetched alongside earnings surprises for prompt context.

### 4. Recommendation Trends (`/stock/recommendation`)

**Our function**: `get_recommendation_trends(symbol)`
**Tool**: `get_analyst_ratings` (modality: `fundamentals`)

Fetches monthly analyst consensus data.

**Fields extracted**:
| Field | Type | Description |
|-------|------|-------------|
| `buy` | int | Buy recommendations |
| `hold` | int | Hold recommendations |
| `sell` | int | Sell recommendations |
| `strongBuy` | int | Strong buy recommendations |
| `strongSell` | int | Strong sell recommendations |
| `period` | str | "YYYY-MM" format |
| `symbol` | str | Ticker |

**Usage**: Available as agent tool. Tool computes `total_analysts` and limits to 6 most recent months.

### 5. Stock Metrics (`/stock/metric`)

**Our function**: `get_stock_metrics(symbol)`
**Formatter**: `format_metrics_for_prompt(metrics)`

Fetches 132 financial metrics; we cherry-pick 14 growth, valuation, and quality indicators.

**Fields extracted (Tier 1 — Growth & Relative Strength)**:
| API Key | Label | Description |
|---------|-------|-------------|
| `epsGrowthTTMYoy` | EPS Growth (TTM YoY) | Trailing twelve months EPS growth rate |
| `revenueGrowthTTMYoy` | Revenue Growth (TTM YoY) | TTM revenue growth rate |
| `revenueGrowthQuarterlyYoy` | Revenue Growth (Q YoY) | Quarterly revenue acceleration |
| `pegTTM` | PEG Ratio | Price/Earnings-to-Growth — single best value-for-growth metric |
| `psTTM` | Price/Sales | Price-to-Sales — essential for pre-profit names |
| `evEbitdaTTM` | EV/EBITDA | Enterprise Value / EBITDA — institutional standard valuation |
| `priceRelativeToS&P50013Week` | vs S&P 500 (13W) | 13-week relative performance vs market |
| `priceRelativeToS&P50026Week` | vs S&P 500 (26W) | 26-week relative performance vs market |
| `priceRelativeToS&P50052Week` | vs S&P 500 (52W) | 52-week relative performance vs market |

**Fields extracted (Tier 2 — Profitability & Quality)**:
| API Key | Label | Description |
|---------|-------|-------------|
| `grossMarginTTM` | Gross Margin | TTM gross margin — pricing power indicator |
| `operatingMarginTTM` | Operating Margin | TTM operating margin — core efficiency |
| `roaTTM` | ROA | Return on Assets — less leverage-distorted than ROE |
| `epsGrowthQuarterlyYoy` | EPS Growth (Q YoY) | Quarterly EPS acceleration |
| `cashFlowPerShareTTM` | CF/Share | Operating cash flow per share |

**Usage**: Pre-fetched for every agent prompt (`prefetch_market_data()` → "Growth & Valuation" section). Not available as an on-demand agent tool.

### 6. Insider Transactions (`/stock/insider-transactions`)

**Our function**: `get_insider_transactions(symbol)`
**Tool**: `check_insider_activity` (modality: `fundamentals`) — via `data_service.py`

Fetches individual SEC Form 4 insider trades. Replaces yfinance as primary source (richer structure, standardized SEC codes, more recent data). Enriched with job titles from yfinance. Falls back to yfinance-only if Finnhub unavailable.

**Fields extracted**:
| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Insider's name |
| `change` | int | Shares bought (+) or sold (-) |
| `share` | int | Total shares held after transaction |
| `transactionDate` | str | Date of transaction |
| `transactionCode` | str | SEC Form 4 code (see below) |
| `transactionPrice` | float | Average price per share |
| `filingDate` | str | SEC filing date |
| `isDerivative` | bool | Whether the transaction is a derivative |
| `id` | str | SEC filing ID |

**Transaction codes**: `P`=purchase, `S`=sale, `A`=award/grant, `M`=option exercise, `F`=tax withholding, `G`=gift, `X`=option exercise, `C`=conversion, `D`=disposition.

**Why Finnhub over yfinance**: More records (117 vs 75 for AAPL), more recent data (1-2 days fresher), standardized SEC transaction codes (vs often-empty free text), filing-level detail (filing ID, derivative flag, post-transaction holdings).

**Usage**: Primary source for `check_insider_activity` tool. Pre-fetched for agent prompts and available as on-demand tool.

---

## How It's Wrapped

### Low-Level Client (`finnhub_client.py`)

All API calls use plain `httpx.get()`:

```python
# Base URL
_BASE_URL = "https://finnhub.io/api/v1"

# Every request follows this pattern:
resp = httpx.get(f"{_BASE_URL}/{endpoint}", params={...,"token": api_key}, timeout=10)
resp.raise_for_status()
return resp.json()
```

**Error handling**: Silent — all exceptions caught, returns empty list `[]`. This enables graceful degradation when the API key is missing or rate limited.

### Tool Layer (`tool_core.py`)

Two tools exposed in `TOOL_REGISTRY`:
- `get_finnhub_news(market, symbol, days_back=3)` → JSON string
- `get_analyst_ratings(market, symbol)` → JSON string

### Prompt Builder (`prompt_builder.py`)

Three auto-fetch functions inject Finnhub data into every agent message:
- `_fetch_finnhub_context(symbols)` — recent news (max 3 symbols, 10 articles each)
- `_fetch_earnings_context(symbols)` — surprises + calendar (max 3 symbols)
- `prefetch_market_data()` → injects "Growth & Valuation" section from `get_stock_metrics()`

### Prefetch Optimization

Both Finnhub tools are listed in `_PREFETCHED_TOOLS` (`agent_pipeline.py`), which prevents agents from redundantly calling tools for data already injected into prompts.

---

## Pricing Tiers

| Tier | Price | Rate Limit | Key Features |
|------|-------|------------|--------------|
| **Free** | $0/mo | 60 req/min | Company news, earnings, recommendations, basic market data |
| **Market Data Basic** | $49.99/mo | Higher limits | Real-time quotes, more exchanges |
| **Market Data Standard** | $129.99/mo | Higher limits | Full exchange coverage |
| **Fundamental** | $50–$200/mo | Higher limits | Insider sentiment, price targets, upgrades/downgrades |
| **All-in-One** | $3,000/mo | Custom | Everything — full global access |

Source: [finnhub.io/pricing](https://finnhub.io/pricing)

### What We Get on Free Tier

- Company news (`/company-news`) — works
- Earnings surprises (`/stock/earnings`) — works
- Earnings calendar (`/calendar/earnings`) — works
- Analyst recommendations (`/stock/recommendation`) — works
- Stock metrics (`/stock/metric`) — works (132 metrics, we use 14)
- Insider transactions (`/stock/insider-transactions`) — works
- 60 requests per minute shared across all calls

### What's Blocked on Free Tier (returns 403)

- **Insider sentiment** (`/stock/insider-sentiment`) — institutional-grade insider flow
- **Price targets** (`/stock/price-target`) — analyst consensus price targets
- **Upgrades/downgrades** (`/stock/upgrade-downgrade`) — analyst rating changes
- **SEC filings** (`/stock/filings`) — SEC EDGAR filings feed
- **Social sentiment** (`/stock/social-sentiment`) — social media sentiment scores
- **Lobbying data** — political lobbying activity
- **Congressional trading** — US Congress member stock trades

### What We'd Get with Fundamental Tier ($50/mo)

The "Fundamental 1" tier at $50/mo would unlock:
- Insider sentiment — high-signal for detecting institutional moves
- Price targets — useful for analyst consensus context
- Upgrades/downgrades — directly relevant to our news triage (analyst action events)
- Enhanced fundamentals — more detailed company metrics

This is the most impactful single upgrade for our pipeline, since analyst upgrades/downgrades and insider sentiment are directly relevant to the news events we process.

---

## What Else Finnhub Offers (Not Currently Used)

| Endpoint | Description | Tier |
|----------|-------------|------|
| `/stock/insider-sentiment` | Net insider buying/selling aggregates | Paid |
| `/stock/price-target` | Analyst consensus target, high, low, median | Paid |
| `/stock/upgrade-downgrade` | Recent analyst rating changes | Paid |
| `/stock/social-sentiment` | Reddit + Twitter sentiment scores | Paid |
| `/stock/filings` | SEC filings feed | Paid |
| `/stock/profile2` | Company profile (sector, industry, etc.) | Free |
| `/stock/peers` | Similar companies list | Free |
| `/forex/rates` | Real-time FX rates | Free |
| `/crypto/candle` | Crypto OHLCV data | Free |
| `/news` | General market news (not company-specific) | Free |
| `/calendar/ipo` | IPO calendar | Free |
| `/stock/revenue-breakdown` | Revenue by segment/geography | Paid |

### Free Endpoints We Could Add

- `/stock/profile2` — company profile (sector, industry, market cap, IPO date)
- `/stock/peers` — similar companies for sector context

---

## Configuration

```bash
# .env
FINNHUB_API_KEY=<your-key-here>  # Optional — tools return empty on missing key
```

No other configuration needed. Rate limits enforced server-side (429 responses). All calls use 10-second timeout.

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/market/finnhub_client.py`](trader/market/finnhub_client.py) | 6 API wrapper functions + 3 formatters |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | `get_finnhub_news`, `get_analyst_ratings` tools |
| [`trader/online/prompt_builder.py`](trader/online/prompt_builder.py) | Auto-fetch + format for agent prompts |
| [`trader/online/explorer_agent.py`](trader/online/explorer_agent.py) | PydanticAI tool wrappers (for watcher) |
