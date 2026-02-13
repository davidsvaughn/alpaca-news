# Design Decisions & Open Questions

> Rationale log for architectural choices and unresolved questions.
> For stable architecture reference, see [ARCHITECTURE.md](ARCHITECTURE.md).
> For implementation status, see [ROADMAP.md](ROADMAP.md).
>
> Last updated: 2026-02-12

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent framework | PydanticAI + custom orchestrator | PydanticAI handles per-agent loop; custom orchestrator sequences agents and passes context |
| Explorer architecture | Multi-agent sequential pipeline | Different LLMs compound evidence; Grok (search+X), OpenAI (reasoning+search), Claude (synthesis+search) |
| Database | SQLite (v1), Postgres later | Speed of development; upgrade when concurrency demands it |
| Explorer approach | Free-form tool use | LLM intelligence improves faster than bandit learning on limited data |
| Knowledge model | Flat insights + BM25 memory | Avoids premature categorization; contextual + general knowledge |
| Position management | Watch lifecycle with phases | Captures full trade trajectory for learning, not just entry |
| Data redundancy | Schwab primary, yfinance fallback | Schwab has best real-time data; yfinance provides free resilience |
| Technical indicators | Computed locally (stockstats) | Zero cost; LLM selects which are relevant per situation |
| Trading mode | Paper-first | Build confidence before live execution |

---

## Open Questions

1. ~~**Tool-use API mechanics:**~~ **RESOLVED** — PydanticAI handles the tool-call
   loop, message threading, and tool dispatch. Native support for Anthropic,
   OpenAI, and Google providers without any compatibility shims.

2. **X stream in explorer:** X stream is inherently async (runs N minutes). Should
   the LLM start a stream and continue investigating (parallel), or does it block?
   **Partially resolved:** Burst runs in background thread (parallel with pipeline).
   Quality gate added: after N tweets, cheap LLM checks relevance and auto-retries
   with revised rules if content is noise. Stale/backfill news skips streaming entirely.

3. **Triage + explorer boundary:** Does triage stay as a separate Stage 1 (cheap
   filter before expensive exploration), or merge into the free-form loop?

4. ~~**Multi-provider tool routing:**~~ **RESOLVED** — Each agent uses its own
   provider's native `WebSearchTool`. No routing needed — all three providers
   (Grok, OpenAI, Claude) get native iterative web search. `x_search` is a
   function tool that calls Grok under the hood, available to all agents.

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
| Alpha Vantage NEWS_SENTIMENT | TradingAgents | Limited free tier; yfinance news + web_search cover this |
| Postgres / Supabase | Original design | SQLite sufficient for single-process v1 |
| Streamlit offline workbench | Original design | FastAPI dashboard is primary |
| ~~Contextual bandits~~ | Original design | **PLANNED (Phase D+)** — see above |
| LangGraph workflow orchestration | TradingAgents | Custom sequential orchestrator is simpler |

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
