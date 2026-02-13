# Roadmap: Implementation Status & Plan

> Living document — updated each session as work progresses.
> For stable architecture reference, see [ARCHITECTURE.md](ARCHITECTURE.md).
> For design rationale, see [DECISIONS.md](DECISIONS.md).
>
> Last updated: 2026-02-12

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
| **X API stream** | DONE | Filtered stream, burst mode, rules, LLM quality gate with auto-retry |
| **Database (SQLite)** | DONE | Snapshots + watches tables, idempotent inserts |
| **Dashboard (FastAPI + SSE + HTMX)** | DONE | Full monitoring + control UI (6 pages, charts, editor) |
| **Schwab market data** | DONE | Quotes, candles, streaming, options, fundamentals, movers, market hours |
| **Knowledge store** | PARTIAL | skip_patterns, reliable_sources, generic append_to_list; insights.json TODO |
| **Explorer (multi-agent pipeline)** | DONE | Sequential Grok→OpenAI→Gemini pipeline — wired into orchestrator |
| **FinnHub data integration** | DONE | Company news, earnings context (auto-fetch), analyst ratings (tool) |
| **Budget awareness** | DONE | Real-time token/request usage injected into tool results via ctx.usage |
| **Pipeline env var config** | DONE | PIPELINE_REQUEST_LIMIT, PIPELINE_TOOL_CALLS_LIMIT, PIPELINE_TOTAL_TOKENS_LIMIT |
| **yfinance data layer** | DONE | Free data: fundamentals, insider tx, price history, news, technicals |
| **BM25 situation memory** | TODO | New — learned from TradingAgents |
| **Schwab Tier 1 expansion** | DONE | Options IV, fundamentals, movers, market hours, enhanced context |
| **insights.json** | TODO | Flat scored insights for prompt injection |
| **Watch lifecycle** | DONE | Full lifecycle: model, DB, creation, monitoring scheduler, retrospective, sealing |
| **Signal extraction step** | DONE (by design) | Built into PydanticAI output_type=TradingSignal |
| **Reflection / Evaluation** | DONE | On-demand LLM evaluation, nested decision tree, Tier A/B insights |
| **Offline loop** | TODO | Labeling, hop scoring, automated reflection scheduling |
| **Bull/bear prompt pattern** | TODO | New — lightweight adversarial reasoning |
| **Data vendor fallback** | DONE | Schwab → yfinance fallback via MarketDataService |
| **Training-ready data capture** | DONE | Modality tags, rounds in snapshot, data_modalities index, richer x_search |
| **Contextual bandits** | TODO (Phase D+) | Online learning for orchestrator config |

---

## Phase A: Data layer expansion (yfinance + Schwab Tier 1) — DONE

1. `trader/market/yfinance_client.py` — insider activity, fundamentals, price history, news
2. `trader/market/indicators.py` — RSI, MACD, Bollinger Bands, etc. via stockstats
3. Schwab Tier 1: `check_options_activity()`, `get_fundamentals()`, `get_movers()`,
   enhanced `check_market_context()`
4. Vendor fallback: try Schwab → fall back to yfinance

## Phase B: Explorer revision (multi-agent pipeline) — DONE

1. **DONE** — Add `pydantic-ai` dependency
2. **DONE** — Create `trader/online/explorer_agent.py` (v1):
   TradingSignal, ExplorerDeps, TracingToolset, 11 market data function tools
3. **DONE** — Add research tools: x_search, url_fetch, x_stream_cache
4. **DONE** — Build multi-agent pipeline (`trader/online/agent_pipeline.py`):
   per-provider agent factory, sequential runner, optional loop, budget enforcement
5. **DONE** — Per-provider integration tests (Grok, OpenAI, Gemini)
6. **DONE** — Wire into orchestrator:
   replaced explore_two_phase with run_pipeline, added per-agent cost estimation
7. **DONE** — Enhance data capture for training readiness:
   TOOL_MODALITY map, modality tags, richer x_search, rounds in snapshot, data_modalities index
