Yes — you can track token usage in a PydanticAI agent loop, but **the model won’t “know” unless you explicitly feed it that information**.

### What’s wrong (or outdated) in that exchange

* PydanticAI **does** track usage during a run: token counts are **provided by the model/provider response**, and PydanticAI **sums them across requests**. ([Pydantic AI][1])
* That usage is accessible from the run object: `AgentRun.usage()` returns `RunUsage` “so far”. ([Pydantic AI][2])
* `UsageLimits` checks request limits **before** requests and token limits **after** responses (because token counts arrive in responses). ([Pydantic AI][1])

So: you *don’t* need to estimate via chars/tool output sizes unless your provider doesn’t return usage.

### What you can realistically do

#### 1) Track usage accurately (externally) during the loop

Use `agent.iter()` / `AgentRun` and poll `run.usage()` after each model response / step. That gives you:

* `input_tokens`, `output_tokens`, `total_tokens` (from `UsageBase`)
* plus `requests` and `tool_calls` ([Pydantic AI][1])

#### 2) Make the agent “budget-aware” by injecting a budget readout

Even if you can measure tokens, **tools won’t automatically see run usage via `RunContext`** (unless you pass it yourself). The clean pattern is:

* Maintain a `deps` object (or shared state) that you update after each step with:

  * tokens used so far (`run.usage().total_tokens`)
  * remaining tokens (`usage_limits.total_tokens_limit - used`, if you set a limit)
  * requests remaining (`usage_limits.request_limit - run.usage().requests`)
* Then expose that to the model via one (or more) of:

  * **Dynamic instructions** (prepend a short “Budget:” line each turn)
  * A **tool** like `budget_status()` that returns the current stats from `deps`
  * A forced “status” tool call every N steps (if you want it always visible)

This gives the model the missing “self-awareness” without hacks.

### A concrete, minimal approach that works

* Set `usage_limits=UsageLimits(total_tokens_limit=..., request_limit=..., tool_calls_limit=...)` so PydanticAI enforces hard stops. ([Pydantic AI][1])
* Run with `agent.iter(...)` and after each step:

  * read `u = run.usage()`
  * update `deps.budget = {...}`
* Provide a tool `budget_status(ctx)` that returns `deps.budget`
* Add one instruction line: “Before starting a new phase, call `budget_status` and adapt the plan if budget is low.”

### Practical caveats

* **Accuracy depends on the provider** returning usage. If a backend doesn’t return token usage reliably, then you fall back to approximations (your “chars/4” idea), but treat it as a heuristic.
* “Token limit” is ambiguous:

  * **per-request context window** (model max context)
  * **per-run budget** (what `UsageLimits.total_tokens_limit` enforces)
    Decide which one you care about; PydanticAI’s usage limits are naturally aligned with **per-run budget**.

If you tell me which provider/model you’re using under PydanticAI (OpenAI, Anthropic, Gemini, Groq, etc.), I can be more specific about whether the usage numbers are reliably available and where they show up in the run events.

[1]: https://ai.pydantic.dev/api/usage/ "pydantic_ai.usage - Pydantic AI"
[2]: https://ai.pydantic.dev/api/agent/ "pydantic_ai.agent - Pydantic AI"

---

Yes—**with OpenAI, Gemini, and Groq you can usually track token usage inside a PydanticAI loop accurately**, because PydanticAI aggregates token counts from each provider’s response into `RunUsage`, and `UsageLimits` enforces limits based on that. ([Pydantic AI][1])

### What works reliably (OpenAI + Groq)

* **OpenAI**: PydanticAI’s OpenAI support is first-class; usage totals are accumulated per run and limits are checked after each response. ([Pydantic AI][1])
* **Groq**: Groq’s OpenAI-compatible chat endpoint returns a `usage` object in responses, so token tracking is straightforward. ([GroqCloud][2])

### Gemini: usually works, but expect edge-case roughness

Gemini token usage can be available, but there have been real-world failure modes where “token-limit-like” situations lead to weird exceptions rather than clean `UsageLimitExceeded` (at least in some versions/configs). ([GitHub][3])
So: token accounting may still work, but **budget enforcement and error semantics can be messier** than OpenAI/Groq.

### The clean pattern (works across providers)

1. Run the agent with an **iterative interface** so you can observe progress.
2. After each model step/response, read the run usage (`run.usage()` / `result.usage()` depending on the API you’re using) and update your own shared state (`deps`).
3. Make the model “aware” by injecting a small budget line (or exposing a `budget_status()` tool it can call).

Minimal sketch:

```python
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

limits = UsageLimits(total_tokens_limit=40_000, request_limit=50)

agent = Agent(model=..., usage_limits=limits)

run = agent.iter("start task", deps=deps)
async for _ in run:
    u = run.usage()              # tokens/requests so far
    deps.budget = {
        "total_tokens": u.total_tokens,
        "requests": u.requests,
        "tool_calls": u.tool_calls,
    }
```

This avoids the “estimate from tool output size” hack entirely for OpenAI/Groq, and likely for Gemini when usage is present. ([Pydantic AI][1])

### Two practical warnings

* **Token counts can be “surprising”** vs what you think you sent (tool schema, structured output scaffolding, hidden/provider-specific formatting, etc.). There are open issues where reported usage doesn’t match expectations in some scenarios. ([GitHub][4])
* You still need a fallback heuristic (chars/4, or just request-count) **only** for cases where a provider fails to return usage (or a wrapper drops it). For Groq/OpenAI this is uncommon; for Gemini you’ll hit more edge cases.

### Bottom line

* **Do it “for real”**: rely on `RunUsage` + `agent.iter()`/run object, update `deps`, and optionally expose a `budget_status()` tool.
* **OpenAI and Groq**: should be solid.
* **Gemini**: workable, but plan for occasional odd errors around limits / response shapes. ([GitHub][3])

[1]: https://ai.pydantic.dev/api/usage/?utm_source=chatgpt.com "pydantic_ai.usage"
[2]: https://console.groq.com/docs/api-reference?utm_source=chatgpt.com "API Reference - GroqDocs"
[3]: https://github.com/pydantic/pydantic-ai/issues/2021?utm_source=chatgpt.com "Incorrect exception type returned for gemini when token ..."
[4]: https://github.com/pydantic/pydantic-ai/issues/2567?utm_source=chatgpt.com "Token Counting · Issue #2567 · pydantic/pydantic-ai"
