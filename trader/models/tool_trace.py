"""ToolTrace schema.

We store tool traces as JSON-friendly dicts inside the Snapshot.
This module provides helpers to create consistent trace dictionaries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


@dataclass(frozen=True)
class TraceExecution:
    model: str
    start_time: str
    end_time: str
    cost_usd: float


def new_tool_trace(
    *,
    trace_id: str,
    hop_index: int,
    parent_trace_id: str | None,
    decision_context: dict[str, Any],
    action: dict[str, Any],
    execution: TraceExecution,
    results: list[dict[str, Any]],
    extracted_signals: dict[str, Any] | None = None,
    stop_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "hop_index": hop_index,
        "parent_trace_id": parent_trace_id,
        "decision_context": decision_context,
        "action": action,
        "execution": {
            "model": execution.model,
            "start_time": execution.start_time,
            "end_time": execution.end_time,
            "cost_usd": execution.cost_usd,
        },
        "results": results,
        "extracted_signals": extracted_signals or {},
        "stop_signal": stop_signal or {},
    }
