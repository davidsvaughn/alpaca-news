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
2. **`logs/trader.log`** — WARNING+ only (errors, tracebacks, timeouts). **Does NOT show successful operations** (those log at INFO level). Do not conclude "everything failed" from this log alone.
3. **Console output** — INFO+ but not persisted across restarts.

```bash
cat logs/trader.log        # view recent errors (WARNING+ only!)
tail -f logs/trader.log    # tail live
```

Configured via env vars: `LOG_FILE` (default: `logs/trader.log`), `LOG_LEVEL` (default: `WARNING`), `LOG_KEEP_DAYS` (default: `14`). Rotates daily at midnight. See `trader/logging_config.py`.

## Documentation Lookup

When working with external libraries, frameworks, or APIs — **always use the Context7 MCP server** (`resolve-library-id` then `query-docs`) to look up current documentation and code examples before writing code or giving advice. Do not rely on training data for API signatures, parameter names, or usage patterns — they may be outdated or wrong.

Specifically:
- **Before using any library API** you haven't already verified in this session, look it up via Context7.
- **When the user asks about a framework** (features, limitations, how something works), query Context7 for authoritative answers rather than guessing.
- **When writing code that uses an external library**, verify the import paths, method signatures, and parameter names against Context7 docs.
- If Context7 doesn't have the library, fall back to `WebSearch` or `WebFetch` — but prefer Context7 first since it returns structured, verified documentation.

# Behavioral guidelines to reduce common LLM coding mistakes

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.