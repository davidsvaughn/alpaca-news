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

## Running the System

The system has two independent processes: the **trader app** and the **tick collector**. They communicate only through a shared SQLite database (`data/trader.db`).

### Trader App

The trader app is the main process. It runs the dashboard, orchestrator, news websockets, and live trading logic.

**Start (foreground):**

```bash
uv run python -m trader.main
```

This launches:
- **Dashboard** at http://127.0.0.1:8000/
- **Orchestrator** (watches news dirs, runs triage + exploration pipeline)
- **News websockets** as child subprocesses (see below)

**Start (background):**

```bash
BACKFILL_ON_START=false nohup uv run python -m trader.main > /tmp/alpaca-dashboard.log 2>&1 &
```

**Stop:**

```bash
pkill -f "trader.main"
```

The app uses a process group, so SIGTERM to the main process kills all children (websockets, worker threads, etc.) automatically. On startup, the app also kills any stale Python servers left on ports 8000/8765 from previous sessions.

**Kill (if stop hangs):**

If the process hung and the signal handler didn't fire, kill everything manually:

```bash
pkill -f "trader.main"; pkill -f "insight_sentry_news"; pkill -f "alpaca_news.py"
```

**Restart:**

```bash
pkill -f "trader.main"; sleep 2; uv run python -m trader.main
```

Or for background:

```bash
pkill -f "trader.main"; sleep 2; BACKFILL_ON_START=false nohup uv run python -m trader.main > /tmp/alpaca-dashboard.log 2>&1 &
```

### News Websockets

News websocket connections are launched automatically as child subprocesses of the trader app, controlled by env vars:

| Env var | Default | Description |
|---------|---------|-------------|
| `WS_INSIGHT_SENTRY` | `true` | Insight Sentry news stream → `output/insight_sentry/` |
| `WS_ALPACA` | `false` | Alpaca news stream → `output/alpaca/` |

They terminate automatically when the trader app exits. If they don't (orphaned processes), kill them manually:

```bash
pkill -f "insight_sentry_news"   # kill Insight Sentry websocket
pkill -f "alpaca_news.py"        # kill Alpaca websocket
```

To run standalone (outside the trader app):

```bash
uv run python -u websocket/insight_sentry_news.py
uv run python websocket/alpaca_news.py
```

### Tick Collector (Schwab L1 → TimescaleDB)

Separate, independent process that streams real-time L1 trade data from Schwab for portfolio symbols into TimescaleDB. **Not a child of the trader app** — must be started/stopped separately. See [docs/TICK-COLLECTOR.md](docs/TICK-COLLECTOR.md) for full details.

The tick collector reads `data/trader.db` to discover which symbols to stream (portfolio sync every 30s). Start the trader app first so holdings are populated, though existing holdings in the DB persist across restarts.

**Prerequisites — TimescaleDB:**

```bash
# Start TimescaleDB (Docker, runs on port 5433, restarts automatically)
docker compose up -d

# Check status
docker compose ps

# Stop (data persists in Docker volume)
docker compose down

# Stop and delete all data
docker compose down -v
```

The DB schema is auto-initialized from `tick_collector/init.sql` on first run.

**Start:**

```bash
uv run python -m tick_collector
```

Singleton lock — duplicate launches exit immediately with an error message.

**Stop:**

```bash
pkill -f "tick_collector"
```

**Restart:**

```bash
pkill -f "tick_collector"; sleep 1; uv run python -m tick_collector
```

**Health monitoring:** The tick collector has a built-in health check that monitors the Schwab WebSocket stream. If the stream dies (Schwab closes the connection), it automatically reconnects and replays all subscriptions. Status logs show `stream=active` or `stream=DEAD`:

```
14:18:28 Status [60s]: symbols=63 L1=440 flushed=432 pending=8 stream=active
```

After 10 consecutive failed restarts, it gives up and logs an error — manual intervention (process restart) is needed at that point.

Requires Schwab credentials (`SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`) in `.env`.

### Schwab Reauthorization

Schwab OAuth refresh tokens expire every 7 days. On startup, the app checks token status and automatically opens a reauth terminal if expired. You can also reauth manually:

```bash
uv run python scripts/schwab_reauth.py
```

Or use the **Reauthorize Schwab** button on the Config page in the dashboard. The token status (OK / expiring soon / expired) is shown there as well.

**Both the trader app and tick collector use Schwab tokens.** If the token expires, both will need a reauth followed by restart.

### Startup Order (recommended)

1. **TimescaleDB**: `docker compose up -d` (if not already running)
2. **Trader app**: `uv run python -m trader.main` (populates holdings in `trader.db`)
3. **Tick collector**: `uv run python -m tick_collector` (reads holdings, starts streaming)

In practice, `trader.db` persists on disk, so if the trader was previously running the tick collector can start in any order. But on a fresh start, the trader should go first.

### Error Logging

The trader app writes warnings and errors to a rotating log file. **Check this first when diagnosing issues:**

```bash
cat logs/trader.log        # view recent errors
tail -f logs/trader.log    # tail live
```

Configured via env vars: `LOG_FILE` (default: `logs/trader.log`), `LOG_LEVEL` (default: `WARNING`), `LOG_KEEP_DAYS` (default: `14`). Rotates daily at midnight. See `trader/logging_config.py`.

The tick collector logs to stdout/stderr (visible in the terminal or nohup log).

### Mock Mode (no API keys needed)

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
