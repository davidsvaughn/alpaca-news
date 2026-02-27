# OpenAI Data Source (Responses API)

> **Status**: Active (paid API)
> **Package**: `openai` SDK
> **Runner**: [`trader/online/runners/openai_runner.py`](trader/online/runners/openai_runner.py)
> **Env vars**: `OPENAI_API_KEY`
> **Demo**: [`demo/openai_demo.py`](demo/openai_demo.py) — `uv run python demo/openai_demo.py [QUERY]`

---

## Overview

OpenAI provides our **second agent** in the 3-agent pipeline (Grok → OpenAI → Gemini). We use the **Responses API** (not Chat Completions) with server-side `web_search` and all 16 custom function tools. OpenAI offers strong reasoning capabilities with configurable `reasoning_effort`.

---

## What We Currently Use

### 1. Server-Side web_search

**Type**: Built-in tool (server-side, like Grok's)
**Invoked by**: Model autonomously during investigation

```python
tools = [{"type": "web_search"}, ...]
response = client.responses.create(model=model, input=conversation, tools=tools)
```

Results are embedded in the response — we record query traces but don't see raw search results.

**Cost**: $0.01 per invocation (2x Grok's web_search) + search content tokens charged at model rates.

### 2. Function Tools (16 custom tools)

Same tools from `TOOL_REGISTRY` as Grok:
- Market data: check_price, get_fundamentals, get_price_history, etc.
- News: get_finnhub_news, get_analyst_ratings, get_company_news
- Research: url_fetch, x_stream_cache

### 3. Reasoning

OpenAI supports configurable reasoning via:
```python
kwargs["reasoning"] = {
    "effort": "medium",       # none, low, medium, high, xhigh
    "summary": "auto",        # Reasoning summary in output
}
```

Reasoning tokens are tracked separately in `total_usage["reasoning_tokens"]`.

---

## How It Works

### Runner Architecture

```python
from openai import OpenAI

client = OpenAI(api_key=key)
response = client.responses.create(
    model="gpt-5.1",
    input=conversation,
    tools=[{"type": "web_search"}, {"type": "function", ...}],
    reasoning={"effort": "medium"},
)
```

### Tool-Calling Loop

OpenAI builds conversation by appending `response.output` items and tool results:

```python
conversation.extend(response.output)       # Model's function_call items
conversation.extend(tool_results)           # Our function_call_output items
response = client.responses.create(model=model, input=conversation, tools=tools)
```

**Note**: Unlike Grok, OpenAI supports `previous_response_id` for stateful conversations, but we don't currently use it — we build the full conversation manually for consistency across runners.

### Response Parsing

Same JSON TradingSignal extraction as Grok (regex-based).

---

## Pricing

### Model Pricing (per 1M tokens)

| Model | Input | Output | Notes |
|-------|------:|-------:|-------|
| **gpt-5.1** | $1.25 | $10.00 | Our current default |
| gpt-5.2 | $1.75 | $14.00 | Latest flagship |
| gpt-5-mini | $0.25 | $2.00 | Fast/cheap alternative |
| gpt-4.1 | $2.00 | $8.00 | Previous generation |
| gpt-4.1-mini | $0.40 | $1.60 | Previous gen mini |
| o4-mini | $1.10 | $4.40 | Reasoning-optimized |

Source: [openai.com/api/pricing](https://openai.com/api/pricing/)

### Server-Side Tool Costs

| Tool | Cost per 1K Calls | Per Call |
|------|-------------------:|--------:|
| **web_search** | $10.00 | $0.01 |

Web search also charges for **search content tokens** at the model's per-token rate. For mini models, this is a fixed block of 8,000 input tokens per search call.

### Pricing Tiers

| Tier | Cost Multiplier | Latency |
|------|:---:|------|
| Batch | 50% discount | 24hr processing |
| Flex | Varies | Variable latency |
| **Standard** | 1x (default) | Normal |
| Priority | 2x | Faster |

### Prompt Caching

Automatic 50–90% discount on repeated prompt content. Cached token usage visible in the API response.

### Cost in Our Pipeline

Typical investigation cost for one news event (OpenAI agent only):
- ~15,000–30,000 input tokens × $1.25/1M = $0.02–$0.04
- ~3,000–10,000 output tokens × $10.00/1M = $0.03–$0.10
- ~1–3 web_search calls × $0.01 = $0.01–$0.03
- **Total per event**: ~$0.06–$0.17

OpenAI is typically the most expensive agent in our pipeline due to higher per-token rates.

---

## Pipeline Role

### Agent 2: OpenAI (Second Investigator)

```python
AgentSpec(
    runner="openai",
    model="gpt-5.1",
    role="SECOND investigator. Build on Agent 1's findings.",
    is_final=False,
    excluded_tools=frozenset({"x_search"}),  # No x_search (OpenAI only)
)
```

**Why OpenAI is second**: Strong reasoning and web search capability. Builds on Grok's social sentiment findings with deeper analytical investigation. The tool call ledger from Grok (including x_search results) is passed downstream.

**Budget skip**: If cumulative pipeline cost exceeds `PIPELINE_MAX_COST_USD` after Grok, OpenAI may be skipped. The final agent (Gemini) always runs.

---

## Configuration

```bash
# .env
OPENAI_API_KEY=<your-key-here>
RESEARCH_MODEL=gpt-5.1
```

---

## What Else OpenAI Offers (Not Currently Used)

| Feature | Description | Status |
|---------|-------------|--------|
| `computer_use` | Browser/desktop automation | Available |
| `file_search` | RAG over uploaded files | Available |
| `code_interpreter` | Python sandbox execution | Available |
| Batch API | Async batch processing (50% discount) | Available |
| Embeddings | Text embeddings for semantic search | Available |
| Image generation | DALL-E | Available |
| Audio/TTS | Whisper, TTS | Available |
| GPT-5.2 | Latest flagship model | Available |

### Potential Additions

- **Batch API** — could be used for bulk retrospective analysis at 50% cost
- **Embeddings** — semantic search over historical snapshots/news
- **GPT-5.2** — upgrade path for better reasoning (higher cost)

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/online/runners/openai_runner.py`](trader/online/runners/openai_runner.py) | `run_openai()` — main OpenAI runner |
| [`trader/llm/pricing.py`](trader/llm/pricing.py) | `OPENAI_PRICING` table |
| [`trader/online/agent_pipeline.py`](trader/online/agent_pipeline.py) | Agent 2 spec + dispatch |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | 16 function tools available to OpenAI |

---

## References

- [OpenAI API Documentation](https://platform.openai.com/docs/)
- [OpenAI Pricing](https://openai.com/api/pricing/)
- [Responses API Guide](https://platform.openai.com/docs/api-reference/responses)
