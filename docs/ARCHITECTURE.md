# Architecture: LLM-Based Day-Trading Research Assistant

> Stable reference for system architecture, components, and data models.
> For implementation status and roadmap, see [ROADMAP.md](ROADMAP.md).
> For design rationale and open questions, see [DECISIONS.md](DECISIONS.md).
>
> Last updated: 2026-02-12

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
                    │   EXPLORER (Stage 2)    │  ← DONE
                    │   free-form tool use    │
                    │   + budget guardrails   │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   SIGNAL EXTRACTION     │  ← Built into final agent
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
                     │ yes
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

**Core loop:**
1. **Online:** news arrives → triage → explore (free-form tool use) → seal Snapshot
2. **Watch:** high-confidence signals → monitored position → exit → retrospective
3. **Offline:** label outcomes → score traces → reflect → update knowledge

**Key design principles:**
- **Multi-agent free-form tool use** — multiple LLMs (Grok, OpenAI, Gemini) run
  sequentially, each with full tool access, compounding evidence and perspectives.
- **Atomic learning artifacts** — every action produces an immutable Snapshot with
  full tool traces, enabling offline learning from complete trade lifecycles.
- **Budget guardrails in code** — cost limits, rate limits, and safety constraints
  are enforced programmatically; the LLM operates freely within those bounds.

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

### Multi-agent sequential pipeline (DONE)

A **multi-agent sequential pipeline** — multiple PydanticAI agents backed by
different LLM providers, each equipped with all available tools, running
sequentially so each builds on the previous agents' findings.

#### Why multi-agent?

Different LLMs bring different strengths: Grok has native X/Twitter search,
OpenAI has strong reasoning + web search, Claude excels at synthesis. Running
them sequentially compounds evidence and perspectives — like analysts passing
a research report around a trading desk.

#### Architecture

```
┌──────────── Sequential Orchestrator (explore()) ────────────┐
│                                                              │
│  Round 1:                                                    │
│    Agent 1 (Grok)   ──→ web_search + x_search + all tools   │
│    Agent 2 (OpenAI) ──→ web_search + all tools               │
│    Agent 3 (Gemini) ──→ all function tools (no web search)   │
│      each agent sees full context from prior agents          │
│      Agent 3 produces TradingSignal (structured output)      │
│                                                              │
│  If Agent 3 confidence < threshold → Round 2 (optional)      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

All agents share:
- **Framework:** PydanticAI — handles per-agent tool loop, message threading,
  structured output, budget enforcement
- **Tool tracing:** `TracingToolset` (WrapperToolset subclass) intercepts every
  tool call across all agents, recording ToolTrace with `raw_tool_output`
- **Budget:** `UsageLimits` per agent + total pipeline budget
- **Dependencies:** `RunContext[ExplorerDeps]` carries `MarketDataService`,
  tool traces, and accumulated context into tool functions

#### Provider capabilities (verified)

| Provider | Model prefix | Native search | x_search | Mix with function tools? |
|----------|-------------|---------------|----------|-------------------------|
| Grok (xAI) | `OpenAIResponsesModel` + xAI base_url | `WebSearchTool(search_context_size=None)` | Function tool wrapper (calls xAI Responses API) | Yes |
| OpenAI | `openai-responses:` | `WebSearchTool()` | N/A | Yes |
| Claude | `anthropic:` | `WebSearchTool()` | N/A | Yes |
| Gemini | `google-gla:` | Google grounding | N/A | **No** — requires two-instance workaround |

#### Context flow between agents

Each agent receives a prompt containing:
1. The original news event
2. All findings from prior agents (accumulated `context.rounds[]`)
3. System prompt with tool definitions, budget, and learned insights

Each agent's output (str findings) becomes part of the next agent's input.
No separate "blackboard" needed — the accumulated context IS the shared state.

#### Signal extraction — built into final agent's output_type

```python
class TradingSignal(BaseModel):
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float                          # 0.0 - 1.0
    horizon: Literal["15m", "60m", "1d"]
    magnitude_estimate: str                    # e.g. "0.5-1.5%"
    key_catalyst: str
    bull_case: str
    bear_case: str
    risk_factors: list[str]
