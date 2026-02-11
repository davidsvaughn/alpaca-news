# Design Plan v2: LLM-Based Day-Trading Research Assistant

> **Consolidated design document** — supersedes `DESIGN_PLAN.md` (original) and
> `DESIGN_REVISION.md` (revisions). This is the single source of truth for system
> architecture, implementation status, and roadmap.
>
> Last updated: 2026-02-11

---

## Executive Summary

This system is a **news-triggered, multi-LLM research pipeline** that identifies
tradeable stock signals from financial news, manages hypothetical positions through
their full lifecycle, and learns from outcomes.

**Core loop:**
1. **Online:** news arrives → triage → explore (free-form tool use) → seal Snapshot
2. **Watch:** high-confidence signals → monitored position → exit → retrospective
3. **Offline:** label outcomes → score traces → reflect → update knowledge

**Key design principles:**
- **Free-form tool use** — the LLM decides what tools to call and when to stop;
  learned knowledge is injected via prompts, not enforced via rigid code structures.
- **Atomic learning artifacts** — every action produces an immutable Snapshot with
  full tool traces, enabling offline learning from complete trade lifecycles.
- **Budget guardrails in code** — cost limits, rate limits, and safety constraints
  are enforced programmatically; the LLM operates freely within those bounds.

---

## Implementation Status Summary

| Component | Status | Notes |
|-----------|--------|-------|
| **Triage / pre-filter** | DONE | Keyword pre-filter + LLM triage |
| **Explorer (Phase 1/2)** | DONE | Rigid two-phase — to be replaced by free-form |
| **Snapshot / SnapshotBuilder** | DONE | Immutable seal pattern |
| **ToolTrace** | DONE | Per-hop recording |
| **Cost tracker** | DONE | Per-tool, per-item, daily budgets |
| **LLM client (OpenAI/Gemini/Grok)** | DONE | Unified multi-provider interface |
| **Evidence acquirer** | DONE | URL extraction, trafilatura |
| **X API stream** | DONE | Filtered stream, burst mode, rules |
| **Database (SQLite)** | DONE | Snapshots table, idempotent inserts |
| **Dashboard (FastAPI + SSE)** | DONE | Live feed, event bus |
| **Schwab market data** | PARTIAL | Quotes, candles, streaming done; Tier 1 expansion TODO |
| **Knowledge store** | PARTIAL | skip_patterns, reliable_sources exist; insights.json TODO |
| **Action menu / weights** | DONE | 12 finite actions — will be deprioritized (see Explorer revision) |
| **Explorer (free-form tool use)** | TODO | Replaces rigid Phase 1/2 |
| **yfinance data layer** | TODO | New — free data for fundamentals, insider tx, technicals |
| **BM25 situation memory** | TODO | New — learned from TradingAgents |
| **Schwab Tier 1 expansion** | TODO | Options IV, fundamentals, movers, enhanced market context |
| **insights.json** | TODO | Flat scored insights for prompt injection |
| **Watch lifecycle** | TODO | Entry → hold → exit → retrospective → sealed |
| **Signal extraction step** | TODO | New — distill verbose output to clean signal |
| **Offline loop** | TODO | Labeling, hop scoring, reflection |
| **Bull/bear prompt pattern** | TODO | New — lightweight adversarial reasoning |
| **Data vendor fallback** | TODO | New — Schwab → yfinance fallback |

---

## 1. Architecture Overview

```
                         NEWS EVENT (Alpaca JSON)
                                │
                    ┌───────────▼────────────┐
                    │   TRIAGE (Stage 1)      │  ← DONE
                    │   pre-filter + LLM      │
                    └───────────┬────────────┘
                                │ investigate
                    ┌───────────▼────────────┐
                    │   EXPLORER (Stage 2)    │  ← TODO: convert to free-form
                    │   free-form tool use    │
                    │   + budget guardrails   │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   SIGNAL EXTRACTION     │  ← TODO
                    │   verbose → structured  │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   SEALED SNAPSHOT       │  ← DONE
                    │   (immutable artifact)  │
                    └──────┬────────┬────────┘
                           │        │
              ┌────────────▼─┐   ┌──▼──────────────┐
              │  WATCH?       │   │  DASHBOARD       │  ← DONE
              │  confidence   │   │  (FastAPI + SSE)  │
              │  >= threshold │   └──────────────────┘
              └──────┬───────┘
                     │ yes                              ← TODO (all below)
          ┌──────────▼──────────┐
          │  WATCH LIFECYCLE     │
          │  hold → exit →       │
          │  retrospective →     │
          │  sealed Watch        │
          └──────────┬──────────┘
                     │
          ┌──────────▼──────────┐
          │  OFFLINE LOOP        │
          │  label → score →     │
          │  reflect → update    │
          │  insights.json       │
          └─────────────────────┘
```

