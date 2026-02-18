# Roadmap: Implementation Status & Plan

> Living document — updated each session as work progresses.
> For stable architecture reference, see [ARCHITECTURE.md](ARCHITECTURE.md).
> For design rationale, see [DECISIONS.md](DECISIONS.md).
>
> Last updated: 2026-02-17

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
| **Database (SQLite)** | DONE | Snapshots + watches + follow_ups tables, idempotent inserts |
| **Dashboard (FastAPI + SSE + HTMX)** | DONE | Full monitoring + control UI (6 pages, charts, editor) |
| **Schwab market data** | DONE | Quotes, candles, streaming, options, fundamentals, movers, market hours |
| **Knowledge store** | PARTIAL | skip_patterns, reliable_sources, generic append_to_list; insights.json TODO |
| **Explorer (multi-agent pipeline)** | DONE | Sequential Grok→OpenAI→Gemini; native SDK runners; pre-fetched market data; tool call ledger; graceful degradation |
| **Native SDK migration** | DONE | Replaced PydanticAI with native SDKs (openai, google-genai) for pipeline runners; server-side x_search; Gemini gets all function tools |
| **FinnHub data integration** | DONE | Company news, earnings context (auto-fetch), analyst ratings (tool) |
| **Budget awareness** | DONE | Output tokens as primary budget (PIPELINE_OUTPUT_TOKENS_LIMIT); real-time usage in tool results |
| **Pipeline env var config** | DONE | PIPELINE_REQUEST_LIMIT, PIPELINE_TOOL_CALLS_LIMIT, PIPELINE_OUTPUT_TOKENS_LIMIT |
| **Graceful degradation** | DONE | Agent failures preserved as partial rounds; pipeline continues; snapshot always sealed |
| **Pre-fetched market data** | DONE | Basic data fetched once for primary symbols; agents focus on investigation |
| **Gemini web search + function tools** | DONE | Gemini uses Google Search grounding AND function tools simultaneously (enabled by native SDK) |
| **Reasoning control** | DONE | Per-provider reasoning effort + thinking summaries + reasoning token capture |
| **yfinance data layer** | DONE | Free data: fundamentals, insider tx, price history, news, technicals |
| **BM25 situation memory** | TODO | New — learned from TradingAgents |
| **Schwab Tier 1 expansion** | DONE | Options IV, fundamentals, movers, market hours, enhanced context |
| **insights.json** | TODO | Flat scored insights for prompt injection |
| **Watch lifecycle** | DONE | Full lifecycle: model, DB, creation, monitoring scheduler, retrospective, sealing |
| **Signal extraction step** | DONE | JSON-in-prompt for native runners; PydanticAI output_type for fallback |
| **Reflection / Evaluation** | DONE | On-demand LLM evaluation, nested decision tree, Tier A/B insights |
| **Follow-up data collection** | DONE | Scheduled post-event data collection (no-buy + post-exit), LLM query planner, query effectiveness tracking |
| **Dashboard activity panel** | DONE | Real-time activity tracking (backfill, exploration, follow-ups), in-flight cost visibility, 10s HTMX refresh |
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

## Phase B-2: Pipeline resilience & efficiency — DONE

Eliminate redundant tool calls, survive agent failures, and control costs.

1. **DONE** — Graceful degradation (`agent_pipeline.py`, `orchestrator.py`):
   - `agent.run()` wrapped in try/except for `UsageLimitExceeded` / `AgentRunError`
   - Partial tool traces survive (on `deps` object); partial round records created
   - Pipeline continues to next agent on failure; `PipelineResult.signal` is optional
   - Orchestrator wraps `run_pipeline()` in try/except; always seals snapshot
2. **DONE** — Pre-fetch basic market data (`explorer_agent.py`):
   - `_prefetch_market_data()` fetches price, fundamentals, technicals, options,
     volume, insider, news, price history ONCE for primary symbols
   - Injected into user message via `_build_user_message(market=...)`
   - Agents guided to investigate (web_search, x_search) rather than re-fetch
3. **DONE** — Updated system prompts (`agent_pipeline.py`):
   - Agents told "Background data is ALREADY provided" — do NOT re-fetch
   - Guided to use web_search, x_search, url_fetch for investigation
4. **DONE** — Tool call ledger (`agent_pipeline.py`):
   - `_format_tool_ledger()` passes full investigative tool results downstream
   - Skips pre-fetched data tools; caps url_fetch at 3000 chars
   - Downstream agents see what was already found, avoiding redundant calls
