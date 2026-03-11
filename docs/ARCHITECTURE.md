# Architecture: LLM-Based Day-Trading Research Assistant

> Stable reference for system architecture, components, and data models.
> For implementation status and roadmap, see [ROADMAP.md](ROADMAP.md).
> For design rationale and open questions, see [DECISIONS.md](DECISIONS.md).
>
> Last updated: 2026-03-04

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
              └──┬────────┬──┘
                 │ yes    │ no
      ┌──────────▼──┐  ┌─▼───────────────┐
      │ WATCH        │  │ FOLLOW-UP        │  ← DONE
      │ LIFECYCLE    │  │ (no-buy)         │
      │ hold → exit  │  │ scheduled data   │
      │ → retro →    │  │ collection       │
      │ sealed Watch │  └─────────────────┘
      └──────┬───────┘
             │
      ┌──────▼──────────┐
      │ FOLLOW-UP        │  ← DONE
      │ (post-exit)      │
      │ scheduled data   │
      │ collection       │
      └──────┬──────────┘
             │
      ┌──────▼──────────┐
      │ OFFLINE LOOP     │
      │ label → score →  │
      │ reflect → update │
      │ insights.json    │
      └─────────────────┘
```

**Core loop:**
1. **Online:** news arrives → triage → explore (free-form tool use) → seal Snapshot
2. **Watch:** high-confidence signals → monitored position → exit → retrospective
3. **Follow-up:** scheduled post-event data collection (no-buy and post-exit)
4. **Offline:** label outcomes → score traces → reflect → update knowledge

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

A **multi-agent sequential pipeline** — multiple LLMs called via their native
SDKs, each equipped with all available tools, running sequentially so each
builds on the previous agents' findings.

#### Why multi-agent?

Different LLMs bring different strengths: Grok has native X/Twitter search,
OpenAI has strong reasoning + web search, Gemini has Google Search grounding.
Running them sequentially compounds evidence and perspectives — like analysts
passing a research report around a trading desk.

#### Architecture

```
┌──────────── Sequential Orchestrator (explore()) ────────────┐
│                                                              │
│  Pre-fetch: basic market data for primary symbols (once)     │
│                                                              │
│  Round 1:                                                    │
│    Agent 1 (Grok)   ──→ server-side web_search + x_search   │
│                         + all function tools (native SDK)    │
│    Agent 2 (OpenAI) ──→ server-side web_search              │
│                         + all function tools (native SDK)    │
│    Agent 3 (Gemini) ──→ Google Search grounding             │
│                         + all function tools (native SDK)    │
│      each agent sees: pre-fetched data + prior findings      │
│      + tool call ledger (full results from prior agents)     │
│      Agent 3 produces TradingSignal (JSON-in-prompt)         │
│                                                              │
│  If Agent 3 confidence < threshold → Round 2 (optional)      │
│  If an agent fails → partial traces saved, pipeline continues│
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

Key features:
- **Native SDK runners:** Each provider uses its own SDK directly — no agent
  framework overhead. `openai` SDK for OpenAI and Grok (xAI mirrors OpenAI API),
  `google-genai` SDK for Gemini. PydanticAI kept only for watcher check-ins and tests.
- **Pre-fetched market data:** Basic data (price, fundamentals, technicals, options,
  volume, insider, news, price history) is fetched once and included in the prompt.
  Agents focus on investigative tools (web_search, x_search, url_fetch).
- **Tool call ledger:** Each agent's investigative tool results (web_search, x_search,
  url_fetch) are passed downstream in full. Agents see what was already found.
- **Graceful degradation:** If an agent fails (e.g. timeout, API error), partial
  tool traces are preserved and the pipeline continues to the next agent.
- **Pure tool functions:** All 16 market data tools are pure functions in `tool_core.py`.
  Each runner wraps them into its SDK's function-calling format. Single source of truth.
- **Server-side search:** Grok gets server-side `x_search` + `web_search` (no inner
  API calls — massive latency improvement). OpenAI gets server-side `web_search`.
  Gemini gets Google Search grounding + all function tools simultaneously.
- **Tool tracing:** Each runner records tool call traces in the common `build_trace_dict()`
  format. Server-side search traces extracted from response metadata.
