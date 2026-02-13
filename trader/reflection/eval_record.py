"""Build a nested decision-tree (EvalRecord) from snapshot + optional watch.

The EvalRecord is the canonical data structure driving both:
- The dashboard "Pipeline Timeline" accordion (Jinja rendering)
- The LLM evaluator prompt (markdown serialization)

Each node: {"type": str, "summary": str, "detail": dict, "children": list[node]}
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_eval_record(
    snapshot: dict[str, Any],
    watch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transform a snapshot dict + optional watch dict into a nested tree."""
    trigger = snapshot.get("trigger") or {}
    nodes: list[dict[str, Any]] = []

    # 1. Triage
    nodes.append(_triage_node(snapshot.get("triage") or {}))

    # 2. Agent rounds (with tool calls as children)
    for r in snapshot.get("rounds") or []:
        nodes.append(_round_node(r))

    # 3. Prediction
    pred = snapshot.get("prediction") or {}
    if pred:
        nodes.append(_prediction_node(pred))

    # 4. Watch lifecycle (if linked)
    if watch is not None:
        nodes.extend(_watch_nodes(watch))

    return {
        "snapshot_id": snapshot.get("snapshot_id", ""),
        "headline": trigger.get("headline", ""),
        "symbols": trigger.get("symbols", []),
        "created_at": snapshot.get("created_at", ""),
        "cost_summary": snapshot.get("cost_summary") or {},
        "nodes": nodes,
    }


