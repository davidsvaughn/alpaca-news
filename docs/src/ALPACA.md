# Alpaca Data Source

> **Status**: Active (free tier)
> **Package**: `alpaca-py`
> **Client**: [`alpaca/news_websocket.py`](alpaca/news_websocket.py)
> **Env vars**: `ALPACA_API_KEY_1`, `ALPACA_SECRET_KEY_1`
> **Demo**: [`demo/alpaca_demo.py`](demo/alpaca_demo.py) — `uv run python demo/alpaca_demo.py [SYMBOL]` or `--stream`

---

## Overview

Alpaca is our **primary trigger source** — the entry point for the entire pipeline. We stream real-time financial news via Alpaca's WebSocket, which fires off the triage → investigation → prediction workflow. The news data comes from **Benzinga** via Alpaca's News API partnership.

---

## What We Currently Pull

### Real-Time News Stream (`NewsDataStream`)

**Our implementation**: `alpaca/news_websocket.py`

```python
from alpaca.data.live import NewsDataStream

stream = NewsDataStream(ALPACA_API_KEY_1, ALPACA_SECRET_KEY_1)
stream.subscribe_news(news_data_handler, "*")  # All symbols
stream.run()
```

Each news article arrives as a `News` Pydantic model, converted to a dict and saved as JSON:

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Alpaca article ID (unique) |
| `headline` | str | Article headline (HTML-unescaped) |
| `summary` | str | Article summary (HTML-unescaped) |
| `content` | str | Full HTML article content |
| `source` | str | Publisher (typically "benzinga") |
| `author` | str | Author name |
| `url` | str | Full article URL |
| `symbols` | list[str] | Associated stock tickers |
| `created_at` | str | ISO 8601 timestamp |
| `updated_at` | str | ISO 8601 timestamp |
| `images` | list | Article images (if any) |

**Output**: Files saved to `data/news/incoming/alpaca/{timestamp}_{article_id}.json`
**Example filename**: `2026-02-18T12-27-55Z_50682104.json`

---

## Pipeline Integration

### Flow

```
Alpaca WebSocket → data/news/incoming/alpaca/*.json
       ↓
Watchdog (orchestrator.py) detects new file → enqueues path
       ↓
Worker thread → loads JSON → creates Trigger
       ↓
Trigger → Triage → Pipeline → Snapshot
```

### Trigger Creation (`orchestrator.py`)

```python
trigger = Trigger(
    type="alpaca_news",
    alpaca_timestamp=str(news.get("created_at")),
    headline=str(news.get("headline") or ""),
    summary=str(news.get("summary") or ""),
    source=str(news.get("source") or ""),
    symbols=[str(x) for x in (news.get("symbols") or [])],
    raw=news,              # Full Alpaca JSON preserved
    source_file=path.name, # Original filename for traceability
)
```

### Deterministic Snapshot ID (`snapshot.py`)

Uses Alpaca's `id` field to generate a UUID v5:
```python
snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpaca-news:{alpaca_id}"))
```
This makes backfill idempotent — reprocessing the same news article produces the same snapshot ID.

### Triage Pre-Filters (`triage.py`)

Before LLM triage, we filter on Alpaca news fields:
- **Skip patterns**: "if you had invested", "dividend aristocrat", etc.
- **Skip authors**: "Benzinga Insights" (auto-generated retrospectives)
- **Source filtering**: Known auto-generated content sources
- **Symbol validation**: Check tickers are valid US equities

### Prompt Building (`prompt_builder.py`)

Agent prompts include all Alpaca news fields:
- Headline, summary, source, timestamp, URL
- Full article content (HTML stripped)
- Associated symbols

---

## Pricing

### Current Tier: Free

| Feature | Free | Algo Trader Plus ($99/mo) |
|---------|------|--------------------------|
| **API calls** | 200/min | Unlimited |
| **Stock quotes** | IEX only (15-min delay via API) | All US exchanges (real-time) |
| **Options quotes** | Indicative only | Real-time |
| **WebSocket symbols** | 30 symbols | Unlimited |
| **Historical data** | 7+ years | 7+ years |
| **News streaming** | Included | Included |
| **Extended hours** | Included | Included |
| **Corporate actions** | Included | Included |

