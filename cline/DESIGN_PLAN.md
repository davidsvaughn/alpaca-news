# Design Plan: LLM-Based Day-Trading Research Assistant

## Executive Summary

This is a **news-triggered, multi-LLM research pipeline** that watches for Alpaca news, filters signal from noise, investigates promising leads across multiple data sources (Schwab market data, web search, X/Twitter), and learns from experience.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PIPELINE ORCHESTRATOR                        │
│              (async Python - watchdog + asyncio event loop)          │
└───────────┬──────────────┬──────────────┬──────────────┬────────────┘
            │              │              │              │
      ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼──────┐
      │  STAGE 1   │ │  STAGE 2   │ │  STAGE 3   │ │  STAGE 4    │
      │  News      │ │  Deep      │ │  Sentiment │ │  Decision   │
      │  Triage    │ │  Research  │ │  + Price   │ │  + Monitor  │
      │  Filter    │ │  & Search  │ │  Analysis  │ │  Engine     │
      └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬───────┘
            │              │              │              │
      ┌─────▼──────────────▼──────────────▼──────────────▼────────┐
      │                    KNOWLEDGE STORE                         │
      │              (SQLite + JSON knowledge files)               │
      └───────────────────────────┬───────────────────────────────┘
                                  │
      ┌───────────────────────────▼───────────────────────────────┐
      │              WEB DASHBOARD (FastAPI + HTMX)               │
      │         monitoring, config, knowledge viewer               │
      └───────────────────────────────────────────────────────────┘
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
- **Explicit stages** (Triage → Research → Analysis → Decision)
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

### Stage 2: Deep Research & Web Search

**Purpose:** For articles that pass triage, gather comprehensive real-time context.

**Process (parallel fan-out):**

#### 2a. Web Search (OpenAI or Gemini):
```python
# Using OpenAI Responses API with web search
response = openai_client.responses.create(
    model="gpt-4o",
    tools=[{"type": "web_search"}],
    input=f"Find the latest real-time news and analysis about {symbols} "
          f"related to: {headline}. Focus on information from the last "
          f"1-12 hours. Evaluate source freshness and reliability."
)
```

#### 2b. Google Search Grounding (Gemini):
```python
# Using Gemini with Google Search grounding
response = gemini_client.models.generate_content(
    model='gemini-2.5-flash',
    contents=f'What is the current market sentiment and latest developments for {symbols}?',
    config=types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )
)
# Extract grounding metadata for source evaluation
sources = response.candidates[0].grounding_metadata.grounding_chunks
```

#### 2c. X/Twitter Search (Grok):
```python
# Using Grok with x_search via OpenAI-compatible API
grok_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
response = grok_client.responses.create(
    model="grok-4-1-fast",
    tools=[{"type": "x_search"}],
    input=f"Search X/Twitter for real-time discussion, breaking news, "
          f"and sentiment about ${symbols}. Focus on posts from the last "
          f"few hours. Look for: insider information hints, unusual volume "
          f"mentions, analyst reactions, institutional activity signals."
)
```

#### 2d. Source evaluation:
Cross-reference findings, check source domains against the learned "reliable sources" list, evaluate recency.

---

### Stage 3: Sentiment + Price Analysis

**Purpose:** Combine research findings with actual market data from Schwab.

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

3. **Synthesis prompt** — feed everything to a strong reasoning model:
   ```
   Given:
   - News item: {headline + summary}
   - Web research findings: {stage2a results}
   - Google search findings: {stage2b results}  
   - X/Twitter sentiment: {stage2c results}
   - Price history: {recent candles, key levels, volume}
   - Current price: {from stream}
   - Knowledge store context: {relevant learned patterns}
   
   Evaluate: Is there a high-probability short-term trade opportunity?
   Return: {opportunity: bool, direction: "long"|"short", confidence: 0-1,
            entry_price: float, target_price: float, stop_loss: float,
            time_horizon: "minutes"|"hours", reasoning: "..."}
   ```

---

### Stage 4: Decision + Position Monitoring

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

## 5. Knowledge Store (The Learning System)

This is the most important long-term differentiator. A **hybrid storage** approach:

### 5a. SQLite Database (`data/knowledge.db`)

Tables:
- **`trade_log`** — every hypothetical/real trade with entry, exit, P&L, reasoning
- **`news_log`** — every news item processed, triage decision, outcome
- **`api_costs`** — every LLM/API call with provider, model, tokens, cost
- **`search_log`** — every web/X search query, results quality rating

### 5b. JSON Knowledge Files (`data/knowledge/`)

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

### 5c. Learning Loop

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

The LLM proposes updates → stored as pending → auto-applied or human-reviewed (configurable).

---

## 6. Learning Mode

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
LEARNING_EXPLORE_RATE=0.3        # 30% of time, try alternative strategies
```

---

## 7. Cost Control & Monitoring

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

## 8. Web Dashboard (FastAPI + HTMX)

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
- **SQLite** for all persistent data (zero infrastructure)

---

## 9. Project Structure

```
alpaca-news/
├── alpaca/
│   └── news_websocket.py          # existing - news ingestion
├── schwab/
│   └── main.py                    # existing - schwab client example
├── trader/                        # NEW - main application
│   ├── __init__.py
│   ├── main.py                    # entry point - starts pipeline + dashboard
│   ├── config.py                  # loads .env, runtime config management
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # watches output/alpaca, dispatches stages
│   │   ├── triage.py              # Stage 1: news filtering
│   │   ├── research.py            # Stage 2: web/X search
│   │   ├── analysis.py            # Stage 3: sentiment + price analysis
│   │   ├── monitor.py             # Stage 4: position monitoring
│   │   └── prompts/               # all LLM prompts as .md files
│   │       ├── triage.md
│   │       ├── research_web.md
│   │       ├── research_x.md
│   │       ├── analysis.md
│   │       ├── decision.md
│   │       └── reflection.md
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
│   │   └── database.py            # SQLite connection management
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
├── data/                          # runtime data
│   └── knowledge.db               # SQLite database
├── output/
│   └── alpaca/                    # news articles (existing)
└── .env
```

---

## 10. Implementation Phases

### Phase 1: Foundation (Start here)
- Unified LLM client with OpenAI, Gemini, Grok support
- Cost tracker
- Config system (.env loading)
- Basic pipeline orchestrator (watchdog on output/alpaca/)
- Stage 1: News triage filter (working end-to-end)

### Phase 2: Research Pipeline
- Stage 2: Web search (OpenAI + Gemini + Grok x_search)
- Schwab client wrapper (price history + streaming)
- Stage 3: Sentiment + price analysis
- SQLite logging (news_log, api_costs)

### Phase 3: Knowledge & Learning
- Knowledge store (JSON files)
- Learning loop / reflection engine
- Trade logging with hypothetical P&L
- Learning mode

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

## Open Questions

1. **Database choice:** SQLite proposed for zero-infrastructure simplicity. Postgres is also available (psycopg2 in requirements.txt). Which do you prefer?

2. **Dashboard framework:** FastAPI + HTMX proposed (lightweight, async). Flask is already in the project. Preference?

3. **Real trades vs. paper only:** Should the system ever execute real trades through Schwab, or is this purely a research/recommendation tool?

4. **Schwab auth flow:** schwabdev requires OAuth — has the token setup flow been completed, or does the system need to handle that?

5. **Starting phase:** Ready to begin with Phase 1?
