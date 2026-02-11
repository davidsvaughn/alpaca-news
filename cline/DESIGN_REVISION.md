# Design Revision: Free-Form Tool-Use Loop with Meta-Learning

## Background

This document captures a series of design discussions (Feb 2026) about how to evolve
the `trader/` system after the initial Phase 1/Phase 2 implementation.

Two new capabilities were requested (from `CHAT2.md` and `CHAT3.md`):

1. **Evidence acquisition / web scraping** — treat LLM web search as a *scout*, then
   fetch and extract the raw documents ourselves for auditability, training, and
   reproducibility.
2. **X API filtered stream** — add X (Twitter) as a real-time data source alongside
   Grok's `x_search` tool, with strict cost/usage guardrails.

Both have been implemented as optional, feature-flagged modules:
- `trader/evidence/` (fetch + extract + persist)
- `trader/xapi/` (client + rules + stream + usage)
- `trader/online/x_stream_service.py` (burst mode service)

While planning how to wire these into the exploration pipeline, a deeper question
emerged about the overall architecture.

---

## The Problem: Over-Structuring Constrains LLM Intelligence

The existing explorer (`trader/online/explorer.py`) uses a **finite action menu** and
**rigid two-phase structure**:

- Phase 1: pick from 8 predefined action templates → run them → generate hypotheses
- Phase 2: LLM ranks hypotheses → pick follow-up templates → run them → stop

This was designed for **learnability** (bandit-style weight updates over a finite action
space). But it has a fundamental problem:

> **It throws away the most valuable thing we're paying for: the LLM's ability to
> reason creatively about novel situations.**

A frontier model (GPT-5, Grok-4.1, Gemini-3) given tools and a clear objective can
often come up with searches and reasoning chains that no predefined template would
capture. The finite menu *prevents* that.

### The tension

| Approach | Pro | Con |
|---|---|---|
| **Structured** (finite menu, policy hooks) | Measurable, formally learnable, reproducible | Constrains LLM intelligence, brittle to novelty |
| **Free-form** (LLM decides everything) | Captures full model capability, adapts | Harder to formally learn from, less predictable |

### Resolution

**Use free-form tool use with prompt-level learning, not code-level policy.**

The reasoning:
1. Frontier LLMs are getting smarter faster than a bandit can learn from our data
2. We don't have enough data yet for statistical learning to beat LLM judgment
3. Prompt-injected knowledge compounds with model improvements (a better model
   uses the same knowledge more effectively)

---

## Revised Architecture: Tool-Use Reasoning Loop

### Core idea

Instead of "pick from a menu," give the LLM **callable tools** and let it reason
freely about what to do, with **budget guardrails enforced in code** and **learned
strategy knowledge injected as prompt context**.

### The tools (available to the LLM during exploration)

| Tool | Description | Cost model |
|---|---|---|
| `web_search(query)` | Search via OpenAI / Gemini / Grok | Per-call LLM + tool fee |
| `x_search(query)` | Search X via Grok | Per-call LLM + tool fee |
| `x_stream(keywords, minutes)` | Start a live X filtered stream burst | X API credits |
| `url_fetch(url)` | Fetch + extract full text of a web page | Free (compute only) |
| `check_price(symbol)` | Current price + recent candles | Free (Schwab) |
| `check_volume(symbol)` | Volume data + regime detection | Free (Schwab) |

The LLM decides *what queries to run*, *which tools to use*, *in what order*, and
*when to stop*. We don't constrain it to templates.

### What stays in code (hard guardrails)

These are **non-negotiable constraints** enforced programmatically, not by the LLM:

- Max budget per event (`MAX_COST_PER_NEWS_ITEM` — CostTracker)
- Max tool calls per event (`MAX_TOTAL_HOPS`)
- X API rate limits and daily quotas
- Never execute real trades without confirmation
- Budget kill switch at daily level

### What moves to prompts (soft guidelines)

Instead of learning "action weights" in Python, we inject learned knowledge as
**context in the exploration prompt**:

```markdown
## What we've learned works (from past experience)
- For rumor-type news, fetching the original source URL yourself (url_fetch)
  is 3× more predictive than relying on the LLM's web_search summary
- X stream bursts during market hours for large-cap tickers yield useful signal;
  afterhours they're usually empty
- When conflicting sources exist, url_fetch on each → compare yields much more
  reliable hypotheses than a single web_search
- Google search grounding rarely adds value when OpenAI web_search already found
  the primary source
- ...

## What tends to waste money
- Broad web searches for well-known companies rarely add new info
- X searches during afterhours have low volume for most tickers
- Doing a second web_search with slightly different keywords after x_search
  returned nothing is usually a dead end
- ...
```

