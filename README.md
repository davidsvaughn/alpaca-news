# alpaca-news

LLM-powered day-trading research assistant. Monitors real-time news feeds, triages articles with LLMs, runs multi-agent investigation pipelines, and produces structured trading signals — all served through a live dashboard.

## Setup

```bash
git clone https://github.com/davidsvaughn/alpaca-news.git
cd alpaca-news

uv venv --python /usr/bin/python3.12
source .venv/bin/activate
uv sync
```

Copy and configure environment:

```bash
cp .env.example .env
# Edit .env — set API keys and preferences (see .env.example for all options)
```

## Quickstart

```bash
uv run python -m trader.main
```

This starts everything:
- **Dashboard** at http://127.0.0.1:8000/
- **Orchestrator** (watches news dirs, runs triage + exploration pipeline)
- **Insight Sentry websocket** (real-time news stream, auto-started by default)

To run in the background:

```bash
BACKFILL_ON_START=false nohup uv run python -m trader.main > /tmp/alpaca-dashboard.log 2>&1 &
```

To stop (kills app + all child processes):

```bash
pkill -f "trader.main"
```

The app uses a process group, so SIGTERM to the main process kills all children
(websockets, worker threads, etc.) automatically. If that doesn't work (e.g. the
process hung and the signal handler didn't fire), use the manual fallback:

```bash
pkill -f "trader.main"; pkill -f "insight_sentry_news"; pkill -f "alpaca_news.py"
```

On startup, the app also kills any stale Python servers left on ports 8000/8765
from previous sessions.

### News websockets

News websocket connections are launched automatically as subprocesses, controlled by env vars:

| Env var | Default | Description |
|---------|---------|-------------|
| `WS_INSIGHT_SENTRY` | `true` | Insight Sentry news stream → `output/insight_sentry/` |
| `WS_ALPACA` | `false` | Alpaca news stream → `output/alpaca/` |

They terminate automatically when the trader app exits. To run standalone:

```bash
uv run python -u websocket/insight_sentry_news.py
uv run python websocket/alpaca_news.py
```

### Schwab reauthorization

Schwab OAuth refresh tokens expire every 7 days. On startup, the app checks token status and automatically opens a reauth terminal if expired. You can also reauth manually:

```bash
uv run python scripts/schwab_reauth.py
```

Or use the **Reauthorize Schwab** button on the Config page in the dashboard. The token status (OK / expiring soon / expired) is shown there as well.

### Tick Collector (Schwab streaming → TimescaleDB)

Separate process that streams real-time trade data for portfolio symbols. See [docs/TICK-COLLECTOR.md](docs/TICK-COLLECTOR.md) for full details.

```bash
# Start (singleton — duplicate launches exit immediately)
uv run python -m tick_collector

# Stop
pkill -f "tick_collector"

# Restart
pkill -f "tick_collector"; sleep 1; uv run python -m tick_collector
```

Requires TimescaleDB running (`docker compose up -d`) and Schwab credentials in `.env`.

### Error logging

The trader app writes warnings and errors to a rotating log file. **Check this first when diagnosing issues:**

```bash
cat logs/trader.log        # view recent errors
tail -f logs/trader.log    # tail live
```

Configured via env vars: `LOG_FILE` (default: `logs/trader.log`), `LOG_LEVEL` (default: `WARNING`), `LOG_KEEP_DAYS` (default: `14`). Rotates daily at midnight. See `trader/logging_config.py`.

### Mock mode (no API keys needed)

```bash
MOCK_LLM=true SCHWAB_DISABLED=true uv run python -m trader.main
```

## Key API keys

Set in `.env` (at least one LLM key required for real mode):

| Key | Purpose |
|-----|---------|
| `OPENAI_API_KEY` | OpenAI pipeline agent |
| `XAI_API_KEY` | Grok/xAI pipeline agent + x_search |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini synthesis agent |
| `INSIGHT_SENTRY_API_KEY` | Insight Sentry news websocket |
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | Alpaca news websocket |
| `FINNHUB_API_KEY` | FinnHub company news (optional) |

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture, components, data models, project structure |
| [docs/LIVE-TRADING.md](docs/LIVE-TRADING.md) | Live trading plan and implementation |
| [docs/ALPACA-TRADING.md](docs/ALPACA-TRADING.md) | Alpaca order execution, multi-account, fills |
| [docs/TICK-COLLECTOR.md](docs/TICK-COLLECTOR.md) | Tick collector service, TimescaleDB, roadmap |
| [docs/BACKTEST-STRATEGIES.md](docs/BACKTEST-STRATEGIES.md) | Backtest architecture, strategies, metrics |

## Dependency groups

```bash
uv sync                  # core
uv sync --group dev      # pytest, pytest-asyncio
uv sync --group classify # classify/ workflows
uv sync --group ft       # ft/ fine-tuning workflows
```

## Tests

```bash
uv run python -m pytest tests/ -v -s
```