def eval_record_to_markdown(record: dict[str, Any]) -> str:
    """Serialize an eval record to markdown for the LLM evaluator."""
    lines: list[str] = []
    lines.append(f"# Snapshot {record.get('snapshot_id', '?')}")
    lines.append(f"**Headline:** {record.get('headline', '?')}")
    syms = ", ".join(record.get("symbols", []))
    lines.append(f"**Symbols:** {syms}")
    lines.append(f"**Created:** {record.get('created_at', '?')}")
    cost = record.get("cost_summary") or {}
    if cost.get("total_usd"):
        lines.append(f"**Cost:** ${cost['total_usd']:.4f}")
    lines.append("")

    for node in record.get("nodes", []):
        _render_node_md(node, depth=0, lines=lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------


def _triage_node(triage: dict[str, Any]) -> dict[str, Any]:
    if not triage:
        return {
            "type": "triage",
            "summary": "[not captured]",
            "detail": {},
            "children": [],
        }
    action = triage.get("action", "?")
    conf = triage.get("confidence", 0)
    return {
        "type": "triage",
        "summary": f"{action} ({conf*100:.0f}%)",
        "detail": triage,
        "children": [],
    }


def _round_node(r: dict[str, Any]) -> dict[str, Any]:
    agent = r.get("agent", "?")
    model = r.get("model", "?")
    usage = r.get("usage") or {}
    elapsed = r.get("elapsed_s", 0)
    cost_usd = r.get("cost_usd", 0.0)
    tool_calls = usage.get("tool_calls", 0)
    total_tok = usage.get("total_tokens", 0)

    tok_str = f"{total_tok // 1000}k" if total_tok >= 1000 else str(total_tok)
    summary = f"{agent} | {tool_calls} tools | {tok_str} tokens | ${cost_usd:.4f} | {elapsed:.1f}s"

    # Build child nodes from tool traces
    children = []
    for trace in r.get("tool_traces") or []:
        children.append(_tool_call_node(trace))

    detail = {
        "agent": agent,
        "model": model,
        "round": r.get("round"),
        "system_prompt": r.get("system_prompt", "[not captured]"),
        "user_message": r.get("user_message", "[not captured]"),
        "findings": r.get("findings", ""),
        "usage": usage,
        "cost_usd": cost_usd,
        "elapsed_s": elapsed,
    }

    return {
        "type": "agent_round",
        "summary": summary,
        "detail": detail,
        "children": children,
    }


def _tool_call_node(trace: dict[str, Any]) -> dict[str, Any]:
    action = trace.get("action") or {}
    execution = trace.get("execution") or {}
    tool = action.get("tool", "?")
    args = action.get("args") or {}
    error = trace.get("error")
    status = "err" if error else "ok"
    duration = execution.get("duration_s", 0)

    # Build a concise summary from tool name + first meaningful arg
    arg_hint = _tool_arg_hint(tool, args)
    summary = f"{tool}{arg_hint} ({status})"

    return {
        "type": "tool_call",
        "summary": summary,
        "detail": {
            "tool": tool,
            "args": args,
            "raw_output": trace.get("raw_tool_output"),
            "error": error,
            "duration_s": duration,
            "cost_usd": execution.get("cost_usd", 0.0),
            "modality": trace.get("modality", "other"),
        },
        "children": [],
    }


def _tool_arg_hint(tool: str, args: dict[str, Any]) -> str:
    """Extract a short hint from tool args for the summary line."""
    if not args:
        return ""
    # Common patterns
    for key in ("query", "symbol", "url", "keyword", "ticker"):
        val = args.get(key)
        if val is not None:
            s = str(val)
            if len(s) > 40:
                s = s[:37] + "..."
            return f" → '{s}'"
    # Fallback: first arg value
    first_val = str(next(iter(args.values())))
    if len(first_val) > 40:
        first_val = first_val[:37] + "..."
    return f" → '{first_val}'"


def _prediction_node(pred: dict[str, Any]) -> dict[str, Any]:
    direction = pred.get("direction", "?")
    conf = pred.get("confidence", 0)
    horizon = pred.get("horizon", "?")
    summary = f"{direction} {conf*100:.0f}% — {horizon} horizon"

    return {
        "type": "prediction",
        "summary": summary,
        "detail": pred,
        "children": [],
    }


# ---------------------------------------------------------------------------
# Watch nodes
# ---------------------------------------------------------------------------


def _watch_nodes(watch: dict[str, Any]) -> list[dict[str, Any]]:
    """Build watch_entry, watch_checkin*, and watch_exit nodes."""
    nodes: list[dict[str, Any]] = []
    entry = watch.get("entry") or {}
    exit_data = watch.get("exit")
    status = watch.get("status", "?")

    # Watch entry
    price = entry.get("price", 0)
    direction = entry.get("direction", "?")
    conf = entry.get("confidence", 0)
    nodes.append({
        "type": "watch_entry",
        "summary": f"{watch.get('symbol', '?')} @ ${price:.2f} {direction}",
        "detail": entry,
        "children": _checkin_children(watch),
    })

    # Watch exit (if exited)
    if exit_data:
        exit_pnl = exit_data.get("realized_pnl_pct", 0)
        reason = exit_data.get("reason", "?")
        nodes.append({
            "type": "watch_exit",
            "summary": f"{exit_pnl:+.2f}% — {_truncate(reason, 50)}",
            "detail": exit_data,
            "children": [],
        })

    # Retrospective summary (if present)
    retro = watch.get("retrospective_data")
    if retro:
        mfe = retro.get("mfe_pct", 0)
        mae = retro.get("mae_pct", 0)
        nodes.append({
            "type": "retrospective",
            "summary": f"MFE {mfe:+.2f}% / MAE {mae:+.2f}%",
            "detail": retro,
            "children": [],
        })

    return nodes


def _checkin_children(watch: dict[str, Any]) -> list[dict[str, Any]]:
    """Build child nodes from checkin_history."""
    children = []
    for ci in watch.get("checkin_history") or []:
        action = ci.get("action", "?")
        pnl = ci.get("pnl_pct", 0)
        depth = ci.get("depth", "?")
        reason = ci.get("reason") or ""
        summary = f"{action} — {pnl:+.2f}% ({depth})"
        if reason:
            summary += f" {_truncate(reason, 40)}"
        children.append({
            "type": "watch_checkin",
            "summary": summary,
            "detail": ci,
            "children": [],
        })
    return children


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_node_md(
    node: dict[str, Any], depth: int, lines: list[str]
) -> None:
    """Recursively render a node tree to markdown lines."""
    indent = "  " * depth
    prefix = "#" * min(depth + 2, 6)  # ## for depth 0, ### for depth 1, etc.
    ntype = node.get("type", "?")
    summary = node.get("summary", "")

    lines.append(f"{indent}{prefix} [{ntype}] {summary}")

    detail = node.get("detail") or {}
    if detail:
        # Render selected detail fields (skip bulky raw_output in tool_calls)
        for key, val in detail.items():
            if key == "raw_output":
                # Truncate raw output to keep prompt manageable
                val_str = str(val)
                if len(val_str) > 500:
                    val_str = val_str[:500] + "... [truncated]"
                lines.append(f"{indent}- **{key}:** {val_str}")
            elif key in ("system_prompt", "user_message", "findings"):
                val_str = str(val)
                if len(val_str) > 1000:
                    val_str = val_str[:1000] + "... [truncated]"
                lines.append(f"{indent}- **{key}:** {val_str}")
            else:
                lines.append(f"{indent}- **{key}:** {val}")
    lines.append("")

    for child in node.get("children") or []:
        _render_node_md(child, depth + 1, lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
