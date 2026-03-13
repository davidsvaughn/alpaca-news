# Logging: Files, Config & Searching

> Where log files live, what they capture, how long they persist, and how to search them.

## Quick reference

| Log file | Source | Default level | Rotation | Retention | Config env vars |
|----------|--------|---------------|----------|-----------|-----------------|
| `logs/trader.log` | Trader app (`uv run python -m trader.main`) | INFO | Daily at midnight | 14 days | `LOG_FILE`, `LOG_LEVEL`, `LOG_KEEP_DAYS` |
| `logs/tick_collector.log` | Tick collector (`uv run python -m tick_collector`) | INFO | Daily at midnight | 14 days | `TC_LOG_FILE`, `TC_LOG_LEVEL`, `TC_LOG_KEEP_DAYS` |
| `logs/notifications.md` | `trader.notifications.notify()` | Always written | None | Manual cleanup | N/A |

Rotated files are named `trader.log.2026-03-12`, `tick_collector.log.2026-03-11`, etc.

## Setup code

| App | Config file | Function |
|-----|-------------|----------|
| Trader | [trader/logging_config.py](../../trader/logging_config.py) | `setup_logging()` |
| Tick collector | [tick_collector/__main__.py](../../tick_collector/__main__.py) | `_setup_logging()` |
| Websocket (Sentry) | [websocket/insight_sentry_news.py](../../websocket/insight_sentry_news.py) | Inline `FileHandler` → `logs/trader.log` |
| Websocket (Alpaca) | [websocket/alpaca_news.py](../../websocket/alpaca_news.py) | Inline `FileHandler` → `logs/trader.log` |

Trader and tick collector follow the same pattern: root logger at DEBUG, console handler at INFO, file handler at configurable level (default INFO). Websocket subprocesses append to `logs/trader.log` via a plain `FileHandler` (no rotation — the parent process handles that).

## What each log captures

### `logs/trader.log`

Everything from the trader app at INFO+:

| Logger name | What it logs |
|-------------|-------------|
| `trader.online.live_monitor` | Exit checks, cooling off, watch creation, LIVE-PM buy/skip results |
| `trader.online.orchestrator` | Pipeline runs, watchdog events, snapshot creation, init failures |
| `trader.online.online_mode` | Online mode startup, scheduling |
| `trader.online.watcher` | Legacy watch check-ins, monitoring cycle errors |
| `trader.online.follow_up_collector` | Follow-up collection, query planner fallbacks |
| `trader.online.news_archive` | Archive/prune operations and failures |
| `trader.online.health_check` | Provider availability checks |
| `trader.online.agent_pipeline` | Pipeline agent failures, cost-based skips |
| `trader.online.feed_manager` | Websocket subprocess start/stop, watchdog schedule/unschedule |
| `trader.online.triage` | Pre-filter matches (DEBUG level) |
| `trader.online.symbol_filter` | Symbol removals (DEBUG level) |
| `trader.online.runners.*` | Grok/OpenAI/Gemini turn-by-turn progress, fallbacks |
| `trader.llm.cost_tracker` | Per-call cost breakdown (DEBUG level) |
| `trader.web.app` | Alpaca account sync, purge operations |
| `trader.market.alpaca_broker` | Buy/sell orders, fill confirmations, position checks |
| `trader.market.alpaca_stream` | WebSocket fill/cancel events |
| `trader.market.alpaca_reconcile` | Reconciliation actions on startup |
| `trader.market.schwab_client` | Stream message handler errors |
| `trader.market.schwab_tokens` | Reauth terminal warnings |
| `trader.market.backtest` | Exit strategy evaluations |
| `trader.main` | Startup, Schwab auth, backfill progress, X stream lifecycle |
| `trader.notifications` | Notification delivery |
| `trader.db.equity_backfill` | Equity snapshot backfill |
| `websocket.insight_sentry` | InsightSentry WS connect/disconnect, article saves |
| `websocket.alpaca` | Alpaca news WS article saves, errors |

**Suppressed loggers** (WARNING+ only in file): `httpx`, `httpcore`, `schwabdev`, `alpaca`, `uvicorn`, `yfinance` (CRITICAL only), `trader.market.volume_delta_shadow`.

**LIVE-EVAL/LIVE-PM debug messages**: Off by default. Set `LIVE_EVAL_VERBOSE=1` in `.env` to see step-by-step filter/allocation messages on console. These log at DEBUG level, so they appear in the log file only if `LOG_LEVEL=DEBUG`.

