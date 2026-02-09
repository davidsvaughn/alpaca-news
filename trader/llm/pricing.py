"""Model/tool pricing tables and helpers.

Notes
-----
- Rates are **USD per 1M tokens** unless stated otherwise.
- Tool costs are **per call**.
- These are meant to be *operational estimates* for budgeting and monitoring.
- Providers change pricing; treat this module as the single source of truth in
  the codebase and update it when your vendor pricing changes.

The user provided these initial tables on 2026-02-09.

Gemini Google Search grounding pricing is left as a placeholder until you
confirm the correct rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OPENAI_PRICING: dict[str, dict[str, float]] = {
    "o3": {"input": 2.00, "output": 8.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "o4-mini-deep-research": {"input": 2.00, "output": 8.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5.1": {"input": 1.25, "output": 10.00},
    "gpt-5.1-chat-latest": {"input": 1.25, "output": 10.00},
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "web_search": {"per_call": 0.01},
}


GROK_PRICING: dict[str, dict[str, float]] = {
    "grok-4.1-fast-reasoning": {"input": 0.20, "output": 0.50},
    "grok-4.1-fast-non-reasoning": {"input": 0.20, "output": 0.50},
    "grok-4-fast-reasoning": {"input": 0.20, "output": 0.50},
    "grok-4-fast-non-reasoning": {"input": 0.20, "output": 0.50},
    "grok-code-fast-1": {"input": 0.20, "output": 1.50},
    "web_search": {"per_call": 0.005},
    "x_search": {"per_call": 0.005},
}


# Gemini Pricing (Per 1M Tokens - USD) - Tiered pricing based on input token count:
# - "input_low": rate for ≤200K input tokens
# - "input_high": rate for >200K input tokens
# - "output": output token rate (flat)
GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-3-pro-preview": {"input_low": 2.00, "input_high": 4.00, "output": 12.00},
    "gemini-3-flash-preview": {"input_low": 0.50, "input_high": 0.50, "output": 3.00},
    "gemini-2.5-pro": {"input_low": 1.25, "input_high": 2.50, "output": 10.00},
    "gemini-2.5-flash": {"input_low": 0.30, "input_high": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input_low": 0.10, "input_high": 0.10, "output": 0.40},
    "gemini-2.0-flash": {"input_low": 0.10, "input_high": 0.10, "output": 0.40},
    # Placeholder (pricing may not actually be per-call; confirm via Google pricing pages)
    "GoogleSearch": {"per_call": 0.01},
}


ProviderName = Literal["openai", "grok", "gemini"]


@dataclass(frozen=True)
class CostBreakdown:
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    tool_cost_usd: float = 0.0

    @property
    def total_cost_usd(self) -> float:
        return self.input_cost_usd + self.output_cost_usd + self.tool_cost_usd


def cost_per_million_to_per_token(rate_per_million: float) -> float:
    return rate_per_million / 1_000_000.0


def estimate_token_cost_openai(model: str, *, input_tokens: int, output_tokens: int) -> CostBreakdown:
    if model not in OPENAI_PRICING:
        raise KeyError(f"Unknown OpenAI model pricing: {model}")
    rates = OPENAI_PRICING[model]
    if "input" not in rates or "output" not in rates:
        raise ValueError(f"OpenAI pricing for model {model} missing input/output rates")
    input_cost = input_tokens * cost_per_million_to_per_token(rates["input"])
    output_cost = output_tokens * cost_per_million_to_per_token(rates["output"])
    return CostBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
    )


def estimate_token_cost_grok(model: str, *, input_tokens: int, output_tokens: int) -> CostBreakdown:
    if model not in GROK_PRICING:
        raise KeyError(f"Unknown Grok model pricing: {model}")
    rates = GROK_PRICING[model]
    if "input" not in rates or "output" not in rates:
        raise ValueError(f"Grok pricing for model {model} missing input/output rates")
    input_cost = input_tokens * cost_per_million_to_per_token(rates["input"])
    output_cost = output_tokens * cost_per_million_to_per_token(rates["output"])
    return CostBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
    )


def estimate_token_cost_gemini(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    input_pricing_tier: Literal["input_low", "input_high"] = "input_low",
) -> CostBreakdown:
    if model not in GEMINI_PRICING:
        raise KeyError(f"Unknown Gemini model pricing: {model}")
    rates = GEMINI_PRICING[model]
    if input_pricing_tier not in rates or "output" not in rates:
        raise ValueError(
            f"Gemini pricing for model {model} missing tier {input_pricing_tier} or output rate"
        )
    input_cost = input_tokens * cost_per_million_to_per_token(rates[input_pricing_tier])
    output_cost = output_tokens * cost_per_million_to_per_token(rates["output"])
    return CostBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
    )


def estimate_tool_cost(*, provider: ProviderName, tool_name: str, calls: int = 1) -> float:
    if calls < 0:
        raise ValueError("calls must be >= 0")

    pricing = {
        "openai": OPENAI_PRICING,
        "grok": GROK_PRICING,
        "gemini": GEMINI_PRICING,
    }[provider]

    if tool_name not in pricing or "per_call" not in pricing[tool_name]:
        raise KeyError(f"Unknown per-call tool pricing: provider={provider} tool={tool_name}")
    return float(pricing[tool_name]["per_call"]) * calls
