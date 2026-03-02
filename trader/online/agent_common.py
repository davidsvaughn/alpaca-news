"""Shared types and helpers for the native-SDK agent runners.

Framework-agnostic dataclasses used by all three runners
(OpenAI, Grok/xAI, Gemini) and consumed by the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


class TradingSignal(BaseModel):
    """Structured trading signal produced by the exploration pipeline."""
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 = no confidence, 1.0 = certain")
    horizon: Literal["1d", "1w"]
    magnitude_estimate: str = Field(description="Expected price move, e.g. '0.5-1.5%'")
    key_catalyst: str = Field(description="One-sentence summary of the main catalyst")
    bull_case: str = Field(description="Brief bull case argument")
    bear_case: str = Field(description="Brief bear case argument")
    risk_factors: list[str] = Field(description="Key risk factors that could invalidate the thesis")

# Modality classification — maps tool names to data categories.
# Kept here (single source of truth) so runners don't depend on explorer_agent.
TOOL_MODALITY: dict[str, str] = {
    "check_price": "market_data",
    "get_price_history": "market_data",
    "check_price_spike": "market_data",
    "check_volume_regime": "market_data",
    "check_market_context": "macro",
    "check_options_activity": "market_data",
    "get_fundamentals": "fundamentals",
    "get_financial_statements": "fundamentals",
    "get_movers": "market_data",
    "get_technical_indicators": "market_data",
    "check_insider_activity": "fundamentals",
    "get_company_news": "news",
    "get_analyst_ratings": "fundamentals",
    "url_fetch": "web_research",
    "web_search": "web_research",
    "x_search": "social",
    "x_stream_cache": "social",
}


@dataclass
class ToolDef:
    """Definition of a callable tool for the native-SDK runners."""

    name: str
    func: Callable[..., str]   # (market, **kwargs) -> str  (or custom signature)
    description: str
    parameters: dict[str, Any]  # JSON Schema for the function parameters
    modality: str = "other"


@dataclass
class AgentRunResult:
    """Result returned by every runner, consumed by the pipeline."""

    output: Any                              # str (intermediate) or TradingSignal (final)
    tool_traces: list[dict[str, Any]]        # trace dicts in SnapshotBuilder format
    usage: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
        "tool_calls": 0,
        "reasoning_tokens": 0,
    })
    thinking_summary: str | None = None
    error: dict[str, str] | None = None
    model_used: str | None = None            # actual model (may differ from spec after fallback)


def build_trace_dict(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    error: str | None,
    start: float,
    end: float,
    hop_index: int,
    cost_usd: float = 0.0,
    builtin: bool = False,
) -> dict[str, Any]:
    """Produce a trace dict in the format SnapshotBuilder.add_tool_trace() expects."""
    raw_output: Any = None
    if error is None and result is not None:
        raw_output = _safe_serialize(result)

    return {
        "trace_id": f"trace_{hop_index}",
        "hop_index": hop_index,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "modality": TOOL_MODALITY.get(tool_name, "other"),
        "action": {
            "tool": tool_name,
            "args": args,
        },
        "execution": {
            "start_time": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "end_time": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(),
            "duration_s": round(end - start, 3),
            "cost_usd": cost_usd,
        },
        "raw_tool_output": raw_output,
        "error": error,
        "builtin": builtin,
    }


def _safe_serialize(obj: Any) -> Any:
    """Attempt to serialize an object for trace storage."""
    if obj is None:
        return None
    if isinstance(obj, str):
        try:
            return json.loads(obj)
        except (json.JSONDecodeError, TypeError):
            return obj
    if isinstance(obj, (dict, list, int, float, bool)):
        return obj
    return str(obj)