```

The final agent must produce a valid `TradingSignal` or the run fails.

### Tools available to all agents

#### Web / social research tools

| Tool | Type | Provider support | Cost |
|------|------|-----------------|------|
| `web_search` | PydanticAI `WebSearchTool` (native, iterative) | Grok, OpenAI, Claude | Per-call LLM + tool fee |
| `x_search(query)` | Function tool (wraps xAI Responses API) | All agents can call it; Grok executes | Per-call Grok + tool fee |
| `x_stream_cache(symbol)` | Function tool (reads XStreamService cache) | All | Free (cached) |
| `url_fetch(url)` | Function tool (httpx + trafilatura) | All | Free (local) |

#### Financial data tools — via MarketDataService (Schwab → yfinance fallback)

| Tool | Status | What it reveals |
|------|--------|-----------------|
| `check_price(symbol)` | DONE | Real-time quote + recent 1-min candles |
| `check_market_context()` | DONE | SPY, VIX, session, real market hours |
| `check_options_activity(symbol)` | DONE | ATM IV, put/call ratio, volume/OI totals |
| `get_fundamentals(symbol)` | DONE | Market cap, P/E, EPS, beta, 52-week range |
| `get_movers(index)` | DONE | Top gainers/losers by % change |
| `check_insider_activity(symbol)` | DONE | Recent insider buys/sells |
| `get_company_news(symbol)` | DONE | Recent news articles per ticker |
| `get_price_history(symbol, period)` | DONE | Historical OHLCV |
| `get_technical_indicators(symbol, indicators)` | DONE | RSI, MACD, BBands, ATR, VWMA, MFI |
| `check_price_spike(symbol)` | DONE | Intraday spike detection |
| `check_volume_regime(symbol)` | DONE | Abnormal volume detection |

### Agent prompt design

Each agent gets a role-aware system prompt with: role description, prior findings,
tool definitions, lessons from experience, budget, the news event, and task
instructions. The final agent (only) has `output_type=TradingSignal`.

---

## 4. Data Sources & Tools

### 4a. LLM Providers — DONE

`trader/llm/client.py` — unified interface, three providers.

For the explorer pipeline, agents use PydanticAI's native provider support:

| Provider | PydanticAI model | Native search | Mix search + function tools? |
|----------|-----------------|---------------|----------------------------|
| Grok (xAI) | `OpenAIResponsesModel` + xAI base_url | `WebSearchTool(search_context_size=None)` | Yes |
| OpenAI | `openai-responses:gpt-5-mini` | `WebSearchTool()` | Yes |
| Claude | `anthropic:claude-sonnet-4-0` | `WebSearchTool()` | Yes |
| Gemini | `google-gla:gemini-3-flash` | Google grounding | **No** — requires two-instance workaround |

### 4b. Schwab Market Data — DONE

`trader/market/schwab_client.py` (~830 lines)

- Real-time quotes, intraday candles, Level 1 streaming
- Market context with real hours, options activity, fundamentals, movers

### 4c. yfinance Data Layer — DONE

Free, no API key. Complements Schwab and serves as fallback.
- Insider activity, fundamentals, price history, news, technical indicators

### 4d. Technical Indicators — DONE

Computed locally from yfinance OHLCV via `stockstats`. Zero cost.
RSI, MACD, Bollinger Bands, ATR, VWMA, MFI, SMA/EMA.

### 4e. Data Vendor Fallback — DONE

`trader/market/data_service.py` — `MarketDataService` with Schwab → yfinance fallback.

### 4f. Evidence Acquisition — DONE

`trader/evidence/` — URL extraction → fetch → extract → persist via trafilatura.

### 4g. X API Stream — DONE

`trader/xapi/` + `trader/online/x_stream_service.py` — filtered stream, burst mode, guardrails.

---

## 5. Knowledge & Learning

### 5a. Knowledge Store — PARTIAL

`trader/knowledge/store.py` — manages JSON files under `data/knowledge/`:

| File | Status | Purpose |
|------|--------|---------|
| `skip_patterns.json` | DONE | Auto-skip keywords/sources for pre-filter |
| `reliable_sources.json` | DONE (empty) | Source domain tracking |
| `insights.json` | TODO | Flat scored insights — primary knowledge artifact |

### 5b. Insights System — TODO

Flat scored insight objects. Scoring: starts at 1, bumped +1/-1 by reflection.
Top N by score injected into prompts as "Lessons from experience."

**Design principle:** Avoid premature formalism. Start with free-form text
insights, observe what forms emerge, then formalize only what's proven useful.

See [DECISIONS.md](DECISIONS.md) for parked ideas (LLMFactor factors, Thompson Sampling).

### 5c. BM25 Situation Memory — TODO

Uses `rank-bm25` (pure Python, no API calls) to match current situations
against past ones. Complements insights.json:
- `insights.json` = general principles distilled from many events
- BM25 memory = specific analogies from similar past situations

### 5d. Online Learning via Contextual Bandits — TODO (Phase D+)

Optimize orchestrator configuration decisions (triage threshold, pipeline
config, model routing, budget allocation) via contextual bandits.
See [DECISIONS.md](DECISIONS.md) for full design.

---

## 6. Snapshot & ToolTrace — Data Storage for Future Training

**Status: DONE (base + training-readiness enhancements)**

`trader/models/snapshot.py`, `trader/models/tool_trace.py`

### Design principle: store raw ephemeral inputs at maximum fidelity

The Snapshot is both an operational artifact and a future training sample.
Critical: store raw inputs NOW (web search results, X posts, options IV,
order book) — they can't be reconstructed later.

### Key data stored

- `data_modalities` index: `{modality: [trace_indices]}` for categorical sampling
- `rounds`: per-agent findings, model, usage, elapsed time
- `tool_traces`: every tool call with `raw_tool_output` and `modality` tag
- `price_context`: per-symbol quote data at trigger time
- `prediction`: TradingSignal from final agent

### Builder pattern

`SnapshotBuilder` accumulates data → `.seal()` → frozen `Snapshot`.

### Future training pipeline (informed by Trading-R1)

Each Snapshot can be converted to training data for SFT or RL:
- **SFT:** Reverse reasoning distillation — generate "ideal" reasoning after
  outcomes are known, train on that (not original exploration reasoning)
- **RL:** Full tool-call trajectory enables RL over investigation strategies
  (what to investigate, how, when to stop, what to conclude)
- **Categorical sampling:** `data_modalities` enables random modality dropout
  for training variety

---

## 7. Watch Lifecycle (Position Management)

### Overview

When the explorer produces a signal meeting confidence threshold, the system
creates a **Watch** — a monitored hypothetical position.

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

### Check-in depth levels

| Depth | What it does | Cost |
|-------|-------------|------|
| `lightweight` | Price check only. No LLM. | Free |
| `medium` | Price + options IV + quick news scan. May use LLM. | ~$0.02-0.05 |
| `full` | Full tool-use loop. All tools available. | ~$0.05-0.15 |
| `force_exit` | Must produce exit decision. Cannot choose "hold." | ~$0.05-0.15 |

### Holding check-in schedule

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

### Budget controls

| Control | Default | Env var |
|---------|---------|---------|
| Monitoring budget per Watch | $0.50 | `WATCH_MONITORING_BUDGET` |
| Max concurrent Watches | 5 | `MAX_CONCURRENT_WATCHES` |
| Entry confidence threshold | 0.7 | `WATCH_CONFIDENCE_THRESHOLD` |
| Max hold duration | 240 min | `WATCH_MAX_HOLD_MINUTES` |

---

## 8. Offline Loop

### 8a. Outcome labeling

Volatility-adjusted, multi-horizon (+15m, +60m, +4h). Normalize returns by
rolling 20-period volatility. Discretize into 5 classes. Also compute MFE/MAE.

### 8b. Hop scoring

Per-trace: novelty, prediction improvement, cost-effectiveness.
Per-sequence: which tool chains produced good outcomes.

### 8c. Reflection

LLM reviews batches of scored Snapshots + sealed Watches:
reinforce/weaken/propose insights, update BM25 memory. Human review before merge.

---

## 9. Infrastructure

### 9a. Database — DONE

`trader/db/database.py` — SQLite with `snapshots` + `watches` tables.

### 9b. Dashboard — DONE

`trader/web/` — FastAPI + HTMX + Bootstrap 5 + Chart.js. Server-side rendered,
no build step.

| Page | URL | Features |
|------|-----|----------|
| Dashboard | `/` | Stats cards (HTMX polling), active watches, manual explore form, live SSE event feed |
| Watches | `/watches` | Filterable table by status, force-exit buttons, detail pages with full lifecycle view |
| Snapshots | `/snapshots` | Filterable table by symbol, detail pages with collapsible agent rounds + tool traces |
| Costs | `/costs` | Budget progress bar, Chart.js daily trend + tool breakdown doughnut, history table |
| Config | `/config` | Read-only grouped settings display (9 categories, 46 fields) |
| Knowledge | `/knowledge` | JSON file viewer/editor with Save/Cancel for 7 knowledge files |

**Control actions (POST):**
- Force exit watch at current market price
- Manual exploration trigger (headline + symbols → full pipeline in background thread)
- Knowledge file editing (JSON validation, whitelist-guarded)

**Real-time:** SSE connection with green/red indicator, toast notifications for key events
(watch created/exited, snapshot sealed, explore complete/error), HTMX auto-refresh panels.

### 9c. Cost Control — DONE

`trader/llm/cost_tracker.py` — per-call estimation, per-tool breakdown,
daily + per-item budget enforcement. Wired into pipeline.

### 9d. Backfill — DONE

`trader/online/backfill.py` — batch driver for processing existing news files.

---

## 10. Project Structure

```
trader/
├── config.py                       # Settings (env vars, defaults)
├── online/
│   ├── orchestrator.py             # Watch loop, queue-decoupled processing
│   ├── triage.py                   # Pre-filter + LLM triage
│   ├── explorer_agent.py           # PydanticAI tools, TradingSignal, TracingToolset
│   ├── agent_pipeline.py           # Multi-agent sequential pipeline
│   ├── watcher.py                  # Watch monitoring scheduler
│   ├── backfill.py                 # Batch reprocessing
│   └── x_stream_service.py         # X stream burst service
├── models/
│   ├── snapshot.py                 # Snapshot + SnapshotBuilder
│   ├── watch.py                    # Watch + WatchBuilder
│   └── tool_trace.py               # ToolTrace per-hop recording
├── llm/
│   ├── client.py                   # Unified LLM client
│   ├── cost_tracker.py             # Budget enforcement
│   └── pricing.py                  # Per-model pricing tables
├── market/
│   ├── data_service.py             # MarketDataService (Schwab → yfinance)
│   ├── schwab_client.py            # Schwab wrapper
│   ├── yfinance_client.py          # yfinance wrapper
│   └── indicators.py               # Technical indicators via stockstats
├── evidence/                       # URL extraction + fetch + persist
├── knowledge/
│   ├── store.py                    # JSON knowledge file management
│   └── memory.py                   # BM25 situation memory (TODO)
├── xapi/                           # X API v2 client + stream
├── db/
│   └── database.py                 # SQLite persistence
├── web/                            # FastAPI dashboard (HTMX + Bootstrap 5)
│   ├── app.py                     # Routes (pages, API fragments, control actions)
│   ├── sse.py                     # SSE helpers
│   └── templates/                 # Jinja2: base, 6 pages, 6 partials
└── data/                           # Runtime data (gitignored)
    ├── snapshots/                  # Sealed Snapshot JSON files
    ├── watches/                    # Watch JSON files
    ├── evidence/                   # Extracted article text
    └── knowledge/                  # skip_patterns, insights, memories
```