---

## 2. Online Loop: Triage

**Status: DONE** — `trader/online/triage.py`

### Pre-filter (Stage 0) — no LLM cost

Cheap local keyword matching before any LLM call:
- Learned skip keywords from `data/knowledge/skip_patterns.json`
- Built-in fluff patterns ("if you had invested", "dividend aristocrat", etc.)
- Known auto-generated content authors (e.g. "Benzinga Insights")

### LLM triage (Stage 1)

If not pre-filtered, send to a fast/cheap LLM:
- Classifies as `investigate` or `skip` with confidence and reasoning
- Optionally extracts new skip patterns to add to the knowledge store
- Returns refined symbol list

**Model:** fast & cheap (e.g. `grok-4-1-fast-reasoning`, `gpt-5-mini`)

---

## 3. Online Loop: Explorer

### Current state: DONE (rigid two-phase)

The existing explorer (`trader/online/explorer.py`, 641 lines) uses:
- **Phase 1:** pick from 8 predefined action templates → run → generate hypotheses
- **Phase 2:** LLM ranks hypotheses → pick follow-up templates → run → stop

This works but constrains the LLM to a finite action menu, preventing creative
reasoning about novel situations.

### Target state: TODO (free-form tool use)

Replace rigid Phase 1/2 with a **free-form tool-use reasoning loop**:
- LLM receives tool definitions and a clear objective
- LLM decides what tools to call, in what order, when to stop
- Each tool call recorded as a ToolTrace (existing infrastructure)
- Budget guardrails enforced after each call
- Learned insights injected via prompt context

**Why:** Frontier LLMs improve faster than bandit learning can converge on our
limited data. Prompt-injected knowledge compounds with model upgrades.

### Tools available to the explorer

#### Web / social research tools

| Tool | Provider | Cost |
|------|----------|------|
| `web_search(query)` | OpenAI / Gemini / Grok | Per-call LLM + tool fee |
| `x_search(query)` | Grok | Per-call LLM + tool fee |
| `x_stream(keywords, minutes)` | X API | X API credits |
| `url_fetch(url)` | Local (trafilatura) | Free |

#### Financial data tools — Schwab (free, no LLM cost)

| Tool | Status | What it reveals |
|------|--------|-----------------|
| `check_price(symbol)` | DONE | Real-time quote + recent 1-min candles |
| `check_market_context()` | PARTIAL | SPY, VIX, session. TODO: /ES futures, market_hours() |
| `check_options_activity(symbol)` | TODO | ATM IV, put/call ratio, unusual activity |
| `get_fundamentals(symbol)` | TODO | Market cap, P/E, EPS, sector, 52-week range |
| `get_movers(index)` | TODO | Top gainers/losers by % change or volume |
| `get_price_history(symbol, period, freq)` | TODO | Historical candles (any granularity) |
| `get_options_chain(symbol, ...)` | TODO | Full chain with Greeks |
| `check_order_book(symbol)` | TODO | Level 2 bid/ask depth |

#### Financial data tools — yfinance (NEW — free, no API key)

| Tool | Status | What it reveals |
|------|--------|-----------------|
| `check_insider_activity(symbol)` | TODO | Recent insider buys/sells — high-signal confirmation |
| `get_fundamentals_yf(symbol)` | TODO | P/E, market cap, debt ratios — free fallback for Schwab |
| `get_price_history_yf(symbol, period)` | TODO | Historical OHLCV — free, no API key needed |
| `get_company_news_yf(symbol)` | TODO | Recent news articles per ticker |

#### Technical indicators — stockstats (NEW — computed locally, free)

| Tool | Status | What it reveals |
|------|--------|-----------------|
| `get_technical_indicators(symbol, indicators)` | TODO | RSI, MACD, Bollinger Bands, ATR, VWMA, MFI |

Computed from yfinance OHLCV data. The LLM chooses which indicators are relevant
for the current situation (no need to compute all of them every time).

### Explorer prompt sketch

