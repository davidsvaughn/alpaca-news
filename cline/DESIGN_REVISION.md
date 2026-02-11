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

#### Web / social research tools

| Tool | Description | Cost model |
|---|---|---|
| `web_search(query)` | Search via OpenAI / Gemini / Grok | Per-call LLM + tool fee |
| `x_search(query)` | Search X via Grok | Per-call LLM + tool fee |
| `x_stream(keywords, minutes)` | Start a live X filtered stream burst | X API credits |
| `url_fetch(url)` | Fetch + extract full text of a web page | Free (compute only) |

#### Financial data tools (via Schwab — all free, no LLM cost)

| Tool | Description | What it reveals |
|---|---|---|
| `check_price(symbol)` | Real-time quote + recent 1-min candles | Current price, bid/ask, volume, recent trend |
| `check_options_activity(symbol)` | ATM options IV, put/call ratio, unusual activity | Market's *expected* move size; hedge fund positioning |
| `get_fundamentals(symbol)` | Market cap, P/E, EPS, sector, 52-week range, dividend yield | Size/context of the company; sensitivity to news |
| `get_movers(index)` | Top movers by % change or volume for $DJI/$SPX/NASDAQ | What else is moving right now; sector-wide vs idiosyncratic |
| `check_market_context()` | Market hours, SPY, VIX, /ES futures, broad market conditions | Is market open? Overall risk-on/off environment |
| `get_price_history(symbol, period, frequency)` | Historical candles (1-min to monthly, any range) | Longer-term context, support/resistance, trend |
| `get_options_chain(symbol, ...)` | Full chain with Greeks (delta, gamma, theta, vega, IV) | Detailed options analysis when warranted |
| `check_order_book(symbol)` | Level 2 bid/ask depth (NYSE/NASDAQ book) | Institutional order stacking, supply/demand imbalance |

The LLM decides *what queries to run*, *which tools to use*, *in what order*, and
*when to stop*. We don't constrain it to templates.

### The full financial data surface (Schwab via schwabdev)

The `schwabdev` library provides access to far more data than just price and volume.
Rather than pre-deciding which data matters, we **describe the full buffet** in the
exploration prompt and let the LLM request what it needs.

#### What schwabdev exposes (REST API)

| schwabdev method | Data | Why it matters for news-driven research |
|---|---|---|
| `quote()` / `quotes()` | Real-time bid/ask/last/volume/mark | Core price data |
| `price_history()` | Historical OHLCV candles (1-min → monthly) | Trend, support/resistance, context |
| `option_chains()` | Full options chain with all Greeks + IV | IV = market's expected move; the single best "is this news real?" signal |
| `option_expiration_chain()` | Available expiration dates | When are options concentrated? |
| `movers()` | Top % gainers/losers, volume leaders by index | "Is the whole sector moving, or just this stock?" |
| `instruments()` with `projection="fundamental"` | P/E, EPS, market cap, sector, 52-week range, dividend yield | Company context; how sensitive is this stock to this type of news? |
| `market_hours()` | Exact session times (pre/regular/post, holidays) | Proper "is market open?" instead of rough EST offset |

#### What schwabdev streams (WebSocket)

| Stream type | Data | Why it matters |
|---|---|---|
| `level_one_equities` | Real-time L1 quotes | What we use now |
| `nyse_book` / `nasdaq_book` | Level 2 order book depth | Institutional order stacking, supply/demand imbalance |
| `options_book` | Options order book | Unusual options activity in real time |
| `chart_equity` | Streaming chart candles | Live charting data |
| `chart_futures` | Futures streaming (/ES, /NQ, etc.) | More liquid than SPY after hours; better gauge of where market is headed |
| `screener_equity` | Real-time screener (e.g. NASDAQ_VOLUME_30) | Biggest movers right now across the market |
| `screener_options` | Options activity screener | Unusual options flow across the market |

#### Currently wrapped vs. not yet wrapped

| Status | Tools |
|---|---|
| ✅ Wrapped | `quote`, `quotes`, `price_history` (1-min), `level_one_equities` stream |
| ❌ Not yet | `option_chains`, `movers`, `instruments/fundamental`, `market_hours`, L2 books, futures, screeners |

#### Tiered implementation approach

**Tier 1 (build first — high value, frequently needed):**
- `check_options_activity(symbol)` — ATM IV + put/call ratio. This is probably the
  single most informative data point we're missing. The options market often "knows"
  before the stock moves.
- `get_fundamentals(symbol)` — one API call gives crucial company context.
- `get_movers(index)` — sector/market-wide context.
- Enhanced `check_market_context()` — proper market hours + /ES futures.

**Tier 2 (build on demand — valuable but situational):**
- `get_options_chain(symbol, ...)` — full chain for detailed analysis.
- `get_price_history(symbol, period, frequency)` — flexible candle periods.
- `check_order_book(symbol)` — L2 depth.

**Tier 3 (describe in prompt, build when requested):**
- Futures streaming, options screener, etc.
- If the LLM requests data we haven't wrapped yet, that's a signal to add it.

#### Baked-in data discovery

