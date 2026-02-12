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
| **X API stream** | DONE | Filtered stream, burst mode, rules |
| **Database (SQLite)** | DONE | Snapshots + watches tables, idempotent inserts |
| **Dashboard (FastAPI + SSE)** | DONE | Live feed, event bus |
| **Schwab market data** | DONE | Quotes, candles, streaming, options, fundamentals, movers, market hours |
| **Knowledge store** | PARTIAL | skip_patterns, reliable_sources exist; insights.json TODO |
| **Explorer (multi-agent pipeline)** | DONE | Sequential Grok→OpenAI→Gemini pipeline — wired into orchestrator |
| **yfinance data layer** | DONE | Free data: fundamentals, insider tx, price history, news, technicals |
| **BM25 situation memory** | TODO | New — learned from TradingAgents |
| **Schwab Tier 1 expansion** | DONE | Options IV, fundamentals, movers, market hours, enhanced context |
| **insights.json** | TODO | Flat scored insights for prompt injection |
| **Watch lifecycle** | IN PROGRESS | Steps 1-2 DONE (model + DB + creation + monitoring scheduler); Steps 3-5 TODO |
| **Signal extraction step** | DONE (by design) | Built into PydanticAI output_type=TradingSignal |
| **Offline loop** | TODO | Labeling, hop scoring, reflection |
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

## Phase C: Watch lifecycle — IN PROGRESS

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

3. TODO — Exit decision logic + retrospective phase
4. TODO — Retrospective snapshots (post-exit counterfactual analysis)
5. TODO — Watch sealing

## Phase D: Offline loop — TODO

Close the learning feedback loop.

1. Outcome labeler (attach +15m/+60m/+1d returns to Snapshots)
2. Hop scorer (per-trace and per-sequence value estimation)
3. Reflection prompt (review scored Snapshots + sealed Watches)
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
