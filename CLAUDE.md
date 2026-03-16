# Project Guidelines

## Project Documentation

Docs live in `docs/`, organized by topic. Two legacy overview docs plus topic-specific docs:

- **`docs/ARCHITECTURE.md`** — High-level system map: components, data flow, project structure. May lag behind topic docs.
- **Topic docs** (the real living documentation):
  - `docs/LIVE-TRADING.md` — Live trading plan and implementation
  - `docs/ALPACA-TRADING.md` — Alpaca order execution, multi-account, fills
  - `docs/VOLUME-DELTA-REALTIME.md` — Shadow collector, Schwab streaming, tick-level VDD
  - `docs/VDD-COMPARISON.md` — Bar-based vs tick-level VDD comparison experiment
  - `docs/TICK-COLLECTOR.md` — Tick collector service: raw trade data, TimescaleDB, roadmap
  - `docs/VDD-REALTIME.md` — Real-time VDD from tick data: design, proportional distribution, sub-minute bucketing
  - `docs/BACKTEST-*.md` — Backtest architecture, strategies, metrics
  - `docs/ALLOCATION-STRATEGIES.md` — Portfolio allocation methods
  - `docs/RECONCILE-TEST-PLAN.md` — **TODO**: Stress test plan for reconciliation fixes (use Paper2/Paper3)
  - `scripts/take_profit_analysis.py` — Take-profit threshold analysis (run weekly as sample grows)

Reference papers are in `docs/refs/`. Archived older docs in `docs/archive/`.

- **`docs/skills/`** — **Agent knowledge base**: operational guides for debugging, Alpaca operations, and codebase navigation. **Read [docs/skills/README.md](docs/skills/README.md) first** when investigating issues or working in unfamiliar areas.

**Cascade check on doc changes:** After renaming, moving, or deleting any doc, grep the entire repo for references to the old filename/path and fix all broken links. This includes `CLAUDE.md`, `README.md`, doc index tables, and inline references in other docs. Do this automatically — don't wait to be asked.

## Market Data Source Priority

**Schwab is the PRIMARY market data source. yfinance is ONLY a fallback.**

When writing ANY code that fetches stock prices, candles, quotes, or market data:
1. Use `SchwabMarketClient` (`trader/market/schwab_client.py`) or `MarketDataService` (`trader/market/data_service.py`) as the default
2. yfinance is ONLY a fallback when Schwab is unavailable or fails for a specific symbol
3. This applies to scripts, tools, analysis code — everything
4. See `docs/src/SCHWABDEV.md` for full API documentation
5. Key env vars: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_DISABLED` (optional)

## Operator Notifications

**Use `notify()` to alert the operator about errors that shouldn't be missed, or to verify that a code fix works in production.**

```python
from trader.notifications import notify
notify(subject="Short title", body="Details about what happened...")
```

- Appends a timestamped entry to `logs/notifications.md` (always active, no config needed)
- Also sends email if `NOTIFY_EMAIL_TO` + `NOTIFY_SMTP_PASSWORD` env vars are set
- See `trader/notifications.py` for full API

**When to use:**
1. **Buy/sell failures** — especially when a replacement sell succeeded but the replacement buy didn't
2. **Safety checks for new fixes** — when a fix can't be tested until a rare code path triggers again, add a `notify()` call so the operator knows whether it worked or needs attention
3. **Any error the operator shouldn't have to discover by tailing logs**

**Pattern for "verify fix works" notifications:**
```python
try:
    # new fix
    ...
except Exception as exc:
    notify(
        subject=f"Fix did not work: {short_description}",
        body=f"The fallback for X failed.\nError: {exc}\n\nContext: ..."
    )
    raise  # or return False, depending on flow
```

## Debugging Issues

**When debugging, read `docs/skills/DIAGNOSTICS.md` first** — it lists all data sources ranked by usefulness and explains common diagnostic workflows.

Key data sources (in priority order):
1. **`alpaca_transactions` table** — Complete order ledger (buys, sells, fills, failures, reconciliation). Query via SQL. See `docs/skills/DIAGNOSTICS.md`.
2. **`logs/trader.log`** — INFO+ (all operations, errors, tracebacks). Rotates daily, keeps 14 days.
3. **`logs/tick_collector.log`** — INFO+ (stream events, inserts, errors). Rotates daily, keeps 14 days.
4. **Console output** — INFO+ (same content as log files, but not persisted across restarts).

All errors, warnings, and tracebacks go through the logging module — log files are the **complete record**. No `print()` or `traceback.print_exc()` for error reporting. See `docs/skills/LOGGING.md` for full details (logger names, grep recipes, gotchas).

```bash
tail -200 logs/trader.log            # recent trader activity
tail -200 logs/tick_collector.log    # recent tick collector activity
```

Trader log env vars: `LOG_FILE` (default: `logs/trader.log`), `LOG_LEVEL` (default: `INFO`), `LOG_KEEP_DAYS` (default: `14`). See `trader/logging_config.py`.

Tick collector log env vars (prefixed `TC_`): `TC_LOG_FILE` (default: `logs/tick_collector.log`), `TC_LOG_LEVEL` (default: `INFO`), `TC_LOG_KEEP_DAYS` (default: `14`). See `tick_collector/__main__.py`.

## Committing and Pushing

**Commit and push at natural breakpoints** — when a logical group of changes is complete and tests pass. Don't wait until the end of a long session; commit as you go in distinct, meaningful units.

- **When to commit**: After completing a coherent set of changes (bug fix, feature, refactor). Each commit should be a self-contained unit that makes sense on its own.
- **Commit messages**: Detailed — list files changed and what each change does (see recent git log for style).
- **Always push** after committing — don't leave commits local.
- **"push" command**: If the user says just "push", commit and push all pending changes with a well-crafted message. This is a reminder that you may have forgotten.
- **Don't auto-commit** after every small edit — wait for a natural breakpoint.

# Behavioral guidelines (beyond built-in defaults)

- **State assumptions explicitly.** If multiple interpretations exist, present them — don't pick silently. Push back when a simpler approach exists.
- **Orphan cleanup rule:** Remove imports/variables/functions that YOUR changes made unused. Don't remove pre-existing dead code unless asked.
- **Goal-driven execution:** Transform tasks into verifiable goals before starting. For multi-step tasks, state a brief plan with verification checks. "Fix the bug" → "Write a test that reproduces it, then make it pass."