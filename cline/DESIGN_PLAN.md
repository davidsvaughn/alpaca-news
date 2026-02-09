# Design Plan: LLM-Based Day-Trading Research Assistant (Snapshot + Policy Learning)

## Executive Summary

This system is a **news-triggered, multi-LLM research pipeline** whose core objective is to **learn an information acquisition policy** (what to search, where, and when to stop) that improves short-horizon price-move predictions under strict cost constraints.

The key design upgrade (from `cline/chat1.md`) is making a **time-aligned “Snapshot”** the **atomic learning artifact**:

- Online (real-time): explore + capture evidence → seal Snapshot
- Offline (async): label outcomes from market data → learn/update search policy

Everything else (triage, search tools, streams, dashboard) exists to produce and learn from these Snapshots.

---

## 1. Architecture Overview (Two Loops)

### Online loop: Explore + Capture (per event)

**Goal:** maximize *information capture quality per dollar*.

**Output:** one immutable **Snapshot** containing tool traces (multi-hop chain), market context, and (optional) a prediction.

### Offline loop: Backtest + Learn Policies (periodic)

**Goal:** attach outcomes to Snapshots (future returns) and learn which actions/hops were worth the cost.

**Output:** versioned policy updates (action weights, stop thresholds, source allow/deny lists) + reports.

---

### Architecture diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ONLINE ORCHESTRATOR                           │
│             (watch output/alpaca + asyncio event loop)                │
└───────────────┬───────────────────────────┬──────────────────────────┘
                │                           │
        ┌───────▼────────┐          ┌───────▼─────────┐
        │ TRIAGE FILTER   │          │ EXPLORER        │
        │ (skip vs invest)│          │ (multi-hop)     │
        └───────┬────────┘          └───────┬─────────┘
                │                           │
                │                    ┌──────▼──────────┐
                │                    │ SCHWAB CONTEXT   │
                │                    │ (history+stream) │
                │                    └──────┬──────────┘
                │                           │
                └───────────────┬───────────▼───────────┬───────────┐
                                │   SNAPSHOT (sealed)   │           │
                                │ (tool traces + ctx)    │           │
                                └───────────┬────────────┘           │
                                            │                        │
                                    ┌───────▼────────┐       ┌───────▼────────┐
                                    │ OFFLINE LABELER │       │ DASHBOARD       │
                                    │ (+15m/+60m/...) │       │ (monitor/edit)  │
                                    └───────┬────────┘       └────────────────┘
                                            │
                                    ┌───────▼────────┐
                                    │ POLICY LEARNER  │
                                    │ (weights/stops) │
                                    └─────────────────┘
```

---

## 2. The Agents Question: Agents SDK vs. Direct API Calls

**Recommendation: Start with direct API calls, with a thin orchestration layer you own.**

### Why NOT use OpenAI Agents SDK (yet):
- The Agents SDK locks you into OpenAI models for orchestration. You want to freely swap between OpenAI, Gemini, and Grok — each has unique strengths (Grok has `x_search`, Gemini has `google_search` grounding, OpenAI has the strongest reasoning).
- Agent handoffs add latency and opaque control flow. For a latency-sensitive day-trading system, you want explicit, auditable control over every LLM call.
- The SDK's value is in multi-turn conversational routing. Your system is more of a **pipeline** — sequential stages with clear inputs/outputs.
- Cost control is critical. With the Agents SDK, agents can recursively call tools in ways that are harder to cap.

### What to do instead:
Build a **lightweight pipeline orchestrator** with:
- **Explicit loop structure** (Online: triage + explore + capture; Offline: label + learn + propose)
- A **unified LLM client wrapper** that lets you call OpenAI, Gemini, or Grok with the same interface
- **Cost tracking** baked into every call
- **Per-stage model config** in `.env` / config

### When to revisit Agents SDK:
If you later want the system to have more autonomous, multi-turn reasoning chains (e.g., "the model decides to do 3 follow-up searches based on what it found"), the Agents SDK could wrap specific stages. But start simple.

---

## 3. Unified LLM Client Design

A key abstraction — one interface, three providers:

```python
class LLMClient:
    """Unified interface to OpenAI, Gemini, and Grok."""
    
    def query(self, prompt, model=None, tools=None, provider=None) -> LLMResponse:
        """
        tools can include: "web_search", "x_search", "google_search"
        provider: "openai" | "gemini" | "grok" (auto-selected if not specified)
        """
    
    # Provider-specific capabilities:
    # - OpenAI: responses API, web_search tool
    # - Gemini: generate_content with GoogleSearch grounding 
    # - Grok:   responses API (OpenAI-compatible), web_search + x_search tools