- **Budget:** Cost-based (`PIPELINE_MAX_COST_USD`, default $0.50). Per-agent limits:
  `pipeline_request_limit` (15), `pipeline_tool_calls_limit` (25). After each agent,
  cumulative cost checked; if over budget, skip remaining intermediates; final always runs.
- **Reasoning control:** Per-provider via runner kwargs:
  - OpenAI: `reasoning_effort` (low/medium/high), `reasoning_summary='detailed'`
  - Gemini: `thinking_config` with configurable thinking level
  - Grok: No effort control (grok-4 always max reasoning); reasoning tokens tracked
- **Reasoning token capture:** Each runner extracts reasoning/thinking tokens from
  provider-specific usage metadata and maps to a common `usage` dict.
- **Runner dispatch:** `AgentSpec.runner` field (`"grok"`, `"openai"`, `"gemini"`,
  `"pydanticai"`) determines which native SDK handles the agent. Pipeline loop calls
  `_dispatch_runner()` for each spec.

#### Provider capabilities (native SDKs)

| Provider | SDK | Server-side search | x_search | Function tools | Reasoning control |
|----------|-----|-------------------|----------|----------------|-------------------|
| Grok (xAI) | `openai` SDK → `https://api.x.ai/v1/` | `{"type": "web_search"}` | `{"type": "x_search"}` (server-side) | Yes — all 16 tools | grok-4 always max; no effort control |
| OpenAI | `openai` SDK (Responses API) | `{"type": "web_search"}` | N/A | Yes — all 16 tools | `reasoning_effort` (low/medium/high) + `reasoning_summary` |
| Gemini | `google-genai` SDK | `GoogleSearch()` grounding | N/A | Yes — all 16 tools (except x_search, x_stream_cache) | `thinking_config` (off/low/medium/high/dynamic) |

#### Context flow between agents

Each agent receives a prompt containing:
1. The original news event (headline + article content when available)
2. Auto-fetched context: FinnHub company news and earnings data for primary symbols
3. Pre-fetched market data: price, fundamentals, technicals, options, volume, insider, news, price history
4. All findings from prior agents (accumulated `context.rounds[]`)
5. Tool call ledger: full results from prior agents' investigative tools (web_search, x_search, url_fetch)
6. System prompt with investigation guidance and budget awareness

Each agent's output (str findings + tool results) becomes part of the next agent's input.
No separate "blackboard" needed — the accumulated context IS the shared state.

#### Signal extraction — JSON-in-prompt for native runners

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

The final agent's system prompt includes the TradingSignal JSON schema and
instructions to output it in a ```json block. The runner extracts and validates
via `TradingSignal.model_validate_json()` with fallback regex parsing.
If extraction fails, `signal=None` — partial data is always preserved.

### Tools available to all agents

#### Web / social research tools

| Tool | Type | Provider support | Cost |
|------|------|-----------------|------|
| `web_search` | Server-side (native per provider) | Grok (xAI), OpenAI, Gemini (Google grounding) | Per-call tool fee |
| `x_search` | Server-side on Grok; N/A on others | Grok only (server-side via xAI API) | $0.005/call + token cost |
| `x_stream_cache(symbol)` | Function tool (reads XStreamService cache) | Grok, OpenAI only | Free (cached) |
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
| `get_finnhub_news(symbol)` | DONE | FinnHub company news (free tier, 60 req/min) |
| `get_analyst_ratings(symbol)` | DONE | Analyst recommendation trends — buy/hold/sell distribution (FinnHub free tier) |
| `get_financial_statements(symbol)` | DONE | Income statement, balance sheet, cash flow |

### Agent prompt design

Each agent gets a role-aware system prompt tailored to its capabilities:
- **All agents now get function tools** — Grok, OpenAI, and Gemini all have full
  tool access (16 market data tools + url_fetch). Gemini also gets Google Search
  grounding simultaneously (impossible with PydanticAI, enabled by native SDK).
- All agents: role description, prior findings, news event, task instructions
- Pre-fetched data explicitly marked "DO NOT re-fetch for primary symbols"
- The final agent gets JSON output format instructions for TradingSignal

---

## 4. Data Sources & Tools

### 4a. LLM Providers — DONE

`trader/llm/client.py` — unified interface for non-pipeline LLM calls.

The explorer pipeline uses **native SDKs directly** (not PydanticAI):

| Provider | SDK | Pipeline runner | Server-side tools |
|----------|-----|----------------|-------------------|
| Grok (xAI) | `openai` → `https://api.x.ai/v1/` | `runners/grok_runner.py` | web_search + x_search |
| OpenAI | `openai` (Responses API) | `runners/openai_runner.py` | web_search |
| Gemini | `google-genai` | `runners/gemini_runner.py` | Google Search grounding |

