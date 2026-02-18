# Google Gemini Data Source

> **Status**: Active (paid API with free tier)
> **Package**: `google-genai>=1.62.0`
> **Runner**: [`trader/online/runners/gemini_runner.py`](trader/online/runners/gemini_runner.py)
> **Env vars**: `GOOGLE_API_KEY`

---

## Overview

Google Gemini is our **final (synthesizer) agent** in the 3-agent pipeline (Grok → OpenAI → Gemini). It uses the `google-genai` SDK with **Google Search grounding** — a unique feature that grounds the model's responses in real-time Google Search results. Gemini is also the only provider where we can combine Google Search grounding with custom function tools simultaneously.

---

## What We Currently Use

### 1. Google Search Grounding

**Type**: Built-in grounding tool
**How**: `types.Tool(google_search=types.GoogleSearch())`

Unlike xAI/OpenAI's web_search (which returns results the model synthesizes), Google Search grounding **grounds the model's own responses** in real-time search results. The grounding metadata includes:
- Search queries generated
- Source URLs used
- Grounding confidence scores

We extract these as traces:
```python
for candidate in response.candidates:
    gm = candidate.grounding_metadata
    for q in gm.web_search_queries:
        # Record as builtin=True web_search trace
```

### 2. Function Tools (16 custom tools)

All tools from `TOOL_REGISTRY` exposed as `FunctionDeclaration`:

```python
types.FunctionDeclaration(
    name=td.name,
    description=td.description,
    parameters_json_schema=td.parameters,
)
```

Combined with Google Search in a single tools list — something PydanticAI couldn't do, which is why we built the native Gemini runner.

### 3. Thinking/Reasoning

Gemini supports thinking via `ThinkingConfig`:
```python
config_kwargs["thinking_config"] = types.ThinkingConfig(
    thinking_budget=1024,  # Max thinking tokens
)
```

Thinking content is extracted from response parts where `part.thought == True`.

---

## How It Works

### Runner Architecture

```python
from google import genai
from google.genai import types

client = genai.Client(api_key=key)

tools = [
    types.Tool(google_search=types.GoogleSearch()),     # Grounding
    types.Tool(function_declarations=[...]),              # Function tools
]

config = types.GenerateContentConfig(
    tools=tools,
    system_instruction=system_prompt,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

response = client.models.generate_content(
    model="gemini-3-flash-preview",
    contents=contents,
    config=config,
)
```

**Key**: `automatic_function_calling` is disabled — we handle the tool-calling loop manually for full tracing control.

### Tool-Calling Loop

```python
while turn < max_turns:
    function_calls = response.function_calls
    if not function_calls: break

    # Add model's response to contents
    contents.append(response.candidates[0].content)

    # Execute tools, build function response parts
    for fc in function_calls:
        result_str = _execute_function_call(fc.name, dict(fc.args), market, ...)
        function_response_parts.append(
            types.Part.from_function_response(name=fc.name, response=result_dict)
        )

    contents.append(types.Content(role="user", parts=function_response_parts))
    response = client.models.generate_content(model=model, contents=contents, config=config)
```

### Response Parsing

Same JSON TradingSignal extraction as Grok/OpenAI (regex-based). As the final agent, Gemini is always expected to produce a TradingSignal.

---

## Pricing

### Model Pricing (per 1M tokens)

| Model | Input (≤200K) | Input (>200K) | Output | Notes |
|-------|------:|------:|------:|-------|
| **gemini-3-flash-preview** | $0.30 | $0.30 | $2.50 | Our default |
| gemini-3-flash-preview-lite | $0.10 | $0.10 | $0.40 | Ultra-cheap |
| gemini-3-pro-preview | $2.00 | $4.00 | $12.00 | Higher capability |
| gemini-2.5-pro | $1.25 | $2.50 | $10.00 | Previous gen |
| gemini-2.0-flash | $0.10 | $0.10 | $0.40 | Previous gen |

Source: [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing)

### Google Search Grounding Cost

| Model Family | Pricing Model | Cost | Free Allowance |
|---|---|---:|---|
| **Gemini 3** | Per-query | $14/1K queries ($0.014/query) | 5,000 prompts/mo (paid tier) |
| Gemini 2.x | Per-prompt | $35/1K prompts ($0.035/prompt) | 1,500 queries/day (paid tier) |

**Important**: A single prompt may generate **multiple** Google Search queries. With Gemini 3, you're charged per individual query, not per prompt. This can add up if the model decides to search extensively.

### Free Tier

Gemini API has a free tier:
- 500–1,500 requests per day (varies by model)
- Google Search grounding free up to 5,000 prompts/month
- Lower rate limits than paid tier

### Cost in Our Pipeline

Typical investigation cost for one news event (Gemini agent only):
- ~20,000–40,000 input tokens × $0.30/1M = $0.006–$0.012
- ~5,000–15,000 output tokens × $2.50/1M = $0.013–$0.038
- ~2–6 Google Search queries × $0.014 = $0.028–$0.084
- ~2–5 function tool calls (free — just the token cost)
- **Total per event**: ~$0.05–$0.13

Gemini is significantly cheaper than OpenAI for token costs, but Google Search grounding can add up.

---

## Pipeline Role

### Agent 3: Gemini (Final Synthesizer)

```python
AgentSpec(
    runner="gemini",
    model="gemini-3-flash-preview",
    role="FINAL synthesizer. Produce the trading signal.",
    is_final=True,
    excluded_tools=frozenset({"x_search"}),  # No x_search
)
```

**Why Gemini is final**:
1. **Google Search grounding** provides a third independent web search perspective
2. **All function tools** available (unlike PydanticAI, which couldn't combine grounding + tools)
3. **Cost-effective** — cheapest token rates of the three providers
4. **Always runs** — even if budget exceeded, the final agent is never skipped

### Budget Protection

Gemini always runs as the final agent regardless of cumulative pipeline cost. This ensures every news event gets a synthesized signal.

---

## Configuration

```bash
# .env
GOOGLE_API_KEY=<your-key-here>
SENTIMENT_PROVIDER=gemini
SENTIMENT_MODEL=gemini-3-flash-preview
```

---

## What Else Gemini Offers (Not Currently Used)

| Feature | Description | Status |
|---------|-------------|--------|
| Gemini 3 Pro | Higher capability model | Available ($2/$12 per 1M) |
| Code execution | Server-side Python sandbox | Available |
| File API | Upload and analyze files | Available |
| Image generation | Imagen 3 | Available |
| Video understanding | Analyze video content | Available |
| Audio understanding | Transcription + analysis | Available |
| Context caching | Cache long contexts for reuse | Available |
| Batch API | 50% discount batch processing | Available |
| Embeddings | Text embeddings | Available |

### Potential Additions

- **Context caching** — could cache system prompts for cheaper repeated calls
- **Gemini 3 Pro** — upgrade path for better synthesis quality
- **Batch API** — retrospective analysis at reduced cost
- **File API** — could analyze financial PDFs/reports

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/online/runners/gemini_runner.py`](trader/online/runners/gemini_runner.py) | `run_gemini()` — main Gemini runner |
| [`trader/llm/pricing.py`](trader/llm/pricing.py) | `GEMINI_PRICING` table |
| [`trader/online/agent_pipeline.py`](trader/online/agent_pipeline.py) | Agent 3 spec + dispatch |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | 16 function tools available to Gemini |

---

## References

- [Gemini API Documentation](https://ai.google.dev/gemini-api/docs)
- [Gemini API Pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [google-genai Python SDK](https://github.com/googleapis/python-genai)
- [Grounding with Google Search](https://ai.google.dev/gemini-api/docs/grounding)