8. **(Future)** Create insights.json + BM25 situation memory

## Phase C: Watch lifecycle — DONE

Full position management from entry to retrospective.

1. **DONE** — Watch data model (`trader/models/watch.py`):
   - Watch, WatchEntry, WatchExit frozen dataclasses + WatchBuilder
   - `watches` table in SQLite with CRUD helpers
   - Config: `WATCH_ENABLED`, `WATCH_CONFIDENCE_THRESHOLD` (0.7), `MAX_CONCURRENT_WATCHES` (5),
     `WATCH_MONITORING_BUDGET`, `WATCH_MAX_HOLD_MINUTES` (240), `WATCH_CHECKIN_MODEL` (gemini-3-flash)
   - Orchestrator creates Watch after snapshot seal when confidence >= threshold
   - Virtual watches only — no real order placement

2. **DONE** — Watch monitoring scheduler + check-in agent (`trader/online/watcher.py`):
   - `WatchMonitor` class with time-based check-in schedule:
     lightweight (0-10m, 2m interval), medium (10-30m/5m, 30-60m/10m), full (60-240m, 15m), force_exit (240m+)
   - Lightweight check-in: price-only (no LLM), computes unrealized P&L, auto stop-loss at -10%
   - Agent check-in: single PydanticAI agent (configurable model via `WATCH_CHECKIN_MODEL`)
     with `CheckinDecision` structured output (hold/exit + reason + P&L)
   - Reuses `ExplorerDeps` + `market_toolset` from explorer_agent.py
   - `WatchBuilder.from_dict()` reconstitutes builder from stored watch dict
   - Daemon monitoring thread in orchestrator's `run_watch_loop()` (60s cycles)
   - 12 tests covering scheduling, timing, P&L, is_due logic, lightweight check-ins, WatchBuilder roundtrip

3. **DONE** — Exit logic, retrospective phase, and watch sealing:
   - `run_check_cycle()` now handles all active statuses: holding, exited, retrospective
   - Exited → retrospective transition: immediate, initializes `retrospective_data` with MFE/MAE tracking
   - Retrospective phase: lightweight price checks (5-15 min intervals), records post-exit price movement
   - Auto-seal after `WATCH_MAX_RETRO_MINUTES` (default 60), records final_price
   - Force exit hardened: agent "hold" override to "exit" when depth=="force_exit"
   - Full lifecycle test: holding → exited → retrospective → sealed via consecutive check cycles
   - 20 watcher tests total (8 new for retrospective + sealing)

## Phase D-1: Reflection & Evaluation — DONE

On-demand decision evaluation with nested decision tree and two-tier insights.

1. **DONE** — Fill data capture gaps:
   - Store triage decision in snapshot (`snapshot.py` + `orchestrator.py`)
   - Store system_prompt + user_message per agent round (`agent_pipeline.py`)
   - Store watch check-in history (`watch.py` + `watcher.py`)

2. **DONE** — Decision timeline / EvalRecord (`trader/reflection/eval_record.py`):
   - `build_eval_record(snapshot, watch)` → nested JSON tree
   - Each node: `{type, summary, detail, children}`
   - Node types: triage, agent_round, tool_call, prediction, watch_entry, watch_checkin, watch_exit, retrospective
   - `eval_record_to_markdown()` for LLM evaluator prompt

3. **DONE** — Pipeline Timeline UI on snapshot detail page:
   - Recursive Jinja macro (`_timeline_node.html`) renders nested Bootstrap accordions
   - Color-coded badges by node type, expandable details + prompts
   - Linked watch data (if any) included in timeline

4. **DONE** — Reflection page (`/reflection`):
   - Snapshot table with checkboxes, symbol filter, select all/none
   - "Evaluate Selected" button triggers LLM evaluation
   - Results partial shows per-snapshot grades, node assessments, insights