PydanticAI is still used for: watcher check-ins (`watcher.py`), follow-up query planning, and test mode (`TestModel`).

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

### 4e. FinnHub Data — DONE

`trader/market/finnhub_client.py` — free tier (60 req/min, `FINNHUB_API_KEY`).

Complements Schwab/yfinance with data they don't carry:
- **Company news** — auto-fetched into user message + available as tool
- **Earnings surprises** — last 4 quarters beat/miss history (auto-fetched)
- **Earnings calendar** — next/last earnings date + estimates (auto-fetched)
- **Recommendation trends** — monthly buy/hold/sell analyst distribution (on-demand tool)

Note: News sentiment, price targets, and upgrade/downgrade endpoints are premium-only (403).

### 4f. Data Vendor Fallback — DONE

`trader/market/data_service.py` — `MarketDataService` with Schwab → yfinance fallback.

### 4g. Evidence Acquisition — DONE

`trader/evidence/` — URL extraction → fetch → extract → persist via trafilatura.

### 4h. X API Stream — DONE

`trader/xapi/` + `trader/online/x_stream_service.py` — filtered stream, burst mode, guardrails.

**Quality gate:** LLM-based relevance monitor (`trader/online/stream_quality.py`).
After N tweets (configurable, default 5), a cheap LLM (gemini-3-flash) evaluates
whether tweets match the target stock/news. If irrelevant, the stream is killed and
auto-retried with LLM-suggested revised filter rules (up to 3 retries, configurable).
Stale/backfill news skips streaming entirely (uses `x_search` instead).

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
- `rounds`: per-agent findings, model, usage (incl. `reasoning_tokens` + `details`), elapsed time, `thinking_summary`
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

## 7b. Follow-up Data Collection — DONE

**Status: DONE** — `trader/models/follow_up.py`, `trader/online/follow_up_collector.py`

### Overview

After a snapshot is sealed, ephemeral data (news coverage, Twitter sentiment, analyst
reactions) fades quickly. The Follow-up system schedules lightweight, periodic data
collection after snapshots are sealed — for both stocks we chose NOT to buy and stocks
we exited after watching.

Two trigger cases:
- **No-buy:** Snapshot sealed, no Watch created (neutral signal or low confidence)
- **Post-exit:** Watch fully sealed after retrospective phase completes

### Data model

```
FollowUp (frozen dataclass)
├── follow_up_id: "fu_{uuid_hex[:12]}"
├── snapshot_id, symbols, reason ("no_buy" | "post_exit")
├── schedule: ["+1h", "+4h", "+1d", "+3d", "+5d"]
├── collections: list[FollowUpCollection]
│   └── FollowUpCollection
│       ├── offset_label, collected_at
│       ├── price: {symbol: quote_data}
│       ├── news: [company_news_results]
│       ├── web_results: [{query, answer, citations, quality}]
│       ├── x_results: [{query, answer, citations, quality}]
│       ├── query_plan: {web_queries, x_queries, reasoning}
│       └── cost_usd
├── status: "active" | "complete"
└── total_cost_usd
```

Builder pattern: `FollowUpBuilder` → frozen `FollowUp` (same as Watch/Snapshot).

### Collection process (two phases)

**Phase 1: LLM Query Planner** — cheap gemini-3-flash call (~$0.001) proposes
targeted search queries informed by:
- Original headline + symbols + prediction context
- Key findings from the exploration pipeline
- Offset label (different time horizons need different queries)
- Prior collection results with quality ratings (feedback loop)

Output: `QueryPlan(web_queries, x_queries, reasoning)` via PydanticAI structured output.

**Phase 2: Mechanical Data Gathering** — no agent reasoning:
1. Price — `MarketDataService.get_quote()` per symbol (free)
2. News — `MarketDataService.get_company_news()` per symbol (free)
3. Web search — direct httpx calls to Grok API (`web_search_preview` tool)
4. X search — direct httpx calls to xAI API (`x_search` tool)

### Query effectiveness tracking