The exploration prompt should describe the **full data surface** — not just the
pre-built tools — so the LLM knows what to ask for. This means including a
"Financial data available" block that lists everything accessible via Schwab, even
if we haven't built a dedicated tool wrapper yet.

When the LLM requests something we haven't built (e.g., "I want to see the options
chain for NVDA"), the system can either:
- Route it through a generic `schwab_query(method, params)` pass-through tool
- Log the request so we know to build a dedicated tool
- Both

This "describe first, build on demand" approach avoids over-engineering tools nobody
uses while ensuring the LLM is aware of the full capability.

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

## Knowledge Model: Flat Insights with Simple Scoring

### Why not categorize knowledge up front?

An earlier draft split knowledge into "Layer 1: domain knowledge" (what moves prices)
and "Layer 2: strategy knowledge" (what exploration approaches work). But real insights
are deeply intertwined:

> "When a rumor about NVDA supply constraints surfaces on X from semiconductor
> insiders, fetching the original source URL and comparing it against the Reuters
> wire is the most reliable confirmation path — and confirmed rumors at this level
> typically move the stock 1-3% within an hour."

That's *simultaneously* domain knowledge, strategy knowledge, and source knowledge.
Forcing it into predefined categories loses the connections and over-structures things
before we know what shape the knowledge actually takes.

### The approach: one flat insights collection

**Operational knowledge files stay as-is** — `skip_patterns.json` is mechanically
used by the pre-filter to avoid LLM calls, so it has a specific code purpose. Same
for `reliable_sources.json` if we use it for source ranking. These are tools, not
"knowledge categories."

**All new learning goes into a single file**: `data/knowledge/insights.json` — a flat
list of insight objects. Each insight can be about anything: markets, tools, strategies,
sources, or (most likely) some blend of all of these.

### Insight schema

```json
{
  "id": "ins_001",
  "text": "When initial web_search finds conflicting sources about a supply chain rumor, url_fetch on each source followed by comparison yields much more reliable evidence than a second web_search",
  "score": 3,
  "created": "2026-02-15",
  "last_touched": "2026-02-20",
  "source_snapshots": ["snap_142", "snap_167", "snap_201"]
}
```

- **`text`**: free-form insight. Can blend market knowledge, tool strategy, source
  reliability — whatever the reflection loop found useful.
- **`score`**: starts at 1 when first created. The offline reflection loop bumps +1
  when new evidence reinforces the insight, -1 when new evidence contradicts it.
  That's the entire scoring mechanism.
- **`last_touched`**: when the score was last changed. Stale insights (not touched in
  a long time) are candidates for re-testing, but we don't build formal re-testing
  machinery — we just mention staleness in the prompt so the LLM can occasionally
  act against an old guideline to check if it still holds.
- **`source_snapshots`**: optional breadcrumbs for traceability.

### How insights are used at prompt time

When building the exploration prompt:
1. Load all insights from `insights.json`
2. Filter out insights with `score ≤ 0` (contradicted more than confirmed)
3. Sort by score descending (strongest insights first)
4. Cap at top N to keep the prompt reasonable
5. Inject as a "Lessons from experience" block

### How insights evolve over time

The offline reflection loop (run periodically over batches of scored Snapshots):
1. For each existing insight: does recent evidence support it (+1) or contradict it (-1)?
2. Any new patterns? → add new insight with score=1
3. Insights that drop to score ≤ 0 stop being injected (but aren't deleted — they
   could recover if new evidence supports them later)

Insights are **not written in stone**. They're more like "anecdotal guidelines with a
running tally." Good ones float to the top; bad ones sink. The LLM can always choose
to ignore them when the situation warrants it.

### How knowledge organization itself can evolve

We deliberately avoid imposing a taxonomy now. If a natural structure emerges after
hundreds of snapshots (e.g., certain clusters of insights clearly relate to specific
sectors, or to specific tool combinations), the reflection loop can *itself* propose
how to reorganize — that's just another kind of meta-learning. But we don't pre-build
categories we might never need.

---

## The Iterative Learning Pipeline

```
┌──────────────────────────────────────────────────┐
│ ONLINE: Process news event                        │
│                                                    │
│  System prompt includes:                           │
│    - Tool definitions                              │
│    - Learned insights (sorted by score)            │
│    - Operational knowledge (skip patterns, etc.)   │
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
│ OFFLINE: Reflection → update insights              │
│                                                    │
│  LLM reviews batch of scored Snapshots:            │
│    "Here are 50 recent Snapshots with outcomes     │
│     and per-hop scores. What patterns do you see?" │
│                                                    │
│  Output:                                           │
│    - Reinforce existing insights (+1 score)        │
│    - Weaken contradicted insights (-1 score)       │
│    - Propose new insights (score=1)                │
│    - Optionally update operational files           │
│      (skip_patterns, reliable_sources, etc.)       │
│                                                    │
│  Human review → merge into knowledge store         │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
            (next event uses updated insights)
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

These become new entries in `insights.json` (score=1) and get injected into future
prompts. If they keep being reinforced by subsequent Snapshots, their score rises
and they become more prominent in the prompt context.

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
- **`data/knowledge/insights.json`** — flat, scored insights collection (see
  "Knowledge Model" section above)
- **Offline hop scorer** (future) — evaluates per-trace and per-sequence value
- **Reflection prompt** (future) — reviews scored Snapshots, proposes new insights
  and score updates to existing ones (extends DESIGN_PLAN.md §9c)

### Drop (or deprioritize)
- **Formal policy hook** (the LLM *is* the policy)
- **Bandit-style weight learning** over action templates (replaced by prompt-level
  knowledge injection)
- **Rigid Phase1/Phase2 boundary** (the LLM decides its own exploration phases)

---

## The Explorer Prompt (Sketch)

```markdown
You are a financial research assistant investigating a breaking news event.