```markdown
You are a financial research assistant investigating a breaking news event.

## Tools available
{tool definitions — web/social + financial data + evidence}

## Lessons from experience
{top-N scored insights from insights.json, filtered to score > 0}

## Similar past situations
{top-K BM25-matched past situations with lessons learned}

## Budget
- Remaining cost: ${budget_remaining}
- Remaining tool calls: {remaining_calls}
- Financial data tools and local indicators are free.

## The news event
Headline: {headline}
Summary: {summary}
Symbols: {symbols}
Source: {source}
Timestamp: {timestamp}

## Your task
Investigate this news event to determine:
1. Is this a real, tradeable signal or noise/recycled content?
2. What is the likely short-term price impact (direction, magnitude, timing)?
3. What is your confidence level?

Think step by step. Use tools as needed. Stop when you have enough evidence.

Before making your final assessment, consider both sides:
- What is the bull case? What evidence supports a price move?
- What is the bear case? What could go wrong or be already priced in?
Then weigh these against each other to reach your conclusion.
```

The "consider both sides" instruction is a lightweight version of adversarial
debate (inspired by TradingAgents' Bull/Bear researcher pattern) — it forces the
LLM to consider counter-arguments without the cost of a multi-agent system.

### Signal extraction step (NEW)

After the explorer produces verbose output, run a cheap fast-model call to extract
a clean, structured trading signal:

```json
{
  "direction": "up | down | none",
  "confidence": 0.82,
  "horizon": "15m | 60m | 1d",
  "magnitude_estimate": "0.5-1.5%",
  "key_catalyst": "Supply constraint confirmed by Reuters + Bloomberg",
  "bull_case": "...",
  "bear_case": "...",
  "risk_factors": ["...", "..."]
}
```

This separates the rich reasoning (stored in ToolTraces) from the actionable
signal (used by the Watch system to decide whether to enter a position).

---

## 4. Data Sources & Tools

### 4a. LLM Providers — DONE

`trader/llm/client.py` — unified interface, three providers:

| Provider | Capabilities | Unique strength |
|----------|-------------|-----------------|
| OpenAI | web_search, Responses API | Strongest reasoning |
| Gemini | GoogleSearch grounding | Google search quality |
| Grok | web_search + x_search | X/Twitter data access |

Per-stage model configuration via `.env`:
```env
TRIAGE_PROVIDER=grok        TRIAGE_MODEL=grok-4-1-fast-reasoning
RESEARCH_PROVIDER=openai    RESEARCH_MODEL=gpt-5-mini
XSEARCH_PROVIDER=grok      XSEARCH_MODEL=grok-4-1-fast-reasoning
SENTIMENT_PROVIDER=gemini   SENTIMENT_MODEL=gemini-3-flash-preview
```

### 4b. Schwab Market Data — PARTIAL

`trader/market/schwab_client.py` (505 lines)

**Done:**
- Real-time quotes (`get_quote()`, `get_quotes()`)
- Intraday candles (`get_intraday_candles()` — 1-min default)
- Level 1 streaming (`start_stream()`, `get_stream_snapshot()`)
- Basic market context (SPY, VIX, session estimate)

**TODO — Tier 1 (high value, build first):**
- `check_options_activity(symbol)` — wraps `option_chains()`, extracts ATM IV +
  put/call ratio. **Probably the single most informative signal we're missing.**
  The options market often "knows" before the stock moves.
- `get_fundamentals(symbol)` — wraps `instruments(symbol, projection="fundamental")`.
  One API call gives market cap, P/E, EPS, sector, 52-week range.
- `get_movers(index)` — wraps `movers()` for $DJI/$SPX/NASDAQ. Reveals whether
  a move is stock-specific or sector-wide.
- Enhanced `check_market_context()` — add `market_hours()` (proper session detection)
  and /ES futures quote (more liquid than SPY after hours).

**TODO — Tier 2 (build on demand):**
- `get_options_chain(symbol, ...)` — full chain for detailed analysis
- `get_price_history(symbol, period, frequency)` — flexible candle periods
- `check_order_book(symbol)` — L2 depth

### 4c. yfinance Data Layer — NEW, TODO

**Motivation:** Completely free, no API key required. Provides data that
complements Schwab and serves as a fallback. Inspired by TradingAgents, which
uses yfinance as its primary data source.

**Planned tools:**

| Tool | Data | Value |
|------|------|-------|
| `check_insider_activity(symbol)` | Recent insider buys/sells (executives, board) | High-signal confirmation. Insider buying before positive news = strong signal. Insider selling before positive news = red flag. |
| `get_fundamentals_yf(symbol)` | P/E, market cap, debt ratios, sector | Quick sanity check: avoid micro-caps, over-leveraged companies, absurd valuations |
| `get_price_history_yf(symbol, period)` | Historical OHLCV (up to 15 years) | Free alternative for backtesting/labeling; doesn't burn Schwab quota |
| `get_company_news_yf(symbol)` | Up to 20 recent articles per ticker | Free news feed — complements web_search |

**Implementation:** New module `trader/market/yfinance_client.py`, wrapping the
`yfinance` library. Dependencies: `yfinance`, `stockstats`.

### 4d. Technical Indicators — NEW, TODO

Computed locally from yfinance OHLCV data using `stockstats`. Zero cost.

**Available indicators:**
- Trend: SMA-50, SMA-200, EMA-10
- Momentum: MACD, MACD Signal, MACD Histogram, RSI
- Volatility: Bollinger Bands (middle/upper/lower), ATR
- Volume: VWMA, MFI (Money Flow Index)

**Usage pattern:** The LLM requests specific indicators relevant to the current
situation. For example, if investigating a price spike, it might check RSI
(overbought?) and Bollinger Bands (breakout or mean-reversion?).

**Implementation:** `trader/market/indicators.py`, using `stockstats` to compute
indicators from a yfinance-fetched DataFrame.

### 4e. Data Vendor Fallback — NEW, TODO

Apply the vendor-abstraction pattern (inspired by TradingAgents' interface layer):
- If Schwab is unavailable (rate limit, auth expired, disabled), automatically
  fall back to yfinance for the same data
- Category-level configuration: set default vendor per data type
- Graceful degradation: the system continues to function (with less data quality)
  even when Schwab is down

**Implementation:** Thin routing layer in the market module that tries Schwab
first, falls back to yfinance on failure. Similar to the existing
`SCHWAB_DISABLED=true` graceful degradation, but automatic.

### 4f. Evidence Acquisition — DONE

`trader/evidence/` — URL extraction → fetch → extract → persist

- Extracts URLs from tool traces
- Fetches and extracts article text via `trafilatura` (primary) or `newspaper3k` (fallback)
- Persists immutable evidence docs to `data/evidence/*.json`

### 4g. X API Stream — DONE

`trader/xapi/` + `trader/online/x_stream_service.py`

- Filtered stream with rules management
- Conservative BURST-first mode (run N minutes, collect posts, stop)
- Daily/burst guardrails (max posts/day, max bursts/day)
- In-memory cache (deque per symbol/tag) for explorer to query

---

## 5. Knowledge & Learning

### 5a. Knowledge Store — PARTIAL

`trader/knowledge/store.py` — manages JSON files under `data/knowledge/`:

| File | Status | Purpose |
|------|--------|---------|
| `skip_patterns.json` | DONE | Auto-skip keywords/sources for pre-filter |
| `reliable_sources.json` | DONE (empty) | Source domain tracking |
| `search_strategies.json` | DONE (empty) | Learnable action weights (deprioritized) |
| `signal_patterns.json` | DONE (empty) | Future learning |
| `anti_patterns.json` | DONE (empty) | Future learning |
| `insights.json` | TODO | Flat scored insights — primary knowledge artifact |

### 5b. Insights System — TODO

`data/knowledge/insights.json` — a flat list of scored insight objects.

```json
{
  "id": "ins_001",
  "text": "When initial web_search finds conflicting sources about a supply chain rumor, url_fetch on each source followed by comparison yields much more reliable evidence than a second web_search",
  "score": 3,
  "created": "2026-02-15",
  "last_touched": "2026-02-20",
  "source_snapshots": ["snap_142", "snap_167"]
}
```

**Scoring:** starts at 1. Offline reflection bumps +1 (reinforced) or -1
(contradicted). Insights with score <= 0 stop being injected into prompts but
aren't deleted (they can recover).

**Prompt injection:** Load all insights → filter score > 0 → sort by score desc
→ cap at top N → inject as "Lessons from experience" block.

**Why flat, not categorized:** Real insights blend domain knowledge, tool strategy,
and source reliability. Forcing premature categories loses connections. If natural
clusters emerge after hundreds of snapshots, the reflection loop can itself propose
how to reorganize.

### 5c. BM25 Situation Memory — NEW, TODO

**Inspired by TradingAgents'** `memory.py` — uses `rank-bm25` (pure Python, no
API calls, no embeddings) to match current situations against past ones.

**How it works:**
1. After a Watch lifecycle completes (or a Snapshot is labeled), store a
   `(situation_text, lesson_learned)` tuple
2. On new events, tokenize the current situation and retrieve the top-K most
   similar past situations via BM25 lexical similarity
3. Inject those lessons into the Explorer prompt as "Similar past situations"

**Why this complements insights.json:**
- `insights.json` = **general principles** distilled from many events
  ("rumor-type news benefits from url_fetch")
- BM25 memory = **specific analogies** ("last time NVDA had a supply rumor,
  here's what happened and what we learned")

**Implementation:** `trader/knowledge/memory.py`
- Depends on: `rank-bm25`
- Storage: `data/knowledge/memories.jsonl` (append-only)
- Index rebuilt on startup from the JSONL file
- Retrieval: `get_similar_situations(current_text, n=3) → list[SituationMemory]`

**What gets stored as a "situation":**
- Concatenation of: headline, summary, market context, key evidence found
- Lesson: what the outcome was, what worked/didn't, key takeaway

This is cheap (no API calls), lightweight, and provides contextual learning
that improves with every trade lifecycle.

---

## 6. Snapshot & ToolTrace

**Status: DONE** — `trader/models/snapshot.py`, `trader/models/tool_trace.py`

### Snapshot — immutable learning artifact

```json
{
  "snapshot_id": "uuid",
  "version": "v1",
  "created_at": "2026-02-09T14:32:11Z",
  "trigger": { "type": "alpaca_news", "headline": "...", "symbols": [...] },
  "market_context": { "session": "market_open", "spy_return_15m": -0.12, "vix_level": 19.4 },
  "price_context": { "per_symbol": { "NVDA": { "last_price": 612.30, "recent_candles_1m": [...] } } },
  "exploration_budget": { "max_hops": 3, "max_cost_usd": 0.35 },
  "tool_traces": [...],
  "prediction": { "direction": "up", "confidence": 0.71, "horizon": "60m" },
  "cost_summary": { "total_usd": 0.21, "by_tool": { "web_search": 0.12, "x_search": 0.09 } }
}
```

**Builder pattern:** `SnapshotBuilder` accumulates data → `.seal()` → frozen `Snapshot`.

### ToolTrace — one per hop

Records: state → action → observation → stop decision, with execution metadata
and extracted signals. Supports parent trace linking for hierarchical reasoning.

---

## 7. Watch Lifecycle (Position Management)

**Status: TODO** — design complete, no code exists

### Overview

When the entry explorer produces a signal meeting a configurable confidence
threshold, the system creates a **Watch** — a monitored hypothetical position
that progresses through phases, each generating Snapshots for learning.

```
Entry Snapshot (confidence >= threshold)
    │ creates Watch
    ▼
Monitoring Snapshots (periodic check-ins during hold)
    │ exit decision
    ▼
Exit Snapshot (exit reason + realized P&L)
    │ post-exit monitoring
    ▼
Retrospective Snapshots (counterfactual analysis)
    │ seal lifecycle
    ▼
Sealed Watch (complete trade lifecycle for reflection)
```

### Watch schema

```json
{
  "watch_id": "watch_001",
  "symbol": "NVDA",
  "status": "holding | exited | retrospective | closed",
  "entry": {
    "snapshot_id": "snap_142",
    "price": 612.30,
    "time": "2026-02-15T14:32:00Z",
    "confidence": 0.82,
    "direction": "up",
    "thesis": "Supply constraint rumor confirmed by Reuters + Bloomberg"
  },
  "exit": {
    "snapshot_id": "snap_148",
    "price": 618.50,
    "time": "2026-02-15T14:54:00Z",
    "reason": "Momentum exhausting, IV collapsing",
    "realized_pnl_pct": 1.01
  },
  "monitoring_snapshot_ids": ["snap_143", "snap_144", "snap_145"],
  "retrospective_snapshot_ids": ["snap_149", "snap_150"],
  "config": { "...schedules and budgets..." },
  "lifecycle_sealed_at": "2026-02-15T15:55:00Z"
}
```

### Check-in depth levels

| Depth | What it does | Cost |
|-------|-------------|------|
| `lightweight` | Price check only (Schwab quote + candles). No LLM. | Free |
| `medium` | Price + options IV + quick news scan. May use LLM. | ~$0.02-0.05 |
| `full` | Full tool-use loop. All tools available. | ~$0.05-0.15 |
| `force_exit` | Must produce exit decision. Cannot choose "hold." | ~$0.05-0.15 |
| `seal` | Seal the retrospective. Final assessment. | ~$0.02-0.05 |

### Holding check-in schedule (defaults, configurable)

| Window | Interval | Depth |
|--------|----------|-------|
| 0-10 min | 2 min | lightweight |
| 10-30 min | 5 min | medium |
| 30-60 min | 10 min | medium |
| 60-240 min | 15 min | full |
| 240+ min | — | force_exit |

### Why post-exit monitoring matters

Most systems stop after exit. But the richest learning comes from counterfactuals:
- "I sold at +1.2% but the stock continued to +3.5% — what signals said hold?"
- "I took a -0.8% loss, but it recovered in 10 more minutes — was my stop too tight?"

Retrospective Snapshots feed the reflection loop with exit-specific insights.

### Budget controls

| Control | Default | Env var |
|---------|---------|---------|
| Monitoring budget per Watch | $0.50 | `WATCH_MONITORING_BUDGET` |
| Retrospective budget per Watch | $0.20 | `WATCH_RETROSPECTIVE_BUDGET` |
| Max concurrent Watches | 5 | `MAX_CONCURRENT_WATCHES` |
| Max daily Watch cost | $5.00 | `MAX_DAILY_WATCH_COST` |
| Entry confidence threshold | 0.7 | `WATCH_CONFIDENCE_THRESHOLD` |
| Max hold duration | 240 min | `WATCH_MAX_HOLD_MINUTES` |
| Max retrospective duration | 60 min | `WATCH_MAX_RETRO_MINUTES` |

### Files to create

- `trader/models/watch.py` — Watch data model
- `trader/online/watcher.py` — Watch lifecycle manager
- `trader/prompts/monitor_hold.md` — holding check-in prompt
- `trader/prompts/monitor_exit.md` — exit evaluation prompt
- `trader/prompts/monitor_retrospective.md` — post-exit retrospective prompt

---

## 8. Offline Loop

**Status: TODO** — design complete, no code exists

### 8a. Outcome labeling

For each Snapshot, compute labels from market data:
- Direction/return at horizons: +15m, +60m, +1d
- Optional magnitude buckets (>0.5%, >1%)
- Optional MFE/MAE (max favorable/adverse excursion)

Uses Schwab historical candles (or yfinance as free fallback for labeling).

### 8b. Hop scoring

Per-trace evaluation:
- Did this hop add novel information?
- Did the prediction improve after it?
- What did it cost? Was it worth it?
- Was url_fetch better than the LLM search summary?

Per-sequence evaluation:
- Which chains of tools produced good outcomes?
- Which sequences were redundant/wasteful?

### 8c. Reflection

LLM reviews batches of scored Snapshots AND sealed Watch lifecycles:
- Reinforce existing insights (+1 score)
- Weaken contradicted insights (-1 score)
- Propose new insights (score=1)
- Update BM25 memory with new (situation, lesson) tuples
- Optionally update operational files (skip_patterns, etc.)

Human review before merging changes into the knowledge store.

### 8d. Iterative learning pipeline

```
Online: event → Snapshot (with injected insights + memories)
    │
    ▼
Offline: label outcomes → score traces
    │
    ▼
Offline: reflection → update insights.json + memories.jsonl
    │
    ▼
(next event uses updated knowledge)
```

---

## 9. Infrastructure

### 9a. Database — DONE

`trader/db/database.py` — SQLite with `snapshots` table, idempotent inserts.

Future consideration: Postgres JSONB for concurrent writes, queryable JSON,
and `pgvector` for similarity search. Not needed for v1.

### 9b. Dashboard — DONE

`trader/web/` — FastAPI + SSE

- Live feed (real-time pipeline events)
- Event bus publishes: news_received, triage_decision, snapshot_sealed, etc.

Future pages (when needed): trade log, cost monitor, knowledge viewer, config panel.

### 9c. Cost Control — DONE

`trader/llm/cost_tracker.py`

- Per-call cost estimation using pricing tables
- Per-tool breakdown (web_search, x_search, GoogleSearch, market, etc.)
- Daily + per-item budget enforcement
- Integrated cost_usd in every ToolTrace

```env
MAX_DAILY_COST=5.00
MAX_COST_PER_NEWS_ITEM=0.50
MAX_TOTAL_HOPS=3
```

### 9d. Backfill — DONE

`trader/online/backfill.py` — batch driver for processing existing news files.
Safe to re-run (deterministic IDs, idempotent inserts). Useful for bootstrapping
datasets and regression testing after prompt changes.

---

## 10. Project Structure

```
trader/
├── __init__.py
├── main.py                         # Entry point
├── config.py                       # Settings (env vars, defaults)
├── online/
│   ├── orchestrator.py             # Watch loop, queue-decoupled processing  [DONE]
│   ├── triage.py                   # Pre-filter + LLM triage               [DONE]
│   ├── explorer.py                 # Two-phase → free-form (TODO: convert)  [DONE/TODO]
│   ├── watcher.py                  # Watch lifecycle manager                [TODO]
│   ├── backfill.py                 # Batch reprocessing                     [DONE]
│   └── x_stream_service.py         # X stream burst service                [DONE]
├── models/
│   ├── snapshot.py                 # Snapshot + SnapshotBuilder             [DONE]
│   ├── tool_trace.py               # ToolTrace per-hop recording           [DONE]
│   ├── actions.py                  # Finite action menu (deprioritized)     [DONE]
│   └── watch.py                    # Watch data model                       [TODO]
├── llm/
│   ├── client.py                   # Unified LLM client (OpenAI/Gemini/Grok) [DONE]
│   ├── cost_tracker.py             # Budget enforcement                     [DONE]
│   └── extract.py                  # Robust JSON extraction from LLM output [DONE]
├── market/
│   ├── schwab_client.py            # Schwab wrapper (quotes, candles, stream) [PARTIAL]
│   ├── yfinance_client.py          # yfinance wrapper (free data)            [TODO]
│   └── indicators.py               # Technical indicators via stockstats     [TODO]
├── evidence/
│   ├── acquirer.py                 # URL extraction + fetch + persist       [DONE]
│   ├── extract.py                  # trafilatura / newspaper3k             [DONE]
│   └── fetch.py                    # HTTP fetch                             [DONE]
├── knowledge/
│   ├── store.py                    # JSON knowledge file management         [PARTIAL]
│   └── memory.py                   # BM25 situation memory                  [TODO]
├── xapi/
│   ├── client.py                   # X API v2 client                        [DONE]
│   ├── rules.py                    # Stream rules management                [DONE]
│   └── stream.py                   # Filtered stream consumer               [DONE]
├── db/
│   └── database.py                 # SQLite persistence                     [DONE]
├── web/
│   ├── app.py                      # FastAPI app                            [DONE]
│   ├── sse.py                      # Server-sent events                     [DONE]
│   └── templates/
│       └── feed.html               # Dashboard template                     [DONE]
├── prompts/
│   ├── explore_phase1.md           # Phase 1 prompt (to be replaced)        [DONE]
│   ├── explore_phase2.md           # Phase 2 prompt (to be replaced)        [DONE]
│   ├── hypothesis_rank.md          # Hypothesis ranking                     [DONE]
│   ├── explore_freeform.md         # Free-form explorer prompt              [TODO]
│   ├── monitor_hold.md             # Holding check-in prompt                [TODO]
│   ├── monitor_exit.md             # Exit evaluation prompt                 [TODO]
│   └── monitor_retrospective.md    # Post-exit retrospective                [TODO]
├── offline/                        # Offline learning loop                  [TODO]
│   ├── labeler.py                  # Attach outcomes to Snapshots
│   ├── scorer.py                   # Score hop/sequence value
│   ├── reflector.py                # Generate insight updates
│   └── validator.py                # Validate before activation
└── data/                           # Runtime data (gitignored)
    ├── snapshots/                  # Sealed Snapshot JSON files
    ├── evidence/                   # Extracted article text
    └── knowledge/
        ├── skip_patterns.json      # [DONE]
        ├── reliable_sources.json   # [DONE, empty]
        ├── search_strategies.json  # [DONE, empty]
        ├── insights.json           # [TODO]
        └── memories.jsonl          # [TODO] BM25 situation memory
```

---

## 11. Implementation Roadmap

### Phase A: Data layer expansion (yfinance + Schwab Tier 1)

Add free data sources and expand Schwab coverage. These are prerequisites for
the explorer revision (the LLM needs tools to call).

1. `trader/market/yfinance_client.py` — insider activity, fundamentals, price history, news
2. `trader/market/indicators.py` — RSI, MACD, Bollinger Bands, etc. via stockstats
3. Schwab Tier 1: `check_options_activity()`, `get_fundamentals()`, `get_movers()`,
   enhanced `check_market_context()`
4. Vendor fallback: try Schwab → fall back to yfinance

### Phase B: Explorer revision (free-form + knowledge injection)

Convert the explorer from rigid Phase 1/2 to free-form tool use.

1. Create `insights.json` (start empty or seed with a few common-sense insights)
2. Implement BM25 situation memory (`trader/knowledge/memory.py`)
3. Write `explore_freeform.md` prompt (with bull/bear pattern, insight injection,
   memory injection)
4. Refactor `explorer.py` to use free-form tool-use loop with budget guardrails
5. Add signal extraction step (cheap LLM call to distill structured signal)

### Phase C: Watch lifecycle

Full position management from entry to retrospective.

1. `trader/models/watch.py` — Watch data model
2. `trader/online/watcher.py` — lifecycle manager
3. Monitoring prompts: `monitor_hold.md`, `monitor_exit.md`, `monitor_retrospective.md`
4. Integration with orchestrator (create Watch when confidence >= threshold)
5. Watch persistence (extend SQLite schema or separate watches table)

### Phase D: Offline loop

Close the learning feedback loop.

1. Outcome labeler (attach +15m/+60m/+1d returns to Snapshots)
2. Hop scorer (per-trace and per-sequence value estimation)
3. Reflection prompt (review scored Snapshots + sealed Watches)
4. Insight update pipeline (reinforce/weaken/create insights)
5. Memory update (store new situation/lesson tuples)

---

## 12. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| LLM orchestration | Direct API calls, not Agents SDK | Multi-provider flexibility, explicit cost control, auditable pipeline |
| Database | SQLite (v1), Postgres later | Speed of development; upgrade when concurrency demands it |
| Explorer approach | Free-form tool use | LLM intelligence improves faster than bandit learning on limited data |
| Knowledge model | Flat insights + BM25 memory | Avoids premature categorization; contextual + general knowledge |
| Position management | Watch lifecycle with phases | Captures full trade trajectory for learning, not just entry |
| Data redundancy | Schwab primary, yfinance fallback | Schwab has best real-time data; yfinance provides free resilience |
| Technical indicators | Computed locally (stockstats) | Zero cost; LLM selects which are relevant per situation |
| Trading mode | Paper-first | Build confidence before live execution |

---

## 13. Open Questions

1. **Tool-use API mechanics:** Use OpenAI Responses API with function definitions,
   or implement a custom tool-call loop (LLM outputs structured request → we
   execute → feed result back)? Responses API is cleaner but ties the explorer
   to OpenAI/Grok-compatible APIs.

2. **X stream in explorer:** X stream is inherently async (runs N minutes). Should
   the LLM start a stream and continue investigating (parallel), or does it block?

3. **Triage + explorer boundary:** Does triage stay as a separate Stage 1 (cheap
   filter before expensive exploration), or merge into the free-form loop?

4. **Multi-provider tool routing:** When the LLM wants `web_search`, should we
   always use the same provider, or let a heuristic choose between OpenAI/Gemini/Grok?

---

## Appendix: Ideas Evaluated and Deferred (v2+)

These ideas were evaluated (sourced from TradingAgents and design discussions)
but deferred to keep v1 focused:

| Idea | Source | Why deferred |
|------|--------|-------------|
| Full adversarial debate (multi-agent bull/bear) | TradingAgents | High cost (multiple LLM calls); lightweight prompt pattern captures 80% of value |
| Three-way risk debate (aggressive/conservative/neutral) | TradingAgents | Complex orchestration; simple risk-check prompt is sufficient for v1 |
| Alpha Vantage NEWS_SENTIMENT | TradingAgents | Limited free tier (25 calls/day); yfinance news + web_search cover this |
| LLM-selected indicator subsets | TradingAgents | Interesting optimization but premature; start by making all indicators available |
| Postgres / Supabase | Original design | SQLite sufficient for single-process v1; upgrade path clear |
| Streamlit offline workbench | Original design | FastAPI dashboard is primary; add if offline analysis needs grow |
| Formal policy learning (contextual bandit) | Original design | Insufficient data; prompt-injected insights are more practical |
| LangGraph workflow orchestration | TradingAgents | Adds dependency and complexity; custom pipeline is more transparent |