### `logs/tick_collector.log`

Everything from the tick collector at INFO+:

| Logger name | What it logs |
|-------------|-------------|
| `tick_collector.collector` | Stream events, subscriptions, restarts, health checks |
| `tick_collector.db` | Trade inserts, DB connections |
| `tick_collector.buffer` | Flush events, buffer stats |
| `tick_collector.portfolio` | Symbol sync from trader DB |
| `tick_collector.vdd` | VDD pool connections, query results |

**Suppressed loggers** (WARNING+ only): `schwabdev`, `asyncpg`.

### `logs/notifications.md`

Timestamped operator alerts. Written by `notify()` — always active, no log level filtering. Contains buy/sell failures, fix verifications, and anything explicitly flagged for operator attention.

## Searching logs

### Recent issues (last N minutes)

**Always read the tail of the log file first.** Do NOT grep for `ERROR`/`WARNING` — tracebacks are multi-line and grep will miss the stack trace lines. Read the raw tail to see the full picture:

```bash
# Read last 100-200 lines of each log (adjust as needed)
tail -200 logs/trader.log
tail -200 logs/tick_collector.log
```

Use grep only for targeted follow-up searches (specific symbol, time range, topic).

### By time window

Log file format: `YYYY-MM-DD HH:MM:SS [logger] LEVEL: message`

```bash
# Everything between 10:50 and 10:55 today
grep "^2026-03-13 10:5[0-5]" logs/trader.log

# All errors today
grep "^2026-03-13.*ERROR" logs/trader.log
```

### By topic

```bash
# All buy/sell activity
grep -i "buy\|sell\|fill\|order" logs/trader.log

# Schwab stream issues
grep -i "stream\|reconnect\|streamerInfo" logs/tick_collector.log

# VDD checks
grep "VDD\|vdd" logs/trader.log

# Exit strategy evaluations
grep "COOLING OFF\|EXIT\|exit" logs/trader.log

# Specific symbol
grep "AAPL" logs/trader.log
grep "AAPL" logs/tick_collector.log
```

### Historical (rotated files)

```bash
# Yesterday's trader errors
grep "ERROR" logs/trader.log.2026-03-12

# All tick collector errors from the last 3 days
grep "ERROR" logs/tick_collector.log*
```

## Completeness

All errors, warnings, informational messages, and tracebacks go through the logging module — no `print()` calls for operational messages. This means the log files are the **complete record** of everything that happened.

The only `print()` calls remaining are in `websocket/` scripts (the `saved ...` lines also go to the log file) and a few test/benchmark scripts (`trader/evidence/`).

## Gotchas

- **Console vs file**: Both show the same content at INFO+ by default. The console uses short time format (`HH:MM:SS`), the file uses full (`YYYY-MM-DD HH:MM:SS`).
- **Schwab/asyncpg are suppressed**: Set to WARNING+ in both apps. You won't see normal Schwab API calls or asyncpg queries — only errors.
- **LIVE-EVAL debug messages**: These are at DEBUG level. They don't appear in log files unless you set `LOG_LEVEL=DEBUG` (which would also capture all other DEBUG messages). Use `LIVE_EVAL_VERBOSE=1` to see them on console only.
- **Rotated file naming**: Files rotate at midnight. The current day's log is always `trader.log` / `tick_collector.log`. Yesterday's is `.log.2026-03-12`.
- **Multi-line tracebacks**: When using `exc_info=True` or `log.exception()`, the full traceback is part of the log record and captured in the file. Always read the tail of the log rather than grepping for `ERROR` — grep misses continuation lines.
- **`.env` overrides code defaults**: The `.env` file can set `LOG_LEVEL`, `TC_LOG_LEVEL`, etc. These take precedence over the code defaults. If logs seem empty or missing expected entries, check `.env` first — a stale override (e.g., `LOG_LEVEL=WARNING`) will silently suppress INFO messages even though the code default is INFO.

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — Full diagnostic guide: SQL queries, all data sources ranked, console prefixes
- [trader/logging_config.py](../../trader/logging_config.py) — Trader logging setup
- [tick_collector/__main__.py](../../tick_collector/__main__.py) — Tick collector logging setup
- [CLAUDE.md](../../CLAUDE.md) — Project-level logging summary