5. **DONE** — Output tokens as primary budget control:
   - `PIPELINE_OUTPUT_TOKENS_LIMIT` (default 50k) replaces total_tokens as primary limit
   - Output tokens cost 3-4x more; input tokens now "free" for richer context
   - `_budget_summary()` shows output tokens used/limit
6. **DONE** — Gemini → Google grounding web search:
   - Gemini uses `WebSearchTool()` (Google grounding) instead of function tools
   - With pre-fetched data + tool ledger, Gemini doesn't need function tools
   - `function_tools=False` flag on AgentSpec; TracingToolset conditionally skipped

## Phase C-2: Follow-up Data Collection — DONE

Scheduled post-event data collection for both no-buy and post-exit cases.

1. **DONE** — FollowUp data model (`trader/models/follow_up.py`):
   - `FollowUp`, `FollowUpCollection` frozen dataclasses + `FollowUpBuilder`
   - `parse_offset_to_minutes()` for schedule parsing (+1h → 60, +1d → 1440)
   - Builder pattern with `from_dict()` roundtrip, `next_offset_label()`, `complete()`

2. **DONE** — Database (`trader/db/database.py`):
   - `follow_ups` table (follow_up_id, snapshot_id, symbols, reason, status, follow_up_json)
   - 7 CRUD functions: insert, update, get, get_active, get_by_snapshot, get_all, count_active

3. **DONE** — Config (`trader/config.py`):
   - 8 `follow_up_*` settings: enabled, schedule, web_searches, x_searches, max_cost,
     max_concurrent, collector_interval_s, planner_model

4. **DONE** — FollowUpCollector (`trader/online/follow_up_collector.py`):
   - Two-phase collection: LLM query planner (gemini-3-flash) → mechanical data gathering
   - Query planner uses PydanticAI Agent with `QueryPlan` structured output
   - Mechanical: price + news (free) + web_search + x_search (direct httpx to Grok API)
   - Query effectiveness tracking: quality ratings fed back to planner for subsequent collections
   - Fallback template queries if LLM planner fails
   - Daemon thread (`collector_loop`) alongside WatchMonitor

5. **DONE** — Orchestrator integration (`trader/online/orchestrator.py`):
   - No-buy follow-up created after snapshot seal when no Watch was created
   - Collector daemon thread started in `run_watch_loop()`

6. **DONE** — Watcher integration (`trader/online/watcher.py`):
   - Post-exit follow-up created in `_seal_watch()` after Watch lifecycle completes

7. **DONE** — Web API (`trader/web/app.py`):
   - Export endpoint includes follow_ups: `{snapshot, watch, follow_ups}`
   - Snapshot detail page passes follow_ups to eval_record
   - New `/api/follow-ups` endpoint with status filter

8. **DONE** — Eval record (`trader/reflection/eval_record.py`):
   - `build_eval_record()` accepts `follow_ups` parameter
   - Follow-up nodes with collection children in decision tree

9. **DONE** — 18 new tests (`tests/test_follow_up.py`):
   - Model (parse_offset, builder, roundtrip), DB CRUD, eval_record integration

## Dashboard Activity Panel — DONE

Real-time visibility into all in-flight operations with cost percolation.

1. **DONE** — ActivityTracker (`trader/online/activity_tracker.py`):
   - Thread-safe `Activity` dataclass + `ActivityTracker` class
   - `start()`, `update()`, `finish()` lifecycle; `get_inflight_cost()` for live cost

2. **DONE** — Orchestrator integration (`trader/online/orchestrator.py`):
   - Exploration activities: triage → agent N/M → sealing → finish
   - Real-time cost updates from `CostTracker.item_spent`

3. **DONE** — Backfill tracking (`trader/main.py`):
   - Backfill activity with progress "X/N" items
   - Bus events: `backfill_started`, `backfill_progress`, `backfill_complete`

4. **DONE** — Follow-up collector integration (`trader/online/follow_up_collector.py`):
   - Activity per collection run (start/finish)

5. **DONE** — Dashboard UI:
   - `/api/activity-panel` HTMX endpoint (10s refresh + SSE triggers)
   - Categorized display: Backfill, Exploring, Follow-up Collection, Scheduled Follow-ups
   - Stats cards show `sealed + inflight` cost with breakdown
   - New SSE events registered for immediate refresh

6. **DONE** — 10 unit tests (`tests/test_activity_tracker.py`):
   - Lifecycle, cost tracking, concurrent access safety

