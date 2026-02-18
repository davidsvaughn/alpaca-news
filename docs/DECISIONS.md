# Design Decisions & Open Questions

> Rationale log for architectural choices and unresolved questions.
> For stable architecture reference, see [ARCHITECTURE.md](ARCHITECTURE.md).
> For implementation status, see [ROADMAP.md](ROADMAP.md).
>
> Last updated: 2026-02-17

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent framework | Native SDKs + custom orchestrator (was PydanticAI) | Switched from PydanticAI to native SDKs (`openai`, `google-genai`) for pipeline runners. PydanticAI added overhead without value — native SDKs give: server-side x_search (no inner API calls), Gemini function tools + Google Search simultaneously, direct usage/cost metadata, simpler tool-calling loops. PydanticAI kept for watcher check-ins and TestModel testing |
| Explorer architecture | Multi-agent sequential pipeline | Different LLMs compound evidence; Grok (search+X), OpenAI (reasoning+search), Gemini (grounding+tools+synthesis) |
| Database | SQLite (v1), Postgres later | Speed of development; upgrade when concurrency demands it |
| Explorer approach | Free-form tool use | LLM intelligence improves faster than bandit learning on limited data |
| Knowledge model | Flat insights + BM25 memory | Avoids premature categorization; contextual + general knowledge |
| Position management | Watch lifecycle with phases | Captures full trade trajectory for learning, not just entry |
| Data redundancy | Schwab primary, yfinance fallback | Schwab has best real-time data; yfinance provides free resilience |
| Technical indicators | Computed locally (stockstats) | Zero cost; LLM selects which are relevant per situation |
| Trading mode | Paper-first | Build confidence before live execution |
| Reflection trigger | On-demand (user-controlled) | Automated evaluation too early; user wants to inspect + control inputs |
| Evaluation data format | Nested JSON tree (EvalRecord) | Same structure serves both human UI (accordions) and LLM evaluator (markdown) |
| Insight tiers | Tier A (auto-apply) + Tier B (code changes) | Separates what can be learned without code changes from what needs development |
| Evaluation model | Gemini (configurable) | Cheap, fast, good at structured JSON output; avoids using pipeline models as self-evaluators |
| FinnHub as data source | Complement Schwab/yfinance | Free tier provides data neither has: earnings surprises, calendar, analyst consensus. Premium endpoints (sentiment, targets, upgrades) are 403 |
| Auto-fetch vs tool pattern | Always-relevant data auto-fetched; on-demand data as tools | Earnings context goes in every prompt (always relevant); analyst ratings is an agent tool (sometimes relevant) |
| Budget awareness approach | Append to tool results via ctx.usage | Preserves prefix caching (no system prompt changes); agents naturally see budget after each tool call; uses PydanticAI's real cumulative counters |
| Include potentially redundant data | Yes — let reflection loop evaluate | FinnHub news + analyst data may overlap with existing sources, but including them tests whether the self-improvement loop can identify and weed out low-value inputs |
| Token limit handling | Soft caps with graceful degradation | Hard limits destroy accumulated work; partial data is better than no data. Pipeline continues on agent failure |
| Pre-fetch vs tool pattern | Basic data pre-fetched; investigative tools remain | Eliminates redundant tool calls (observed 3x check_price, 4x get_finnhub_news per pipeline). Agents focus on web_search, x_search, url_fetch |
| Budget metric | Output tokens (not total) | Output tokens cost 3-4x more than input; penalizing input discourages richer context. `PIPELINE_OUTPUT_TOKENS_LIMIT=50000` |
| Tool call ledger | Full results for investigative tools | Downstream agents see complete web_search/x_search/url_fetch results. url_fetch capped at 3000 chars. Pre-fetched data tools omitted (already in prompt) |
| Gemini tool mode | Google Search grounding + all function tools | Was grounding-only under PydanticAI (couldn't mix). Native `google-genai` SDK enables both simultaneously. Gemini now has full tool access like other agents, plus independent Google Search grounding |
| Follow-up vs Watch | Separate data model | Watch = active position management (agent reasoning, hold/exit decisions). FollowUp = passive data collection (mechanical, no decisions). Different enough to warrant separate models, but share scheduling infrastructure pattern |
| Follow-up: no-buy vs post-exit | Same FollowUp structure, `reason` field distinguishes | Same collection process, same scheduling. Only difference is trigger context (no watch vs sealed watch). Avoids unnecessary type splitting |
| Follow-up query planning | LLM planner (gemini-3-flash) before mechanical collection | Template queries too generic. LLM planner proposes context-aware queries informed by headline, prediction, prior collection results. ~$0.001 per call |
| Follow-up search API | Direct httpx to Grok API (no PydanticAI agent) | No agent reasoning needed for mechanical data collection. Direct API calls are simpler, cheaper, and more predictable. Same pattern as x_search function tool |
| Query effectiveness | Simple heuristic (answer length > 100 chars) | Avoids costly LLM evaluation of search quality. Good enough to identify dead-end queries. LLM planner uses quality feedback to adjust subsequent queries |
| Activity tracking | In-memory ActivityTracker (not DB) | Dashboard needs sub-second reads; operations are transient; no value in persisting "currently running" state across restarts |
| In-flight cost visibility | ActivityTracker accumulates per-activity cost, summed for dashboard | CostTracker is per-item ephemeral; daily cost only reflects sealed snapshots. ActivityTracker bridges the gap with real-time cost_usd per activity |
| Activity panel refresh | HTMX polling every 10s + SSE-triggered immediate | 10s is responsive enough for activity changes; SSE events trigger immediate refresh on state transitions (backfill progress, snapshot sealed) |
| Gemini system prompt | Synthesis-focused, no tool mentions | Gemini can't combine Google grounding + function tools (Live API only). Previous prompt mentioned ~14 tools Gemini couldn't call, confusing the model. Now gated on `spec.function_tools` |
| Reasoning effort control | Per-provider `model_settings` on AgentSpec | PydanticAI has first-class support via `OpenAIResponsesModelSettings` and `GoogleModelSettings`. Settings passed at `agent.run()` time, not agent construction. Configurable via env vars (`OPENAI_REASONING_EFFORT`, `GEMINI_THINKING_LEVEL`) |
| Grok reasoning | Skip effort control, capture tokens only | grok-4 always runs at max reasoning (no `reasoning_effort` param). Only grok-3-mini exposes `reasoning_content`. grok-4 encrypted reasoning only. Just track `reasoning_tokens` from usage details |
| OpenAI reasoning summary | `detailed` (always on) | Returns `ThinkingPart` objects in model responses. Stored as `thinking_summary` per round for visibility into model reasoning. Minimal cost overhead |
| Reasoning token tracking | Extract from `usage.details` per provider | OpenAI: `details['reasoning_tokens']`, Gemini: `details['thoughts_tokens']`. Heterogeneous key names unified with fallback logic. Displayed in snapshot detail UI |
| Builtin tool trace output | Descriptive string when None | `BuiltinToolReturnPart.content` is often None for Gemini grounding (server-side, not exposed). `"[server-side grounding]"` is more informative than `"None"` in the UI |
| Server-side x_search | Grok runner uses `{"type": "x_search"}` (native) | Previously x_search was a PydanticAI function tool making inner httpx POST to xAI — ~12s each. Server-side x_search eliminates the inner API call overhead entirely. Only Grok supports x_search |
| Pure tool functions | `tool_core.py` with `TOOL_REGISTRY` | All 16 market data tools extracted as plain functions (no RunContext). Single source of truth — each runner wraps into its SDK's format. `explorer_agent.py` @tool decorators delegate to tool_core |
| TradingSignal extraction | JSON-in-prompt for native runners | PydanticAI enforced schema via `output_type`. Native runners instruct model to output JSON in ```json blocks. Extracted via regex + `model_validate_json()`. Three fallback strategies: code block → raw JSON → regex. Fallback to `signal=None` |
| Runner dispatch pattern | `AgentSpec.runner` field | Simple string dispatch: `"grok"` → `run_grok()`, `"openai"` → `run_openai()`, `"gemini"` → `run_gemini()`, `"pydanticai"` → `_run_pydanticai_agent()`. No runner class hierarchy — just async functions |
| Gemini system prompt | Now includes full tool instructions | Was synthesis-only (Gemini had no tools). With native SDK giving Gemini all function tools, system prompt now includes tool docs and investigation guidance like other agents |

---

## Open Questions

1. ~~**Tool-use API mechanics:**~~ **RESOLVED** — Each provider's native SDK handles
   the tool-call loop directly. OpenAI/Grok: `responses.create()` + `previous_response_id`
   chaining. Gemini: `generate_content()` with manual function calling. No framework needed.

2. **X stream in explorer:** X stream is inherently async (runs N minutes). Should
   the LLM start a stream and continue investigating (parallel), or does it block?
   **Partially resolved:** Burst runs in background thread (parallel with pipeline).
   Quality gate added: after N tweets, cheap LLM checks relevance and auto-retries
   with revised rules if content is noise. Stale/backfill news skips streaming entirely.

3. **Triage + explorer boundary:** Does triage stay as a separate Stage 1 (cheap
   filter before expensive exploration), or merge into the free-form loop?

4. ~~**Multi-provider tool routing:**~~ **RESOLVED** — Each provider's native SDK
   handles search differently: Grok gets server-side `web_search` + `x_search`,
   OpenAI gets server-side `web_search`, Gemini gets Google Search grounding.
   All three get the same 16 function tools via `TOOL_REGISTRY`.

5. **Pipeline composition:** Should the agent sequence be configurable (e.g.
   run only 2 agents instead of 3)? Or always run the full pipeline?

6. **Loop termination:** When the final agent says "need more info" and triggers
   another round, how many rounds max? What's the convergence criterion?

---

## Contextual Bandits Design (Phase D+)

Lightweight RL that learns in real-time: observe context → pick action → observe reward.

### Application points

| Decision | Context features | Actions | Reward signal | Est. data needed |
|----------|-----------------|---------|---------------|-----------------|
| **Triage threshold** | News source, sector, symbol count, time-of-day, market session | Confidence cutoff for investigate vs. skip | Signal quality vs. cost saved | ~50-100 events |
| **Pipeline configuration** | News type, triage confidence | Which agents to run, agent order | Signal quality / cost ratio | ~100-200 events |
| **Model routing** | News type, complexity, sector | Which LLM for each pipeline slot | Per-slot accuracy, cost | ~50 per model pair |
| **Budget allocation** | Triage confidence, news type, market session, volatility | Tool call limit, token budget per agent | Marginal value of last tool call | ~100+ events |
| **Parameter tuning** | Historical performance by news type, time-of-day, market regime | Confidence threshold, max rounds, hold duration limits, cost caps | Trade outcome quality at different settings | ~200+ events |

### Thompson Sampling for insights.json

```python
# Current: flat score
{"id": "ins_001", "text": "...", "score": 3}

# Thompson Sampling: Beta distribution
{"id": "ins_001", "text": "...", "successes": 5, "failures": 2}

# At prompt injection time:
#   sample = Beta(successes, failures).sample()
#   → new/uncertain insights get explored
#   → validated insights converge to true value
```

### Implementation approach

1. **Phase 1 (data capture):** Log orchestrator config alongside outcomes. No bandits yet.
2. **Phase 2 (simple bandits):** Thompson Sampling for 1-2 decisions after ~100-200 events.
3. **Phase 3 (contextual bandits):** Feature-based bandits (LinUCB or neural) for model routing and budget.

---

## LLMFactor's "Factors" Concept (parked)

From `docs/refs/LLMFactor.pdf` — ideas worth revisiting once we have operational experience:

1. **Causal hypotheses vs. pattern labels.** Push insights toward "mechanism" statements.
2. **Decomposition into constituent factors.** Makes insights matchable to new events.
3. **Context conditions.** Insights that specify when they apply are more useful.
4. **Structured extraction templates.** If a useful insight structure emerges, template it.
5. **Thompson Sampling for scoring.** Beta distributions instead of flat +1/-1.

**Why parked:** We don't yet know what forms useful insights will take. Revisit after
2-4 weeks of operation.

---

## Evaluated and Deferred Ideas

| Idea | Source | Why deferred |
|------|--------|-------------|
| ~~Full adversarial debate~~ | TradingAgents | **ADOPTED** — multi-agent pipeline provides natural adversarial reasoning |
| Three-way risk debate | TradingAgents | Subsumed by multi-agent pipeline |
| Alpha Vantage NEWS_SENTIMENT | TradingAgents | Limited free tier; FinnHub news sentiment also premium-only (403). yfinance news + FinnHub company news + web_search cover this space |
| Postgres / Supabase | Original design | SQLite sufficient for single-process v1 |
| Streamlit offline workbench | Original design | FastAPI dashboard is primary |
| ~~Contextual bandits~~ | Original design | **PLANNED (Phase D+)** — see above |
| LangGraph workflow orchestration | TradingAgents | Custom sequential orchestrator is simpler |
| PydanticAI for pipeline | Phase B-4 migration | Replaced with native SDKs. PydanticAI added overhead without enabling server-side tools or Gemini multi-tool. Kept for watcher (simpler single-agent use case) and TestModel |

---

## Reference Papers

| Paper | File | Key ideas adopted |
|-------|------|-------------------|
| Trading-R1 | `docs/refs/TradingR1.pdf` | Multi-modal input, volatility-adjusted labeling, reverse reasoning distillation, categorical data sampling |
| TradingAgents | (external repo) | yfinance, BM25 memory, bull/bear prompts, vendor fallback |
| LLMFactor | `docs/refs/LLMFactor.pdf` | Causal hypothesis factors, SKGP prompting (parked for future insight design) |
| MarketSenseAI | `docs/refs/MarketSenseAI.pdf` / `MarketSenseAI_2.pdf` | Reference architecture |
| LLM-Guided RL | `docs/refs/LLM_Guided_RL.pdf` | RL over tool-call trajectories |
| Self-improving agents | `docs/refs/self-improving-agents.pdf` | Reflection + insight loop design |
