# xAI / Grok Data Source (x_search)

> **Status**: Active (paid API)
> **Package**: `openai` SDK (OpenAI-compatible endpoint)
> **Runner**: [`trader/online/runners/grok_runner.py`](trader/online/runners/grok_runner.py)
> **Env vars**: `XAI_API_KEY`, `XAI_BASE_URL`

---

## Overview

xAI provides the **Grok** family of LLMs with unique **server-side search tools** — `x_search` (X/Twitter search) and `web_search` (general web search). These tools run entirely on xAI's infrastructure, meaning no additional API keys, rate limits, or round-trips are needed. Grok is our **first agent** in the 3-agent pipeline (Grok → OpenAI → Gemini).

The key differentiator is `x_search` — real-time search across X/Twitter posts — which is exclusive to xAI and unavailable from any other LLM provider.

---

## What We Currently Use

### 1. Server-Side x_search

**Type**: Built-in tool (no function call overhead)
**Invoked by**: Grok model autonomously during investigation

Searches X/Twitter posts in real-time. The model decides when to search and what queries to use based on the news event context. Results are embedded directly in the model's response — we don't see the raw search results, only the model's synthesis.

**Cost**: $0.005 per invocation (+ token costs for the inner inference)

**Trace format**: Recorded as `builtin=True` in tool traces.

### 2. Server-Side web_search

**Type**: Built-in tool (no function call overhead)
**Invoked by**: Grok model autonomously during investigation

General web search for broader context — company news, analyst reports, financial data. Same server-side execution model as x_search.

**Cost**: $0.005 per invocation (+ token costs)

### 3. Function Tools (16 custom tools)

All tools from `TOOL_REGISTRY` are exposed to Grok as function tools:
- Market data tools (check_price, get_fundamentals, etc.)
- News tools (get_finnhub_news, get_analyst_ratings)
- Research tools (url_fetch, x_stream_cache)

These are **client-side** — Grok makes function calls, our runner executes them locally, and sends results back.

---

## How It Works

### Runner Architecture

```python
from openai import OpenAI

client = OpenAI(api_key=key, base_url="https://api.x.ai/v1/")
response = client.responses.create(
    model="grok-4-1-fast-reasoning",
    input=conversation,
    tools=[
        {"type": "x_search"},          # Server-side
        {"type": "web_search"},         # Server-side
        {"type": "function", ...},      # Client-side (from TOOL_REGISTRY)
    ],
)
```

### Tool-Calling Loop

xAI does **not** support `previous_response_id` like OpenAI. Instead, the runner manually builds the full conversation by appending response items and tool results:

```
Turn 1: Send message → Model calls x_search + check_price
Turn 2: x_search auto-resolves; send check_price result → Model calls url_fetch
Turn 3: Send url_fetch result → Model produces final analysis
```

Max turns: 15 (configurable via `max_turns`)

### Response Parsing

For the final agent, output is parsed for a JSON `TradingSignal`:
1. Try markdown code blocks: ` ```json {...}``` `
2. Try raw JSON text
3. Try loose JSON pattern: `{.*"direction".*}`
4. Fall back to raw text

### Reasoning/Thinking

Grok may include reasoning summaries in the response. These are extracted and stored in `AgentRunResult.thinking_summary`.

---

## Pricing

### Model Pricing (per 1M tokens)

| Model | Input | Output | Cached Input | Notes |
|-------|------:|-------:|-------------:|-------|
| **grok-4-1-fast-reasoning** | $0.20 | $0.50 | $0.05 | Our default |
| grok-4-1-fast-non-reasoning | $0.20 | $0.50 | $0.05 | No chain-of-thought |
| grok-4-fast-reasoning | $0.20 | $0.50 | $0.05 | Previous generation |
| grok-code-fast-1 | $0.20 | $1.50 | — | Code-specialized |
| grok-4-0709 | $3.00 | $15.00 | — | Full Grok 4 (expensive) |
| grok-3 | $3.00 | $15.00 | — | Previous generation |
| grok-3-mini | $0.30 | $0.50 | — | Small model |

