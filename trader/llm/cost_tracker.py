"""Cost tracking and budget enforcement.

This module is intentionally *loud* on budget violations.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

log = logging.getLogger(__name__)

from trader.llm.pricing import (
    estimate_token_cost_gemini,
    estimate_token_cost_grok,
    estimate_token_cost_openai,
    estimate_tool_cost,
)


ProviderName = Literal["openai", "grok", "gemini"]


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostTracker:
    max_daily_cost: float
    max_cost_per_item: float
    debug: bool = False
    daily_spent: float = 0.0
    item_spent: float = 0.0
    day: date = field(default_factory=date.today)
    daily_by_tool: dict[str, float] = field(default_factory=dict)
    item_by_tool: dict[str, float] = field(default_factory=dict)

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.daily_spent = 0.0
            self.daily_by_tool = {}

    def reset_item(self) -> None:
        """Reset the per-news-item accumulator."""
        self.item_spent = 0.0
        self.item_by_tool = {}

    def check_budget(self, estimated_cost: float) -> None:
        self._roll_day_if_needed()
        if self.daily_spent + estimated_cost > self.max_daily_cost:
            raise BudgetExceeded(
                f"Daily budget exceeded: spent={self.daily_spent:.4f} estimated_add={estimated_cost:.4f} max={self.max_daily_cost:.4f}"
            )

        if self.item_spent + estimated_cost > self.max_cost_per_item:
            raise BudgetExceeded(
                f"Per-item budget exceeded: spent={self.item_spent:.4f} estimated_add={estimated_cost:.4f} max={self.max_cost_per_item:.4f}"
            )

    def log_llm_call(
        self,
        *,
        provider: ProviderName,
        model: str,
        usage: dict[str, Any],
        tools_used: list[str],
        stage: str,
        purpose: str,
    ) -> float:
        """Record cost for an LLM call. Returns cost_usd.

        If the usage dict contains ``authoritative_cost_usd`` (e.g. from
        xAI's ``cost_in_usd_ticks``), that value is used directly.
        Otherwise falls back to estimating from token counts + tool fees.
        """

        # Prefer authoritative cost from the provider when available.
        auth_cost = usage.get("authoritative_cost_usd")
        if auth_cost and float(auth_cost) > 0:
            total = float(auth_cost)
        else:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cached_tokens = int(usage.get("cached_tokens") or 0)

            token_cost = 0.0
            if input_tokens or output_tokens:
                if provider == "openai":
                    token_cost = estimate_token_cost_openai(model, input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens).total_cost_usd
                elif provider == "grok":
                    token_cost = estimate_token_cost_grok(model, input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens).total_cost_usd
                elif provider == "gemini":
                    token_cost = estimate_token_cost_gemini(model, input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens).total_cost_usd
                else:
                    raise ValueError(f"Unknown provider: {provider}")

            tool_cost = 0.0
            for tool in tools_used:
                tool_cost += estimate_tool_cost(provider=provider, tool_name=tool, calls=1)

            total = float(token_cost + tool_cost)

        self.check_budget(total)
        self.daily_spent += total
        self.item_spent += total

        # Track per-tool cost breakdown.
        # When using authoritative cost, attribute everything to "llm_tokens"
        # since we can't reliably split token vs tool costs.
        if auth_cost and float(auth_cost) > 0:
            self.daily_by_tool["llm_tokens"] = self.daily_by_tool.get("llm_tokens", 0.0) + total
            self.item_by_tool["llm_tokens"] = self.item_by_tool.get("llm_tokens", 0.0) + total
        else:
            for tool in tools_used:
                add = estimate_tool_cost(
                    provider=provider, tool_name=tool, calls=1
                )
                self.daily_by_tool[tool] = self.daily_by_tool.get(tool, 0.0) + add
                self.item_by_tool[tool] = self.item_by_tool.get(tool, 0.0) + add
            llm_portion = max(0.0, total - sum(
                estimate_tool_cost(provider=provider, tool_name=t, calls=1)
                for t in tools_used
            ))
            if llm_portion > 0:
                self.daily_by_tool["llm_tokens"] = self.daily_by_tool.get("llm_tokens", 0.0) + llm_portion
                self.item_by_tool["llm_tokens"] = self.item_by_tool.get("llm_tokens", 0.0) + llm_portion

        if self.debug or os.getenv("DEBUG", "").lower() in ("1", "true"):
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            src = "auth" if auth_cost and float(auth_cost) > 0 else "est"
            log.debug(
                "COST stage=%s purpose=%s provider=%s model=%s "
                "tokens(in=%d,out=%d) tools=%s total=$%.4f(%s) daily=$%.4f/%.2f",
                stage, purpose, provider, model,
                input_tokens, output_tokens, tools_used,
                total, src, self.daily_spent, self.max_daily_cost,
            )
        return total