Each search result rated by simple heuristic:
- `"good"` — answer > 100 chars
- `"empty"` — answer short or generic
- `"error"` — API error

Prior query quality fed back to the LLM planner for subsequent collections.

### Scheduling

`FollowUpCollector` runs as a daemon thread (same pattern as `WatchMonitor`).
Default interval: 300s. Checks all active follow-ups, runs collections that are due
based on `started_at + parse_offset_to_minutes(label)`.

### Budget controls

| Control | Default | Env var |
|---------|---------|---------|
| Enabled | true | `FOLLOW_UP_ENABLED` |
| Schedule | +1h,+4h,+1d,+3d,+5d | `FOLLOW_UP_SCHEDULE` |
| Web searches per collection | 2 | `FOLLOW_UP_WEB_SEARCHES` |
| X searches per collection | 1 | `FOLLOW_UP_X_SEARCHES` |
| Max cost per follow-up | $0.20 | `FOLLOW_UP_MAX_COST` |
| Max concurrent follow-ups | 20 | `FOLLOW_UP_MAX_CONCURRENT` |
| Collector interval | 300s | `FOLLOW_UP_COLLECTOR_INTERVAL_S` |
| Planner model | gemini-3-flash | `FOLLOW_UP_PLANNER_MODEL` |

### Export unit

The evaluation unit is now: `snapshot + watch + follow_ups`
- Bought stocks: snapshot + watch + post-exit follow-up(s)
- Not-bought stocks: snapshot + no-buy follow-up(s)

Export endpoint (`/api/snapshots/{id}/export`) returns all three.

---

## 8. Reflection & Evaluation — DONE

**Status: DONE** — `trader/reflection/`

On-demand LLM evaluation of pipeline decision quality with nested decision trees
and two-tier actionable insights.

### 8a. Decision Timeline (EvalRecord)

`trader/reflection/eval_record.py` — transforms a snapshot + optional watch +
follow-ups into a nested JSON tree. Same data structure drives both the UI and
LLM evaluator.

Each node: `{type, summary, detail, children}`

```
triage (85% investigate)
├── agent_round (grok | 8 tools | 45k tokens)
│   ├── tool_call (web_search → 'TWLO earnings')
│   ├── tool_call (check_price → TWLO $111.89)
│   └── ...
├── agent_round (openai | 5 tools | 32k tokens)
│   └── ...
├── prediction (bullish 78% — 60m horizon)
├── watch_entry (TWLO @ $111.89 bullish)
│   ├── watch_checkin (hold — +0.4% lightweight)
│   ├── watch_checkin (hold — +0.8% medium)
│   └── ...
├── watch_exit (+1.39% — momentum exhausting)
└── follow_up (post_exit | TWLO | 3 collections | $0.04)
    ├── follow_up_collection (+1h | 2 web + 1 x | $0.015)
    ├── follow_up_collection (+4h | 2 web + 1 x | $0.015)
    └── follow_up_collection (+1d | 2 web + 1 x | $0.010)
```

**Backward compatible:** Old snapshots missing `triage`, `system_prompt`, or
`user_message` show `[not captured]` in those nodes.

### 8b. Pipeline Timeline UI

Added to snapshot detail page (`/snapshots/{id}`). Renders the EvalRecord tree
as nested Bootstrap accordions via recursive Jinja macro (`_timeline_node.html`).
Color-coded badges by node type. Expandable detail with prompts, tool outputs,
and usage stats.

### 8c. LLM Evaluator

`trader/reflection/evaluator.py` — Gemini-based (configurable via `REFLECTION_MODEL`).

1. User selects snapshots on `/reflection` page
2. Builds markdown timeline for each snapshot via `eval_record_to_markdown()`
3. Sends batch to Gemini with structured evaluation prompt
4. Returns per-snapshot grades (A-F), per-node assessments, and insights
5. Evaluation persisted to `evaluations` DB table

### 8d. Two-tier Insights

| Tier | Description | Action |
|------|-------------|--------|
| **A** | Auto-applicable (no code changes) | Applied to knowledge JSON files via `KnowledgeStore.append_to_list()` |
| **B** | Requires code changes | Saved to `data/reflection/suggestions/{timestamp}.md` for coding agent |

Tier A action types: `add_skip_keyword`, `add_signal_pattern`, `add_anti_pattern`,
`add_search_template`, `add_model_note`.