Source: [alpaca.markets/data](https://alpaca.markets/data)

### What We Use

We use Alpaca **exclusively for news streaming** (`NewsDataStream`). We subscribe to `"*"` (all symbols), which works on the free tier. We do **not** use Alpaca for market data quotes — that's handled by Schwab/yfinance.

### Free Tier for Our Use Case

The free tier is sufficient because:
- News WebSocket is included (our primary use)
- We don't need Alpaca's market data (we use Schwab)
- We subscribe to all symbols via `"*"` wildcard
- 200 API calls/min is plenty (we only use WebSocket, not REST)

### What $99/mo Would Add

The Algo Trader Plus tier would provide:
- **Unlimited real-time quotes** from all US exchanges — but we already get this from Schwab for free
- **Real-time options quotes** — useful if we wanted a second options data source
- **Unlimited WebSocket symbols** — only relevant for market data streaming, which we don't use from Alpaca

**Verdict**: Not needed currently. We'd only upgrade if we wanted to use Alpaca as a market data source, replacing or supplementing Schwab.

---

## What Else Alpaca Offers (Not Currently Used)

### Market Data API

| Endpoint | Description | Our Alternative |
|----------|-------------|-----------------|
| Stock quotes (REST) | Real-time/delayed quotes | Schwab |
| Stock bars (REST) | Historical OHLCV | Schwab/yfinance |
| Stock trades | Tick-level trade data | Not needed |
| Stock snapshots | Multi-symbol snapshot | Schwab |
| Options data | Options quotes/chains | Schwab |
| Crypto data | Crypto quotes/bars | Not needed |

### Trading API

| Feature | Description | Status |
|---------|-------------|--------|
| Paper trading | Simulated trading | Available (free) |
| Live trading | Real brokerage | Available (commission-free) |
| Order management | Place/cancel/modify orders | Available |
| Account management | Positions, balances | Available |
| Portfolio history | Performance tracking | Available |

### News REST API

```python
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

news_client = NewsClient()
request = NewsRequest(symbols="AAPL", limit=50, include_content=True)
news = news_client.get_news(request)
```

We currently only use the WebSocket stream, not the REST API. The REST API provides:
- Historical news back to 2015
- Filtering by symbol, date, source
- Pagination for bulk retrieval
- Full article content with `include_content=True`

### Potential Additions

- **News REST API** — for backfilling historical news or enriching context
- **Paper trading** — for backtesting our signals without real money
- **Stock snapshots** — could supplement Schwab for multi-symbol context

---

## Configuration

```bash
# .env
ALPACA_API_KEY_1=<your-key-here>
ALPACA_SECRET_KEY_1=<your-secret-here>
ALPACA_NEWS_DIR=data/news/incoming/alpaca  # Where news JSON files are saved
```

---

## Key Files

| File | Purpose |
|------|---------|
| [`alpaca/news_websocket.py`](alpaca/news_websocket.py) | WebSocket streaming client |
| [`trader/models/snapshot.py`](trader/models/snapshot.py) | `Trigger` dataclass + deterministic ID generation |
| [`trader/online/orchestrator.py`](trader/online/orchestrator.py) | `process_news_file()` — loads JSON, creates Trigger |
| [`trader/online/triage.py`](trader/online/triage.py) | Pre-filters on news fields |
| [`trader/online/prompt_builder.py`](trader/online/prompt_builder.py) | Builds agent prompts from news data |
| [`trader/online/backfill.py`](trader/online/backfill.py) | Batch processor for existing files |

---

## References

- [Alpaca Documentation](https://docs.alpaca.markets/)
- [Alpaca News API](https://docs.alpaca.markets/docs/streaming-real-time-news)
- [alpaca-py GitHub](https://github.com/alpacahq/alpaca-py)
- [Alpaca Market Data Pricing](https://alpaca.markets/data)