```

**Key insight from the docs:**
- **Grok uses the OpenAI-compatible Responses API** (same `client.responses.create()` pattern, just pointed at `https://api.x.ai/v1`). This makes OpenAI ↔ Grok swapping trivial.
- **Gemini** uses its own `google-genai` SDK with `types.Tool(google_search=types.GoogleSearch())`.
- **OpenAI** uses the Responses API with `tools=[{"type": "web_search"}]`.

### Per-stage model configuration (`.env`):

```env
TRIAGE_MODEL=grok-4-1-fast          # fast + cheap for filtering
TRIAGE_PROVIDER=grok
RESEARCH_MODEL=gpt-4o               # strong reasoning for research
RESEARCH_PROVIDER=openai
SENTIMENT_MODEL=gemini-2.5-flash    # Google Search grounding for sentiment
SENTIMENT_PROVIDER=gemini
XSEARCH_MODEL=grok-4-1-fast         # only Grok has x_search
DECISION_MODEL=gpt-4o               # strongest reasoning for buy/sell
REASONING_LEVEL=medium               # low/medium/high (controls token budget)
```

---

## 4. Pipeline Stages in Detail

### Stage 1: News Triage Filter

**Trigger:** `watchdog` FileSystemEventHandler watches `output/alpaca/` for new `.json` files.

**Purpose:** Fast, cheap classification — is this news actionable?

**Process:**
1. Load the news JSON (headline, summary, symbols, source, content)
2. Check against **learned skip patterns** from Knowledge Store (keywords, phrases, sources that are known fluff)
3. If not pre-filtered, send to LLM with a triage prompt:
   ```
   You are a financial news triage agent. Evaluate whether this news item 
   could signal imminent stock price movement (within minutes to hours).
   
   REJECT if: retrospective/hypothetical articles, generic market commentary, 
   old re-hashed news, press releases with no price catalyst, ...
   
   [Known skip patterns from knowledge store injected here]
   
   Return: {action: "investigate" | "skip", confidence: 0-1, reasoning: "...", 
            symbols: [...], skip_patterns_learned: [...]}
   ```
4. Log the decision. If "skip", optionally store any new skip patterns learned.

**Model choice:** Fast & cheap (e.g., `grok-4-1-fast` or `gpt-4o-mini`)

---

### Stage 2: Exploration (Multi-hop, Two-phase)

This replaces the naive “parallel fan-out” design. Searches are **multi-hop** and explicitly recorded as an **action trace** so we can learn “which hop was worth it?” offline.

#### The correct mental model

We want **asymmetric exploration**:

- **Phase 1:** wide, cheap, shallow → generate hypotheses
- **Phase 2:** narrow, selective, deeper → confirm/deny top-K hypotheses

The key rule is: **explore to create disagreement (competing narratives), not to accumulate volume.**

#### Phase 1: Broad hypothesis generation (1 hop)

Goal: enumerate plausible explanations and quickly assess recency.

Typical actions (budgeted):
- 1–2 `web_search` / `google_search` (high recall)
- 1 `x_search` (broad, low filter)
- 0–1 market anomaly checks (price/volume)

Output: a small set of hypotheses like:
- “rumor → confirmation pending”
- “analyst reaction”
- “macro spillover”
- “recycled/false headline”

#### Phase 2: Selective deepening (beam-like but gated)

