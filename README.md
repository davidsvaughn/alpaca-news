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

To stop:

```bash
pkill -f "trader.main"
```

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
| [docs/ROADMAP.md](docs/ROADMAP.md) | Implementation status and plan |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Design rationale, open questions, deferred ideas |
| [trader/README.md](trader/README.md) | Detailed trader module docs (explorer phases, backfill, knobs) |

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