Source: [docs.x.ai/developers/models](https://docs.x.ai/developers/models)

### Server-Side Tool Costs (per 1,000 calls)

| Tool | Cost |
|------|-----:|
| **x_search** | $5.00 ($0.005/call) |
| **web_search** | $5.00 ($0.005/call) |
| Code Execution | $5.00 |
| File Attachments | $10.00 |
| Collections Search | $2.50 |

**Note**: The per-call fee is just the invocation cost. Each tool call also triggers an internal Grok inference with its own token costs. Observed average total: ~$0.018/call (fee + tokens).

### Prompt Caching

All requests automatically benefit from prompt caching. Repeated prompts cost less — cached input tokens are 75% cheaper ($0.05 vs $0.20 per 1M). Cached token usage is visible in the API response `usage` object.

### Cost in Our Pipeline

Typical investigation cost for one news event (Grok agent only):
- ~5,000–15,000 input tokens × $0.20/1M = $0.001–$0.003
- ~2,000–8,000 output tokens × $0.50/1M = $0.001–$0.004
- ~2–5 search calls × $0.005 = $0.01–$0.025
- **Total per event**: ~$0.01–$0.03

---

## Pipeline Role

### Agent 1: Grok (First Investigator)

```python
AgentSpec(
    runner="grok",
    model="grok-4-1-fast-reasoning",
    role="FIRST investigator. Your strength is web and social media research.",
    is_final=False,
    excluded_tools=frozenset(),  # Gets all tools
)
```

**Why Grok is first**: x_search gives immediate access to real-time social sentiment around the news event — something no other provider offers. This social context is passed downstream to OpenAI and Gemini via the tool call ledger.

### PydanticAI Wrapper (Watcher Only)

For watcher check-ins (not the main pipeline), there's a PydanticAI function tool wrapper that makes direct HTTP calls to the xAI API:

```python
# explorer_agent.py — direct httpx POST for watcher check-ins
resp = httpx.post(
    "https://api.x.ai/v1/responses",
    json={"model": model, "input": query, "tools": [{"type": "x_search"}]},
    headers={"Authorization": f"Bearer {api_key}"},
)
```

---

## Configuration

```bash
# .env
XAI_API_KEY=<your-key-here>
XAI_BASE_URL=https://api.x.ai/v1          # Optional (this is the default)
XSEARCH_PROVIDER=grok
XSEARCH_MODEL=grok-4.1-fast-reasoning     # Model for pipeline
MAX_X_SEARCHES_PER_ITEM=2                  # Budget guard
MAX_WEB_SEARCHES_PER_ITEM=3               # Budget guard
```

---

## What Else xAI Offers (Not Currently Used)

| Feature | Description | Status |
|---------|-------------|--------|
| `code_execution` | Server-side Python sandbox | Available ($5/1K calls) |
| `collections_search` | RAG over uploaded documents | Available ($2.50/1K calls) |
| `mcp()` | Remote MCP server access | Available (token-based only) |
| `view_image` | Image understanding | Available (token-based) |
| `view_x_video` | X video understanding | Available (token-based) |
| Grok 4 (full) | Frontier reasoning model | Available ($3/$15 per 1M) |
| Image generation | `grok-2-image`, `grok-imagine-*` | Available |

### Potential Additions

- **`code_execution`** — could be useful for on-the-fly financial calculations
- **Grok 4 (full)** — more capable reasoning, but 15x more expensive than fast models
- **Image understanding** — could analyze chart screenshots from URLs

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/online/runners/grok_runner.py`](trader/online/runners/grok_runner.py) | `run_grok()` — main Grok runner |
| [`trader/online/explorer_agent.py`](trader/online/explorer_agent.py) | PydanticAI x_search wrapper (watcher) |
| [`trader/llm/pricing.py`](trader/llm/pricing.py) | `GROK_PRICING` table |
| [`trader/online/agent_pipeline.py`](trader/online/agent_pipeline.py) | Agent 1 spec + dispatch |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | 16 function tools available to Grok |

---

## References

- [xAI API Documentation](https://docs.x.ai/)
- [xAI Models & Pricing](https://docs.x.ai/developers/models)
- [Grok 4.1 Fast & Agent Tools](https://x.ai/news/grok-4-1-fast)