The LLM reads these as *guidelines*, not hard constraints. It can ignore them when
the situation calls for it (e.g., a truly novel event that doesn't match past patterns).

---

## Two Layers of Learning

### Layer 1: Domain knowledge (what moves prices)

Already partially implemented in `data/knowledge/`:
- `signal_patterns.json` — patterns that preceded profitable trades
- `anti_patterns.json` — patterns that preceded losses
- `reliable_sources.json` — domains/URLs ranked by reliability
- `skip_patterns.json` — headlines/keywords to auto-skip

These answer: **"Given this news, is there likely a tradeable signal?"**

### Layer 2: Strategy knowledge (what exploration approaches work) — NEW

A new knowledge file, e.g. `data/knowledge/exploration_strategies.json`, that captures:

- Which tool sequences are productive for which types of news
- Which tools are redundant with each other under certain conditions
- Which tools are dead ends under certain conditions
- When url_fetch adds value over LLM summarization
- When X stream adds value over x_search (and vice versa)
- Effective query patterns (not rigid templates, but learned heuristics)

These answer: **"Given this news, how should I investigate it?"**

Both layers are injected into the exploration prompt. Both are updated by the offline
reflection loop.

---

## The Iterative Learning Pipeline

```
┌──────────────────────────────────────────────────┐
│ ONLINE: Process news event                        │
│                                                    │
│  System prompt includes:                           │
│    - Tool definitions                              │
│    - Domain knowledge (signal/anti patterns)       │
│    - Strategy knowledge (exploration strategies)   │
│    - Budget guardrails                             │
│                                                    │
│  LLM reasons freely, calls tools, stops when done │
│  → Sealed Snapshot with full ToolTrace chain       │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│ OFFLINE: Label outcomes + score traces             │
│                                                    │
│  For each Snapshot:                                │
│    1. Attach price outcomes (+15m, +60m, +1d)     │
│    2. Score each ToolTrace hop:                    │
│       - Did this hop add novel information?        │
│       - Did the prediction improve after it?       │
│       - What did it cost?                          │
│       - Was url_fetch better than LLM summary?    │
│    3. Score action SEQUENCES:                      │
│       - "web_search → url_fetch → compare"         │
│         worked well for rumor confirmation         │
│       - "x_search → x_stream_burst" was redundant │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│ OFFLINE: Reflection → update knowledge             │
│                                                    │
│  LLM reviews batch of scored Snapshots:            │
│    "Here are 50 recent Snapshots with outcomes     │
│     and per-hop scores. What patterns do you see?" │
│                                                    │
│  Output: proposed updates to BOTH:                 │
│    - Domain knowledge (signal_patterns, etc.)      │
│    - Strategy knowledge (exploration_strategies)   │
│                                                    │
│  Human review → merge into knowledge store         │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
            (next event uses updated knowledge)
```

### What "scoring action sequences" means concretely

In each Snapshot, tool traces are ordered. The offline scorer identifies patterns:

```
Snapshot #142 (outcome: +2.3% in 60m, prediction correct)
  hop1: web_search("NVDA supply shortage") → found 3 URLs       cost: $0.04
  hop2: url_fetch(reuters_url) → full article text               cost: ~$0
  hop3: url_fetch(bloomberg_url) → full article, confirms        cost: ~$0
  hop4: x_search("NVDA shortage channel checks") → 2 posts      cost: $0.03
  → SEQUENCE: web_search → url_fetch × 2 → x_search
  → VALUE: high (correct prediction, strong evidence chain)

Snapshot #143 (outcome: -0.1% in 60m, prediction wrong)
  hop1: web_search("AAPL dividend announcement") → found URLs    cost: $0.04
  hop2: x_search("AAPL dividend") → nothing useful               cost: $0.03
  hop3: web_search("AAPL dividend history") → rehashed info      cost: $0.04
  → SEQUENCE: web_search → x_search → web_search (redundant)
  → VALUE: low (wrong prediction, redundant searches, wasted $)
```

The reflection LLM can then produce insights like:
> "Pattern: when initial web_search finds conflicting sources, url_fetch on each
> source followed by comparison yields high-value evidence."
>
> "Anti-pattern: doing a second web_search with slightly different keywords after
> x_search returned nothing — this is usually a dead end."

These go into `exploration_strategies.json` and get injected into future prompts.

---

## What Changes in the Codebase

### Keep (already built, still valuable)
- **Snapshot + SnapshotBuilder** — immutable learning artifacts with ToolTraces
- **CostTracker** — per-call + per-tool cost tracking, budget enforcement
- **Evidence layer** (`trader/evidence/`) — fetch, extract, persist
- **X API layer** (`trader/xapi/`, `x_stream_service.py`) — stream, rules, usage
- **Knowledge store** (`trader/knowledge/`) — JSON knowledge files
- **Triage** (`trader/online/triage.py`) — pre-filter + LLM triage
- **Dashboard + SSE** (`trader/web/`) — monitoring
- **Database** (`trader/db/`) — SQLite persistence

### Simplify
- **`explorer.py`** → replace rigid two-phase template selection with a free-form
  tool-use reasoning loop. The LLM calls tools via the Responses API (OpenAI/Grok)
  or equivalent, and the loop records each call as a ToolTrace.
- **`actions.py`** → finite action menu becomes less important. Tool *definitions*
  (what tools exist and how to call them) replace it. The menu can remain as
  documentation / reference, but it no longer gates what the LLM can do.

### Add
- **`data/knowledge/exploration_strategies.json`** — strategy knowledge file
- **Offline hop scorer** — evaluates per-trace and per-sequence value
- **Strategy reflection prompt** — generates exploration_strategies updates
  (extends the existing reflection concept from DESIGN_PLAN.md §9c)

### Drop (or deprioritize)
- **Formal policy hook** (the LLM *is* the policy)
- **Bandit-style weight learning** over action templates (replaced by prompt-level
  knowledge injection)
- **Rigid Phase1/Phase2 boundary** (the LLM decides its own exploration phases)

---

## The Explorer Prompt (Sketch)

```markdown
You are a financial research assistant investigating a breaking news event.

## Tools available
- web_search(query): Search the web. Returns summaries + source URLs.
- x_search(query): Search X/Twitter posts via Grok. Returns posts + metadata.
- x_stream(keywords, minutes): Start a live X stream for the given keywords.
  Returns buffered posts after the stream completes.
- url_fetch(url): Fetch and extract the full text of a web page. Use this when
  you need the actual article content rather than a search summary.
- check_price(symbol): Get current price, bid/ask, volume, and recent 1-min candles.
- check_volume(symbol): Get volume data and detect abnormal activity.

## Exploration strategies (learned from experience)
{inject exploration_strategies.json contents here}

## Market knowledge (learned from experience)
{inject signal_patterns.json, anti_patterns.json, reliable_sources.json}

## Budget for this event
- Remaining cost: ${budget_remaining}
- Remaining tool calls: {remaining_calls}
- Stop when you have enough evidence OR when further exploration isn't worth the cost.

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

Think step by step. Use tools as needed. You may use multiple tools, revisit
sources, or stop early if the signal is clearly noise. Explain your reasoning
at each step.
```

---

## Implementation Plan (When Ready)

1. **Simplify `explorer.py`** → free-form tool-use loop
   - LLM calls tools via Responses API or function-calling
   - Each tool call recorded as a ToolTrace
   - Budget guardrails enforced after each call
   - Loop ends when LLM says "done" or budget exhausted

2. **Add `exploration_strategies.json`** to knowledge store
   - Initially seeded with common-sense strategies
   - Updated by offline reflection

3. **Update exploration prompt**
   - Inject both domain + strategy knowledge
   - Provide tool definitions
   - Let LLM reason freely

4. **Add offline hop scorer** (future, for learning)
   - Per-trace: did this hop add novel info? What was the cost/benefit?
   - Per-sequence: which chains of tools produced good outcomes?

5. **Add strategy reflection prompt** (future, for learning)
   - Reviews scored Snapshots in batches
   - Proposes updates to exploration_strategies.json
   - Human-in-the-loop review before merging

---

## Open Questions

1. **Multi-provider tool routing**: When the LLM wants `web_search`, should we always
   use the same provider, or let the LLM (or a heuristic) choose between
   OpenAI/Gemini/Grok based on the query type?

2. **Tool-use API mechanics**: Should we use OpenAI's Responses API with function
   definitions, or implement our own tool-call loop (LLM outputs structured
   "I want to call X" → we execute → feed result back)?

3. **How to handle `x_stream`**: X stream is inherently async (runs for N minutes).
   Should the LLM be able to "start a stream and continue investigating" (parallel),
   or does it block until the stream completes?

4. **Triage integration**: Does triage stay as a separate Stage 1 (cheap/fast filter
   before the expensive exploration loop), or does it merge into the free-form
   loop (the LLM's first "action" could be to skip)?

5. **Snapshot structure**: ToolTraces still work well for recording each hop. Do we
   need any schema changes to accommodate free-form tool use (e.g., the LLM might
   use tools in unexpected orders or call the same tool multiple times)?