### 8e. Data Capture for Evaluation

Added to support full decision tree reconstruction:
- **Triage decision** stored in snapshot (`builder.set_triage()`)
- **System prompt + user message** stored per agent round
- **Check-in history** stored in watch (`checkin_history[]`)

---

## 9. Offline Loop — TODO

### 9a. Outcome labeling

Volatility-adjusted, multi-horizon (+15m, +60m, +4h). Normalize returns by
rolling 20-period volatility. Discretize into 5 classes. Also compute MFE/MAE.

### 9b. Hop scoring

Per-trace: novelty, prediction improvement, cost-effectiveness.
Per-sequence: which tool chains produced good outcomes.

### 9c. Automated Reflection

Scheduled batch evaluation of recent snapshots + sealed watches.
Builds on Phase D-1 on-demand evaluation.

---

## 10. Infrastructure

### 10a. Database — DONE

`trader/db/database.py` — SQLite with `snapshots`, `watches`, `follow_ups`, `event_log`, and `evaluations` tables.

### 10b. Dashboard — DONE

`trader/web/` — FastAPI + HTMX + Bootstrap 5 + Chart.js. Server-side rendered,
no build step.

| Page | URL | Features |
|------|-----|----------|
| Dashboard | `/` | Stats cards (HTMX polling), activity panel (10s refresh), active watches, manual explore form, live SSE event feed |
| Watches | `/watches` | Filterable table by status, force-exit buttons, detail pages with full lifecycle view |
| Snapshots | `/snapshots` | Filterable table (symbol, signal, date, price, volume, market cap, P/E), detail pages with pipeline timeline + collapsible agent rounds + tool traces, integrated backtest panel with strategy/allocation selection + portfolio simulation, SSE auto-refresh on new snapshots |
| Strategies | `/strategies` | Strategy parameter explorer |
| Costs | `/costs` | Budget progress bar, Chart.js daily trend + tool breakdown doughnut, history table |
| Config | `/config` | Read-only grouped settings display (10 categories, 47 fields) |
| Knowledge | `/knowledge` | JSON file viewer/editor with Save/Cancel for 7 knowledge files |
| Reflection | `/reflection` | Snapshot multi-select, LLM evaluation trigger, grade/insight results |

**Control actions (POST):**
- Force exit watch at current market price
- Manual exploration trigger (headline + symbols → full pipeline in background thread)
- Knowledge file editing (JSON validation, whitelist-guarded)

**Activity panel:** Shows all in-flight operations categorized by type (backfill, exploration,
follow-up collection, scheduled follow-ups). Each item shows progress, symbols, cost. Powered
by `ActivityTracker` — thread-safe in-memory state read by `/api/activity-panel` endpoint.

**Real-time costs:** Daily Cost card shows `sealed + inflight` cost. Inflight cost comes from
`ActivityTracker.get_inflight_cost()`, updated as explorations accumulate LLM spend.

**Real-time:** SSE connection (`/events` endpoint, `EventBus` → `sse.py`) with green/red
indicator, toast notifications for key events (watch created/exited, snapshot sealed, explore
complete/error), HTMX auto-refresh panels. Backtest lifecycle events (`backtest_started`,
`backtest_complete`, `backtest_error`) enable running backtests to persist across navigation.

**Backtest system:** Async job-based — `POST /api/strategies/backtest` returns a `job_id`
immediately (HTTP 202), runs the backtest engine in a background task, and publishes SSE
events on completion/error. `GET /api/strategies/backtest/{job_id}` returns job status +
results. Frontend stores `job_id` in localStorage for state recovery across navigation and
page refresh. See [BACKTEST-ARCHITECTURE.md](BACKTEST-ARCHITECTURE.md) for details.

### 10c. Cost Control — DONE

`trader/llm/cost_tracker.py` — per-call estimation, per-tool breakdown,
daily + per-item budget enforcement. Wired into pipeline.

### 10d. Backfill — DONE

`trader/online/backfill.py` — batch driver for processing existing news files.

### 10e. Running Services

**Trader app (dashboard + orchestrator):**
```bash
# Launch (foreground)
uv run python -m trader.main

# Launch (background, skip backfill)
BACKFILL_ON_START=false nohup uv run python -m trader.main > /tmp/alpaca-dashboard.log 2>&1 &

# Kill
pkill -f "trader.main"
```