## Web / social research tools
- web_search(query): Search the web. Returns summaries + source URLs.
- x_search(query): Search X/Twitter posts via Grok. Returns posts + metadata.
- x_stream(keywords, minutes): Start a live X stream for the given keywords.
  Returns buffered posts after the stream completes.
- url_fetch(url): Fetch and extract the full text of a web page. Use this when
  you need the actual article content rather than a search summary.

## Financial data tools (free — no LLM cost)
- check_price(symbol): Real-time quote (bid/ask/last/volume/mark) + recent
  1-min candles.
- check_options_activity(symbol): ATM implied volatility, put/call volume ratio,
  and unusual options activity. IV is the market's expected move — one of the
  strongest signals for whether news is being priced in.
- get_fundamentals(symbol): Market cap, P/E, EPS, sector, 52-week range,
  dividend yield. Essential context for assessing news sensitivity.
- get_movers(index): Top gainers/losers and volume leaders for $DJI, $SPX, or
  NASDAQ right now. Reveals whether a move is stock-specific or sector-wide.
- check_market_context(): Market session (pre/regular/post), SPY, VIX, /ES
  futures. Overall risk-on/risk-off environment.
- get_price_history(symbol, period, frequency): Historical candles at any
  granularity (1-min to monthly). For longer-term context, trends,
  support/resistance.
- get_options_chain(symbol, contractType, strikeCount, range): Full options chain
  with all Greeks (delta, gamma, theta, vega, IV, open interest). Use when you
  need detailed options analysis.
- check_order_book(symbol): Level 2 bid/ask depth. Shows institutional order
  stacking and supply/demand imbalance.

## Additional financial data available (not yet dedicated tools)
The system can also access via Schwab:
- S&P 500 futures (/ES) and NASDAQ futures (/NQ) streaming
- Real-time equity and options screeners (biggest movers across the market)
- Options expiration dates for any symbol
- NYSE and NASDAQ order book streaming
If you need any of this data, describe what you want and the system will
attempt to fetch it.

## Lessons from experience (scored insights, strongest first)
{inject top-N insights from insights.json here, filtered to score > 0}

Note: some of these insights may be stale (not tested recently). If the
situation warrants it, you may act against a guideline to test whether it
still holds.

## Budget for this event
- Remaining cost: ${budget_remaining}
- Remaining tool calls: {remaining_calls}
- Financial data tools are free and don't count against your cost budget.
- Stop when you have enough evidence OR when further exploration isn't worth
  the cost.

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

1. **Expand Schwab wrapper** (`trader/market/schwab_client.py`) — Tier 1 tools
   - `check_options_activity(symbol)` — wraps `option_chains()`, extracts ATM IV +
     put/call ratio + unusual activity signals
   - `get_fundamentals(symbol)` — wraps `instruments(symbol, projection="fundamental")`
   - `get_movers(index)` — wraps `movers()` for $DJI/$SPX/NASDAQ
   - Enhanced `check_market_context()` — add `market_hours()` + /ES futures quote

2. **Simplify `explorer.py`** → free-form tool-use loop
   - LLM calls tools via Responses API or function-calling
   - Each tool call recorded as a ToolTrace
   - Budget guardrails enforced after each call (financial data tools are free)
   - Loop ends when LLM says "done" or budget exhausted

3. **Create `data/knowledge/insights.json`**
   - Start empty (or seed with a few common-sense insights)
   - Updated by offline reflection loop over time

4. **Update exploration prompt**
   - Inject top-N scored insights from `insights.json`
   - Provide full tool definitions (web + financial data + evidence + X)
   - Describe additional available data (Tier 3) for discovery
   - Let LLM reason freely

5. **Add Tier 2 Schwab tools on demand**
   - `get_options_chain(symbol, ...)` — full chain when detailed analysis warranted
   - `get_price_history(symbol, period, frequency)` — flexible candle periods
   - `check_order_book(symbol)` — L2 depth
   - Build these when the LLM consistently requests them (or when we see value
     in Snapshots)

6. **Add offline hop scorer** (future, for learning)
   - Per-trace: did this hop add novel info? What was the cost/benefit?
   - Per-sequence: which chains of tools produced good outcomes?

7. **Add reflection prompt** (future, for learning)
   - Reviews scored Snapshots in batches
   - Proposes new insights (score=1) and score adjustments to existing ones
   - Optionally updates operational files (skip_patterns, etc.)
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