Keep only **top K hypotheses** (K=2–3). Each surviving hypothesis gets **one dedicated follow-up** using different tools/templates (enforce orthogonality).

Stop early if marginal value < marginal cost.

---

## 5. Action Menu (Finite, Learnable)

To make learning tractable, the explorer should choose from a **finite menu** of actions instead of inventing arbitrary queries each time.

An action is:

> (tool × query_template × constraints)

The explorer’s job is to select the next action, given the current state summary.

### Web search actions (OpenAI web_search / Gemini google_search)

| Action ID | Template | Purpose |
|---|---|---|
| `news_confirmation` | `"latest confirmation of {headline} {symbol}"` | rumor → confirmation |
| `breaking_followup` | `"breaking {symbol} today"` | freshness check |
| `filing_check` | `"site:sec.gov {company} 8-K"` | regulatory catalyst |
| `analyst_reaction` | `"analyst reaction {symbol}"` | secondary effects |

### X search actions (Grok x_search)

| Action ID | Template |
|---|---|
| `x_realtime_rumor` | `"{symbol} rumor OR hearing OR channel checks"` |
| `x_volume_alerts` | `"{symbol} unusual volume"` |
| `x_insider_accounts` | `"{symbol}" from:{trusted_handles}` |

### Market-data-only actions (non-LLM)

| Action ID | Purpose |
|---|---|
| `price_spike_check` | confirm move vs noise |
| `volume_regime_shift` | detect abnormal activity |

### Stop actions (also learnable)

Stopping is a first-class decision.

| Stop ID | Meaning |
|---|---|
| `STOP_CONFIRMED` | sufficient confirmation |
| `STOP_LOW_SIGNAL` | evidence quality too weak |
| `STOP_BUDGET` | marginal value < cost |
| `STOP_REDUNDANT` | no new info vs prior hops |

---

## 6. Snapshot + ToolTrace (Atomic Learning Unit)

Instead of treating “logs” as separate tables, we store a **single Snapshot artifact per event** that is replayable enough for offline learning.

### Snapshot (v1) — suggested schema

```json
{
  "snapshot_id": "uuid",
  "version": "v1",
  "created_at": "2026-02-09T14:32:11Z",
  "trigger": {
    "type": "alpaca_news",
    "alpaca_timestamp": "2026-02-09T14:31:58Z",
    "headline": "...",
    "summary": "...",
    "source": "reuters",
    "symbols": ["NVDA", "AMD"]
  },
  "market_context": {
    "session": "market_open | premarket | afterhours",
    "spy_return_15m": -0.12,
    "vix_level": 19.4
  },
  "price_context": {
    "per_symbol": {
      "NVDA": {
        "last_price": 612.30,
        "recent_candles_1m": [
          {"t": "...", "o": 611.8, "h": 612.4, "l": 611.7, "c": 612.3, "v": 18234}
        ]
      }
    }
  },
  "exploration_budget": {"max_hops": 3, "max_cost_usd": 0.35},
  "tool_traces": [],
  "prediction": {"direction": "up | down | none", "confidence": 0.71, "horizon": "60m"},
  "cost_summary": {"total_usd": 0.21, "by_tool": {"web_search": 0.12, "x_search": 0.09}}
}
```

### ToolTrace (one per hop)

Each hop records: **state → action → observation → stop**.

```json
{
  "trace_id": "trace_2",
  "hop_index": 2,
  "parent_trace_id": "trace_1",
  "decision_context": {
    "state_summary": "Rumor of NVDA supply constraint; no confirmation yet",
    "reason_for_action": "seek confirmation from social / insiders"
  },
  "action": {
    "tool": "x_search",
    "provider": "grok",
    "query_template": "x_realtime_rumor",
    "query": "NVDA supply constraint OR shortage",
    "filters": {"recency_hours": 6}
  },
  "execution": {
    "model": "grok-4-1-fast",
    "start_time": "2026-02-09T14:32:20Z",
    "end_time": "2026-02-09T14:32:24Z",
    "cost_usd": 0.045
  },
  "results": [
    {
      "rank": 1,
      "source_type": "x_post",
      "author": "@semianalyst",
      "timestamp": "2026-02-09T14:20:11Z",
      "text": "Hearing from channel checks that NVDA shipments delayed...",
      "content_hash": "sha256:..."
    }
  ],
  "extracted_signals": {"sentiment": "bullish", "novelty": "high", "confirmation_strength": "weak"},
  "stop_signal": {"should_stop": false, "reason": "confirmation incomplete"}
}
```