5. **DONE** — LLM evaluator (`trader/reflection/evaluator.py`):
   - Gemini-based (configurable via `REFLECTION_MODEL`)
   - Builds markdown timelines, sends to LLM, parses structured JSON response
   - Per-snapshot: grade (A-F), summary, per-node assessments
   - Insights: Tier A (auto-apply to knowledge) + Tier B (save to markdown)
   - Evaluations persisted to DB (`evaluations` table)

6. **DONE** — KnowledgeStore extensions:
   - Generic `append_to_list(filename, key, item)` for all knowledge files
   - Supports signal_patterns, anti_patterns, search_strategies, model_notes

## Phase D-1.5: Data enrichment & budget awareness — DONE

1. **DONE** — FinnHub free tier integration (`trader/market/finnhub_client.py`):
   - Company news auto-fetched into user message
   - Earnings context (surprises + calendar) auto-fetched into user message
   - `get_analyst_ratings` tool for on-demand recommendation trends
   - Premium endpoints (news sentiment, price targets, upgrades) confirmed 403 — skipped
2. **DONE** — Budget awareness via `ctx.usage`:
   - `TracingToolset.call_tool()` appends real-time budget summary to tool results
   - Shows tokens used/limit, requests used/limit, expensive tool counts
   - Agents self-regulate based on visible budget consumption
3. **DONE** — Pipeline limits as env vars:
   - `PIPELINE_REQUEST_LIMIT` (default 15), `PIPELINE_TOOL_CALLS_LIMIT` (25),
     `PIPELINE_TOTAL_TOKENS_LIMIT` (80,000) — wired into ExplorerDeps + UsageLimits
4. **DONE** — Article content inclusion:
   - HTML-stripped article body included in user message when available

## Phase D-2: Offline loop — TODO

Close the automated learning feedback loop.

1. Outcome labeler (attach +15m/+60m/+1d returns to Snapshots)
2. Hop scorer (per-trace and per-sequence value estimation)
3. Automated reflection scheduling (periodic batch evaluation)
4. Insight update pipeline (reinforce/weaken/create insights)
5. Memory update (store new situation/lesson tuples)

---

## Parked Ideas

### Stock selection optimization & finscore integration

Under budget constraints (max concurrent watches, daily cost caps), the system
must learn **which stocks deserve expensive exploration/monitoring**. Several
connected ideas:

1. **Learned stock selector from historical data.** Every sealed Watch produces
   an outcome label (P&L). Over time, train a lightweight selector to rank incoming
   news by expected return-on-investigation. Feeds from Phase D outcomes.

2. **Warm-up + pruning ("tournament bracket").** Start N > K watches at lightweight
   depth (price checks only, no LLM). After warm-up window (10-15 min), prune to
   top K based on early price action. Compatible with check-in depth levels and
   contextual bandits.

3. **Contextual bandits for allocation.** Context = news features + triage signal +
   market state + finscore output. Arm = allocate (depth level) or skip. Reward =
   Watch P&L. LinUCB or Thompson Sampling after ~100-200 labeled events.

4. **Legacy finscore model as cheap parallel signal.** Fine-tuned Llama-3.2-1B
   (`davidsvaughn/finscore-W4A16`, quantized, served via vLLM) produces:
   - **Type** (T0-T7): 8-class news categorization
   - **Signal** (0-10): Short-term signal potential score
   Integration: **record but don't act**. Call during triage, log alongside snapshot,
   evaluate correlation with Watch outcomes during reflection.

**Connection:** Finscore could be the cheap pre-filter that enables warm-up+prune.
Score all incoming news instantly, start watches on top N, prune using real-time data.

**Timeline:**
- **Now (data capture):** Already logging triage + exploration in snapshots.
- **Near-term (finscore):** Optional finscore call in triage, record in snapshot. ~50 LOC.
- **Medium-term (warm-up+prune):** After Phase C monitoring is complete.
- **Long-term (bandits/selector):** After ~200 labeled events.