## Phase B-3: Reasoning control & prompt refinement — DONE

Reasoning effort control, thinking token capture, and Gemini prompt fix.

1. **DONE** — Fix Gemini system prompt (`agent_pipeline.py`):
   - Investigation instructions (url_fetch, market data tools, x_search cost) gated on `spec.function_tools`
   - Gemini gets synthesis-focused guidance instead of tool-use instructions it can't follow
   - Cost awareness section also gated (only shown to agents with function tools)

2. **DONE** — Reasoning control via `model_settings` (`agent_pipeline.py`, `config.py`):
   - `AgentSpec.model_settings` field carries provider-specific settings to `agent.run()`
   - OpenAI: `reasoning_effort` (env `OPENAI_REASONING_EFFORT`, default `medium`) + `reasoning_summary='detailed'`
   - Gemini: `thinking_config` with `include_thoughts` and configurable level (env `GEMINI_THINKING_LEVEL`, default `dynamic`)
   - Grok: Skipped (grok-4 always max reasoning, no effort control)

3. **DONE** — Reasoning token & thinking summary extraction (`agent_pipeline.py`):
   - `reasoning_tokens` extracted from `usage.details` (OpenAI: `reasoning_tokens`, Gemini: `thoughts_tokens`)
   - `ThinkingPart` content extracted from model response messages via `_extract_thinking_content()`
   - Round records include `usage.reasoning_tokens`, `usage.details`, and `thinking_summary`
   - Total pipeline usage accumulates reasoning tokens

4. **DONE** — Improved builtin tool traces (`agent_pipeline.py`):
   - `None` output → descriptive `"[server-side grounding — results not exposed by provider]"`
   - Clarifying comments on cost (baked into provider's token cost)

5. **DONE** — Snapshot detail UI (`snapshot_detail.html`):
   - Summary table shows reasoning tokens inline with warning color
   - Per-agent accordion badge shows reasoning token count
   - Collapsible "Reasoning Summary" section when thinking content available

## Phase B-4: Native SDK migration — DONE

Replace PydanticAI agent framework with native SDKs for the exploration pipeline.
Motivated by: excessive search calls, Gemini unable to use function tools,
x_search overhead from inner API calls, poor cost visibility.

1. **DONE** — Extract shared types (`trader/online/agent_common.py`):
   - `AgentRunResult` dataclass (universal runner return type)
   - `ToolDef` dataclass (name, func, description, JSON schema, modality)
   - `build_trace_dict()` helper (common trace format for all runners)
   - `TOOL_MODALITY` dict (maps tool names to data categories)

2. **DONE** — Extract pure tool functions (`trader/online/tool_core.py`):
   - 16 pure functions (no PydanticAI RunContext dependency)
   - `TOOL_REGISTRY: list[ToolDef]` — single source of truth for tool definitions
   - `TOOL_BY_NAME: dict[str, ToolDef]` — quick lookup
   - `explorer_agent.py` @tool decorators now delegate to tool_core

3. **DONE** — Grok runner (`trader/online/runners/grok_runner.py`):
   - `openai` SDK pointed at `https://api.x.ai/v1/`
   - Server-side `x_search` + `web_search` (no inner API calls — major latency win)
   - Tool-calling loop with `previous_response_id` chaining
   - Extracts reasoning summaries from `reasoning` output items

4. **DONE** — OpenAI runner (`trader/online/runners/openai_runner.py`):
   - `openai` SDK Responses API with `previous_response_id`
   - Server-side `web_search` + all 16 function tools
   - Supports `reasoning_effort` and `reasoning_summary` kwargs
   - Extracts web_search traces from response metadata

5. **DONE** — Gemini runner (`trader/online/runners/gemini_runner.py`):
   - `google-genai` SDK with `client.models.generate_content()`
   - Google Search grounding + all function tools simultaneously
     (impossible with PydanticAI — this was the key unblocking win)
   - Manual function calling control for full tracing
   - Extracts thinking content and grounding metadata

6. **DONE** — Pipeline dispatch (`trader/online/agent_pipeline.py`):
   - `AgentSpec.runner` field: `"grok"`, `"openai"`, `"gemini"`, `"pydanticai"`
   - `_dispatch_runner()` routes to appropriate native SDK runner
   - PydanticAI fallback for TestModel testing
   - System prompt enhanced: final agent gets JSON output format instructions
   - Gemini now `function_tools=True` (was False)

7. **DONE** — All tests pass (108/109, 1 pre-existing FinnHub 403)

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