### Hard constraint

**Never store “the internet” — store “the evidence”.**

- store top K results per tool call (K=5–10)
- store raw result fields + citations/grounding metadata
- store a structured “takeaways” summary per hop
- dedupe via content hash

---

## 7. Market Data: Price Context + Streaming (Schwab)

We still use Schwab for:
- historical candles for labeling + features
- real-time streams for live context and monitoring

**Process:**

1. **Fetch historical price data** via schwabdev:
   ```python
   # Recent intraday data (1-min candles, last day)
   history = client.price_history(symbol, periodType="day", period=1, 
                                   frequencyType="minute", frequency=1)
   ```

2. **Start real-time stream** for symbols of interest:
   ```python
   streamer = client.stream
   streamer.start(receiver=price_handler)
   streamer.send(streamer.level_one_equities(
       keys=symbols, 
       fields="0,1,2,3,4,5,8,10,11,12,17,18,33,42"  
       # bid, ask, last, bid/ask size, volume, high, low, close, open, 
       # net change, mark, %change
   ))
   ```

3. **Synthesis prompt (optional)**

   In capture-first mode, we can store an optional prediction, but the primary goal is still to seal a high-quality Snapshot.

   ```
   Given:
   - Trigger news: {headline + summary}
   - Tool traces (multi-hop): {tool_traces with citations/grounding}
   - Price context: {recent candles + stream last price + volume stats}
   - Known policies/patterns: {skip patterns, reliable sources, current policy version}

   Produce (optional):
   - direction: up|down|none
   - confidence: 0-1
   - horizon: 15m|60m|1d
   - short rationale referencing trace_ids
   ```

---

## 8. Decision + Monitoring (Paper-first)

**Purpose:** If Stage 3 identifies an opportunity, monitor and manage it.

**Process:**
1. Log the hypothetical (or real) trade entry
2. Continue monitoring via Schwab stream
3. Periodically re-evaluate with LLM:
   - Has the thesis changed?
   - Are there new developments? (trigger another web/X search)
   - Has target/stop been hit?
4. Log exit and P&L
5. **Feed outcome back to Knowledge Store** for learning

---

## 9. Offline Loop: Labeling + Learning + Proposals

### 9a. Outcome labeling (define targets early)

For each Snapshot, compute labels from market data (Schwab historical candles):

- direction/return over horizons: **+15m**, **+60m**, **+1d** (start with 2–3 horizons)
- optional magnitude buckets (e.g., >0.5%, >1%)
- optional MFE/MAE (max favorable/adverse excursion)

These labels let us backtest:
- overall prediction accuracy
- marginal value of each hop/tool/template ("was hop #2 worth $0.04?")

### 9b. Policy learning (v1)

Start simple:
- weighted action selection over the Action Menu
- heuristic stopping thresholds

As data accumulates, upgrade to:
- contextual bandit for action selection
- learned stopping policy

### 9c. Reflection (constrained)

Reflection should generate **versioned proposals**, not mutate production logic.

---

## 10. Knowledge Store + Policies (What “Learning” Means)

This is the most important long-term differentiator. A **hybrid storage** approach:

### 10a. Database (Postgres)

Tables (initial):
- **`snapshots`** — one row per Snapshot (JSON blob + metadata)
- **`outcome_labels`** — computed returns per snapshot and horizon
- **`policy_versions`** — active policy + historical versions
- **`policy_proposals`** — offline-generated proposals + validation status
- **`api_costs`** — per tool/model call costs (can also be embedded in Snapshot)

### 10b. JSON Knowledge Files (`data/knowledge/`)