**Error logging**: The trader app writes warnings and errors to a rotating log file. Check this first when diagnosing issues:
```bash
# View recent errors
cat logs/trader.log

# Tail live
tail -f logs/trader.log
```
Configured via env vars: `LOG_FILE` (default: `logs/trader.log`), `LOG_LEVEL` (default: `WARNING`), `LOG_KEEP_DAYS` (default: `14`). Rotates daily at midnight. See `trader/logging_config.py`.

**News websockets** are auto-launched as subprocesses by the trader app, controlled by env vars:

| Env var | Default | Script |
|---------|---------|--------|
| `WS_INSIGHT_SENTRY` | `true` | `websocket/insight_sentry_news.py` |
| `WS_ALPACA` | `false` | `websocket/alpaca_news.py` |

They terminate automatically when the trader app exits. To run standalone:
```bash
uv run python -u websocket/insight_sentry_news.py
uv run python websocket/alpaca_news.py
```
Insight Sentry requires `INSIGHT_SENTRY_API_KEY` in `.env`. Saves to `data/news/incoming/insight_sentry/`. Auto-reconnects on disconnect.

---

## 11. Project Structure

```
trader/
├── config.py                       # Settings (env vars, defaults)
├── online/
│   ├── orchestrator.py             # Watch loop, queue-decoupled processing
│   ├── triage.py                   # Pre-filter + LLM triage
│   ├── explorer_agent.py           # PydanticAI tools, TradingSignal, TracingToolset (watcher/tests)
│   ├── agent_common.py             # Shared types: AgentRunResult, ToolDef, build_trace_dict
│   ├── tool_core.py                # Pure tool functions (16 tools) + TOOL_REGISTRY
│   ├── agent_pipeline.py           # Multi-agent sequential pipeline + runner dispatch
│   ├── runners/                    # Native SDK runners
│   │   ├── grok_runner.py          # openai SDK → xAI (server-side x_search + web_search)
│   │   ├── openai_runner.py        # openai SDK Responses API (server-side web_search)
│   │   └── gemini_runner.py        # google-genai SDK (Google Search + function tools)
│   ├── watcher.py                  # Watch monitoring scheduler (still PydanticAI)
│   ├── follow_up_collector.py      # Follow-up data collection daemon
│   ├── activity_tracker.py         # Thread-safe in-flight activity tracking
│   ├── backfill.py                 # Batch reprocessing
│   ├── x_stream_service.py         # X stream burst service
│   └── stream_quality.py           # LLM quality gate for stream bursts
├── models/
│   ├── snapshot.py                 # Snapshot + SnapshotBuilder
│   ├── watch.py                    # Watch + WatchBuilder
│   ├── follow_up.py                # FollowUp + FollowUpBuilder
│   └── tool_trace.py               # ToolTrace per-hop recording
├── llm/
│   ├── client.py                   # Unified LLM client
│   ├── cost_tracker.py             # Budget enforcement
│   └── pricing.py                  # Per-model pricing tables
├── market/
│   ├── data_service.py             # MarketDataService (Schwab → yfinance)
│   ├── schwab_client.py            # Schwab wrapper
│   ├── yfinance_client.py          # yfinance wrapper
│   ├── finnhub_client.py           # FinnHub free tier (news, earnings, analyst)
│   └── indicators.py               # Technical indicators via stockstats
├── evidence/                       # URL extraction + fetch + persist
├── reflection/
│   ├── eval_record.py              # Nested decision tree builder
│   └── evaluator.py                # LLM evaluation + insight generation
├── knowledge/
│   ├── store.py                    # JSON knowledge file management
│   └── memory.py                   # BM25 situation memory (TODO)
├── xapi/                           # X API v2 client + stream
├── db/
│   └── database.py                 # SQLite persistence
├── web/                            # FastAPI dashboard (HTMX + Bootstrap 5)
│   ├── app.py                     # Routes (pages, API fragments, control actions)
│   ├── sse.py                     # SSE helpers
│   └── templates/                 # Jinja2: base, 7 pages, 10 partials
└── data/                           # Runtime data (gitignored)
    ├── snapshots/                  # Sealed Snapshot JSON files
    ├── watches/                    # Watch JSON files
    ├── evidence/                   # Extracted article text
    └── knowledge/                  # skip_patterns, insights, memories
```