Editable, human-readable files that the system reads and updates:

```
data/knowledge/
├── skip_patterns.json        # headlines/keywords to auto-skip
├── reliable_sources.json     # domains/URLs ranked by reliability & freshness
├── search_strategies.json    # effective search query templates
├── x_search_strategies.json  # effective X search patterns  
├── signal_patterns.json      # patterns that preceded profitable trades
├── anti_patterns.json        # patterns that preceded losses
└── model_notes.json          # model-specific observations (which model is best at what)
```

Example `skip_patterns.json`:
```json
{
  "headline_keywords": ["if you had invested", "dividend aristocrat", "10 years ago"],
  "sources_to_skip": ["motleyfool.com/retrospective"],
  "symbol_contexts": {"SPY": "skip if headline is purely political/non-economic"},
  "last_updated": "2026-02-09T14:00:00Z",
  "auto_learned": 142,
  "human_edited": 7
}
```

### 10c. Offline learning loop (proposal-only)

After each trade outcome (or periodical batch review):
```python
# Reflection prompt to a reasoning model:
"""
Review these recent trade outcomes:
{trade_log entries with outcomes}

Current knowledge patterns:
{current skip_patterns, signal_patterns, etc.}

What patterns do you notice? What should be added/modified in our knowledge store?
Suggest specific updates to: skip_patterns, reliable_sources, search_strategies, 
signal_patterns, anti_patterns.
"""
```

The offline loop produces **proposals** that are validated before becoming active policy.

Example proposal:

```json
{
  "proposal_id": "2026-02-09-R1",
  "change_type": "adjust_action_weight",
  "target": "x_realtime_rumor",
  "delta": 0.12,
  "evidence": ["snapshot_123", "snapshot_141"],
  "expected_effect": "improves 60m direction accuracy by ~3%",
  "rollback_condition": "accuracy < baseline after 50 samples"
}
```

---

## 11. Learning Mode (Exploration Controls)

A special operating mode where the system:

1. **Never executes real trades** — everything is hypothetical
2. **Aggressively explores** — lower triage threshold, investigates more news items
3. **A/B tests** different search strategies, model choices, reasoning approaches
4. **Tracks hypothetical P&L** with timestamps
5. **Runs reflection loops** more frequently (e.g., every 50 news items or end of each trading day)
6. **Generates daily learning reports** summarizing what was learned

```env
LEARNING_MODE=true
LEARNING_TRIAGE_THRESHOLD=0.3    # lower = investigate more (vs 0.6 in production)
LEARNING_REFLECTION_INTERVAL=50  # reflect every N news items
LEARNING_EXPLORE_RATE=0.3        # 30% of events allow extra breadth
MAX_PHASE1_ACTIONS=4
MAX_PHASE2_BRANCHES=2
MAX_TOTAL_HOPS=3
```

---

## 12. Cost Control & Monitoring

### Pricing source of truth

Maintain a single pricing table for all providers/tools in:

`trader/llm/pricing.py`

This should include:
- per-1M-token rates (input/output)
- per-call tool fees (e.g. OpenAI `web_search`, Grok `x_search`)

**Note:** tool pricing, especially Google Search grounding, may change and should be treated as configurable/estimated until confirmed.

### Per-call tracking:
```python
class CostTracker:
    """Wraps every LLM call, logs to api_costs table."""
    
    def __init__(self, daily_budget=5.00):
        self.daily_budget = daily_budget
        self.daily_spent = 0.0
    
    def check_budget(self, estimated_cost):
        if self.daily_spent + estimated_cost > self.daily_budget:
            raise BudgetExceeded(f"Daily budget ${self.daily_budget} would be exceeded")
    
    def log_call(self, provider, model, input_tokens, output_tokens, cost, stage, purpose):
        # Log to SQLite api_costs table
```

### `.env` controls:
```env
MAX_DAILY_COST=5.00
MAX_COST_PER_NEWS_ITEM=0.50
MAX_WEB_SEARCHES_PER_ITEM=3
MAX_X_SEARCHES_PER_ITEM=2
COST_ALERT_THRESHOLD=0.80  # alert at 80% of daily budget
```

---

## 13. Storage & Deployment (Postgres / Supabase)

### Recommendation

- **Default datastore: Postgres** (local Docker for dev; managed Postgres/Supabase for prod if desired)
- SQLite can still be useful for **unit tests / quick prototypes**, but the system’s “native” persistence should be Postgres.

### Why Postgres fits this system

This system stores lots of **Snapshots** (JSON) + derived labels + policy versions, and will have concurrent writers (online capture, offline labeler, dashboard edits). Postgres supports:

- **JSONB** for Snapshot/ToolTrace storage (queryable + indexable)
- strong concurrency & integrity guarantees
- future extensions (e.g., `pgvector` for similarity search over past snapshots)

### Supabase (optional)

Supabase is helpful as *infra convenience* (managed Postgres, auth, dashboards), especially if you want remote access to the UI.

If using Supabase, I’d still keep the core design:

- store **evidence, not the internet** (cap tool results, dedupe, avoid unbounded page dumps)
- store immutable Snapshots and append-only audit logs

---

## 14. Web Dashboard (FastAPI + HTMX)

A lightweight dashboard for monitoring and control:

### Pages:
1. **Live Feed** — real-time view of news items being processed, triage decisions, active investigations
2. **Trade Log** — all hypothetical/real trades with P&L, reasoning chains
3. **Cost Monitor** — API costs by provider, model, stage, day — charts and running totals
4. **Knowledge Viewer** — browse and edit all knowledge files (skip patterns, reliable sources, etc.)
5. **Config Panel** — live editing of model selections, thresholds, cost limits (writes to `.env` or runtime config)
6. **Learning Report** — daily summaries of what the system learned

### Tech:
- **FastAPI** backend (async, fast)
- **HTMX** for dynamic updates without heavy JS framework
- **SSE (Server-Sent Events)** for real-time streaming of pipeline activity
- **Postgres** for persistence (local Docker for dev; Supabase optional)

### Streamlit (optional “offline workbench”)

Streamlit can be useful later as a **research/analysis UI** (offline loop), e.g.:

- browsing Snapshots + outcome labels
- quick plotting (PnL curves, confusion matrices)
- manual inspection/triage of policy proposals

But I would not replace the operational FastAPI dashboard with Streamlit.

---

## 15. Real Trading Readiness (Design Now, Enable Later)

Since you eventually want live trades, we should design the execution layer up front with hard safety boundaries.

### Mode flags

Use a hard guard:

```env
TRADING_MODE=paper   # paper|live
```

### Risk controls (enforced outside the LLM)

- max daily loss
- max position size / max notional exposure
- max concurrent positions
- kill switch

### Audit log

Store immutable records for:

- every recommendation (with snapshot_id)
- every order request (payload)
- every broker response
- every fill/cancel/replace

This pairs naturally with the Snapshot: you can always answer “why did we trade?”

---

## 16. Project Structure (Revised)

```
alpaca-news/
├── alpaca/
│   └── news_websocket.py          # existing - news ingestion
├── schwab/
│   └── main.py                    # existing - schwab client example
├── trader/                        # main application
│   ├── __init__.py
│   ├── main.py                    # entry point - starts pipeline + dashboard
│   ├── config.py                  # loads .env, runtime config management
│   ├── online/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # watches output/alpaca, seals snapshots
│   │   ├── triage.py              # Stage 1
│   │   ├── explorer.py            # Stage 2 (two-phase multi-hop)
│   │   ├── price_capture.py       # Schwab context capture
│   │   └── monitor.py             # monitoring loop (paper-first)
│   ├── offline/
│   │   ├── __init__.py
│   │   ├── labeler.py             # attach outcomes (+15m/+60m/+1d)
│   │   ├── scorer.py              # estimate hop/tool value
│   │   ├── policy.py              # propose updates
│   │   └── validator.py           # validate before activation
│   ├── models/
│   │   ├── __init__.py
│   │   ├── snapshot.py            # Snapshot schema + helpers
│   │   ├── tool_trace.py          # ToolTrace schema + helpers
│   │   └── actions.py             # finite action menu + stopping actions
│   ├── prompts/                   # all LLM prompts as .md files
│   │       ├── triage.md
│   │       ├── explore_phase1.md
│   │       ├── explore_phase2.md
│   │       ├── hypothesis_rank.md
│   │       ├── decision.md
│   │       └── reflection_offline.md
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py              # unified LLM client (OpenAI/Gemini/Grok)
│   │   ├── openai_provider.py     # OpenAI Responses API wrapper
│   │   ├── gemini_provider.py     # Gemini with Google Search grounding
│   │   ├── grok_provider.py       # Grok with web_search + x_search
│   │   └── cost_tracker.py        # cost logging and budget enforcement
│   ├── market/
│   │   ├── __init__.py
│   │   ├── schwab_client.py       # schwabdev wrapper for price data
│   │   └── stream_manager.py      # manages real-time price streams
│   ├── knowledge/
│   │   ├── __init__.py
│   │   ├── store.py               # knowledge store manager
│   │   ├── learning.py            # learning loop / reflection engine
│   │   └── data/                  # knowledge JSON files
│   │       ├── skip_patterns.json
│   │       ├── reliable_sources.json
│   │       ├── search_strategies.json
│   │       ├── signal_patterns.json
│   │       └── anti_patterns.json
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py              # SQLAlchemy models
│   │   └── database.py            # database connection management (Postgres)
│   └── web/
│       ├── __init__.py
│       ├── app.py                 # FastAPI app
│       ├── routes.py              # API endpoints
│       ├── sse.py                 # server-sent events for live updates
│       └── templates/
│           ├── base.html
│           ├── feed.html
│           ├── trades.html
│           ├── costs.html
│           ├── knowledge.html
│           └── config.html
├── data/                          # runtime files (knowledge JSON, exports, reports)
├── output/
│   └── alpaca/                    # news articles (existing)
└── .env
```

---

## 17. Implementation Phases (Revised)

### Phase 1: Capture-first (Start here)
- Snapshot + ToolTrace schema + persistence (**Postgres JSONB** + JSON knowledge files)
- Online orchestrator watches `output/alpaca/` and **seals Snapshots**
- Stage 1 triage filter
- Minimal Phase 1 exploration actions (1 web + 1 X) recorded as traces
- Cost tracker integrated into Snapshot

### Phase 2: Explorer v1 (Action Menu + Two-phase)
- Implement finite action menu + stopping actions
- Two-phase exploration with top-K hypothesis selection
- Orthogonality enforcement / redundancy detection
- Schwab context capture (history + stream) included in Snapshot

### Phase 3: Offline labeling + policy proposals
- Outcome labeler (+15m/+60m/+1d returns)
- Policy proposal generator (weights, stop thresholds, source lists)
- Validator to prevent bad updates

### Phase 4: Dashboard & Monitoring
- FastAPI web dashboard
- Live feed, trade log, cost monitor
- Knowledge viewer/editor
- Config panel

### Phase 5: Refinement
- Position monitoring (Stage 4)
- Advanced learning (A/B testing strategies)
- Daily reports
- Production hardening

---

## Decisions + Remaining Questions

### Decisions captured

- **Database:** Postgres (Supabase optional); SQLite only for tests/prototyping.
- **Dashboard:** FastAPI + HTMX for operational control.
- **Trading:** design for eventual live execution; default to paper mode.
- **Schwab OAuth:** already completed.

### Remaining questions

1. **Postgres hosting path (now):** do you want to start with local Docker Postgres, or immediately use Supabase?
2. **Live-trading safety workflow:** when `TRADING_MODE=live`, should we require manual confirmation in the UI for every order, or allow fully automated orders after a “session unlock”?
3. **Streamlit workbench:** do you want a Streamlit app in Phase 4/5 for offline analysis, or keep everything in the FastAPI dashboard?
