"""Build a nested decision-tree (EvalRecord) from snapshot + optional watch.

The EvalRecord is the canonical data structure driving both:
- The dashboard "Pipeline Timeline" accordion (Jinja rendering)
- The LLM evaluator prompt (markdown serialization)

Also provides ``snapshot_export_to_markdown`` for full-fidelity markdown export.

Each node: {"type": str, "summary": str, "detail": dict, "children": list[node]}
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_eval_record(
    snapshot: dict[str, Any],
    watch: dict[str, Any] | None = None,
    *,
    follow_ups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Transform a snapshot dict + optional watch dict + follow-ups into a nested tree."""
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

    # 5. Follow-up data collections
    for fu in follow_ups or []:
        nodes.extend(_follow_up_nodes(fu))

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
# Follow-up nodes
# ---------------------------------------------------------------------------


def _follow_up_nodes(fu: dict[str, Any]) -> list[dict[str, Any]]:
    """Build follow_up parent node with collection children."""
    fu_json = fu.get("follow_up_json") or fu  # DB row vs raw dict
    if isinstance(fu_json, str):
        import json
        fu_json = json.loads(fu_json)

    reason = fu_json.get("reason", "?")
    status = fu_json.get("status", "?")
    n_collections = len(fu_json.get("collections", []))
    total_cost = fu_json.get("total_cost_usd", 0.0)
    symbols = ", ".join(fu_json.get("symbols", []))

    children: list[dict[str, Any]] = []
    for c in fu_json.get("collections", []):
        offset = c.get("offset_label", "?")
        cost = c.get("cost_usd", 0.0)
        n_web = len(c.get("web_results", []))
        n_x = len(c.get("x_results", []))
        price_syms = list(c.get("price", {}).keys())
        price_hint = ", ".join(price_syms) if price_syms else "none"

        children.append({
            "type": "follow_up_collection",
            "summary": f"{offset} | {n_web} web + {n_x} x | ${cost:.4f}",
            "detail": {
                "offset_label": offset,
                "collected_at": c.get("collected_at", ""),
                "price_symbols": price_hint,
                "web_queries": [r.get("query", "") for r in c.get("web_results", [])],
                "x_queries": [r.get("query", "") for r in c.get("x_results", [])],
                "cost_usd": cost,
            },
            "children": [],
        })

    return [{
        "type": "follow_up",
        "summary": f"{reason} | {symbols} | {n_collections} collections | ${total_cost:.4f} | {status}",
        "detail": {
            "follow_up_id": fu_json.get("follow_up_id", ""),
            "reason": reason,
            "status": status,
            "schedule": fu_json.get("schedule", []),
            "started_at": fu_json.get("started_at", ""),
            "total_cost_usd": total_cost,
        },
        "children": children,
    }]


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


# ===========================================================================
# Full-fidelity markdown export (for human / LLM evaluation)
# ===========================================================================


def snapshot_export_to_markdown(
    snapshot: dict[str, Any],
    watch: dict[str, Any] | None = None,
    follow_ups: list[dict[str, Any]] | None = None,
) -> str:
    """Generate markdown export aligned with the current snapshot detail page."""
    lines: list[str] = []
    trigger = snapshot.get("trigger") or {}
    symbols = trigger.get("symbols") or []
    triage = snapshot.get("triage") or {}
    rounds = snapshot.get("rounds") or []
    pred = snapshot.get("prediction") or {}
    cost = snapshot.get("cost_summary") or {}

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    lines.append(f"# Snapshot Detail Export: {snapshot.get('snapshot_id', '?')}")
    lines.append("")
    lines.append(f"**Headline:** {trigger.get('headline', '?')}")
    lines.append(f"**Symbols:** {', '.join(symbols)}")
    lines.append(f"**Created:** {snapshot.get('created_at', '?')}")
    lines.append(f"**Version:** {snapshot.get('version', '?')}")
    if cost.get("total_usd") is not None:
        lines.append(f"**Total Cost:** ${cost['total_usd']:.4f}")
    if pred.get("direction"):
        conf = pred.get("confidence", 0)
        lines.append(f"**Direction:** {pred['direction']} ({conf*100:.0f}% confidence)")
    lines.append("")

    # ------------------------------------------------------------------
    # Pipeline Execution Summary
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Pipeline Execution Summary")
    lines.append("")
    lines.append("| # | Stage | Model | Tools | Tokens (in/out) | Reqs | Cost | Time |")
    lines.append("|---|-------|-------|------:|----------------:|-----:|-----:|-----:|")

    triage_usage = triage.get("usage") or {}
    triage_provider = triage.get("provider", "?")
    triage_model = triage.get("model", "?")
    triage_is_prefilter = triage_provider == "pre-filter" or triage_model == "keyword"
    triage_tools = int(triage_usage.get("tool_calls", 0) or 0)
    triage_in = int(triage_usage.get("input_tokens", 0) or 0)
    triage_out = int(triage_usage.get("output_tokens", 0) or 0)
    triage_total = int(triage_usage.get("total_tokens", triage_in + triage_out) or 0)
    triage_reason = int(triage_usage.get("reasoning_tokens", 0) or 0)
    triage_reqs_raw = triage.get("requests")
    if triage_reqs_raw is None:
        triage_reqs = 0 if triage_is_prefilter else (1 if triage else 0)
    else:
        triage_reqs = int(triage_reqs_raw or 0)
    triage_cost_saved = float(triage.get("cost_usd", 0.0) or 0.0)
    round_cost_sum = sum(float((r.get("cost_usd", 0.0) or 0.0)) for r in rounds)
    triage_cost = triage_cost_saved
    if triage_cost <= 0 and triage_reqs > 0 and cost.get("total_usd") is not None:
        fallback = float(cost.get("total_usd", 0.0) or 0.0) - round_cost_sum
        if fallback > 0:
            triage_cost = fallback
    triage_time = float(triage.get("elapsed_s", 0.0) or 0.0)

    if triage:
        stage_label = "triage"
        if triage.get("action"):
            stage_label += f" ({triage.get('action')})"
        model_label = f"`{triage_provider} / {triage_model}`"
        tok_label = f"{triage_in:,} / {triage_out:,}"
        if triage_reason:
            tok_label += f" ({triage_reason:,} reasoning)"
        lines.append(
            f"| T | {stage_label} | {model_label} | {triage_tools} | {tok_label} | "
            f"{triage_reqs} | ${triage_cost:.4f} | {triage_time:.1f}s |"
        )

    total_tools = triage_tools
    total_tokens = triage_total
    total_reasoning = triage_reason
    total_reqs = triage_reqs
    total_time = triage_time
    total_cost = triage_cost

    for i, r in enumerate(rounds, 1):
        u = r.get("usage") or {}
        tc = int(u.get("tool_calls", 0) or 0)
        inp = int(u.get("input_tokens", 0) or 0)
        out = int(u.get("output_tokens", 0) or 0)
        rtok = int(u.get("reasoning_tokens", 0) or 0)
        reqs = int(u.get("requests", 0) or 0)
        rcost = float(r.get("cost_usd", 0) or 0.0)
        rtime = float(r.get("elapsed_s", 0) or 0.0)
        tok_label = f"{inp:,} / {out:,}"
        if rtok:
            tok_label += f" ({rtok:,} reasoning)"
        lines.append(
            f"| {i} | {r.get('agent', '?')} | `{r.get('model', '?')}` | {tc} | {tok_label} | "
            f"{reqs} | ${rcost:.4f} | {rtime:.1f}s |"
        )

        total_tools += tc
        total_tokens += int(u.get("total_tokens", inp + out) or 0)
        total_reasoning += rtok
        total_reqs += reqs
        total_time += rtime
        total_cost += rcost

    total_cost_display = float(cost.get("total_usd", total_cost) or 0.0)
    total_tok_label = f"{total_tokens:,}"
    if total_reasoning:
        total_tok_label += f" ({total_reasoning:,} reasoning)"
    lines.append(
        f"|  | **Total** |  | **{total_tools}** | **{total_tok_label}** | "
        f"**{total_reqs}** | **${total_cost_display:.4f}** | **{total_time:.1f}s** |"
    )
    lines.append("")

    # ------------------------------------------------------------------
    # Trade Action
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Trade Action")
    lines.append("")
    if watch:
        entry = watch.get("entry") or {}
        _kv(lines, "Watch ID", watch.get("watch_id"))
        _kv(lines, "Symbol", watch.get("symbol"))
        _kv(lines, "Direction", entry.get("direction"))
        if entry.get("price") is not None:
            _kv(lines, "Entry Price", f"${entry['price']:.2f}")
        if entry.get("confidence") is not None:
            _kv(lines, "Confidence", f"{entry['confidence']*100:.0f}%")
        _kv(lines, "Status", watch.get("status"))
    else:
        lines.append("- No watch linked. Manual override (BUY/SELL) available in dashboard UI.")
        if pred.get("direction") and pred.get("direction") != "neutral":
            lines.append(
                f"- Pipeline signal: **{pred.get('direction')}** @ "
                f"**{(pred.get('confidence', 0)*100):.0f}%**"
            )
    lines.append("")

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    prior_marker = "\n\n---\n\n## Prior agent findings\n"
    if rounds:
        first_msg = rounds[0].get("user_message", "") or ""
        shared = first_msg.split(prior_marker, 1)[0] if prior_marker in first_msg else first_msg
        lines.append("### Shared Base Input (all agents)")
        lines.append("")
        if shared.strip():
            lines.append(_normalize_news_event_md(shared.strip()))
        else:
            lines.append("_Not captured._")
        lines.append("")

        if len(rounds) > 1:
            lines.append("### Additional Input for Downstream Agents")
            lines.append("")
            for r in rounds[1:]:
                agent = r.get("agent", "agent")
                msg = r.get("user_message", "") or ""
                if prior_marker in msg:
                    extra = "## Prior agent findings\n" + msg.split(prior_marker, 1)[1]
                else:
                    extra = msg
                lines.append(f"#### {agent} additional context")
                lines.append("")
                if extra.strip():
                    lines.append(_normalize_news_event_md(extra.strip()))
                else:
                    lines.append("_No additional downstream context captured._")
                lines.append("")
    else:
        lines.append("_No agent rounds captured._")
        lines.append("")

    # ------------------------------------------------------------------
    # Pipeline Stages
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Pipeline Stages")
    lines.append("")

    if triage:
        lines.append("### triage")
        lines.append("")
        _kv(lines, "Action", triage.get("action"))
        _kv(lines, "Confidence", f"{(triage.get('confidence', 0) * 100):.0f}%")
        _kv(lines, "Provider", triage_provider)
        _kv(lines, "Model", triage_model)
        _kv(lines, "Requests", triage_reqs)
        _kv(lines, "Tokens", f"{triage_in:,} / {triage_out:,}")
        if triage_reason:
            _kv(lines, "Reasoning Tokens", f"{triage_reason:,}")
        _kv(lines, "Cost", f"${triage_cost:.4f}")
        _kv(lines, "Elapsed", f"{triage_time:.3f}s")
        _kv(lines, "Symbols", ", ".join(triage.get("symbols", []) or []))
        if triage.get("skip_patterns_learned"):
            _kv(lines, "Skip Patterns Learned", ", ".join(triage["skip_patterns_learned"]))
        _kv(lines, "Reasoning", triage.get("reasoning"))
        lines.append("")

    for idx, r in enumerate(rounds, 1):
        u = r.get("usage") or {}
        lines.append(f"### agent_round: {r.get('agent', f'agent_{idx}')}")
        lines.append("")
        _kv(lines, "Model", f"`{r.get('model', '?')}`")
        _kv(lines, "Requests", u.get("requests", 0))
        _kv(lines, "Tool Calls", u.get("tool_calls", 0))
        _kv(lines, "Tokens", f"{int(u.get('input_tokens', 0) or 0):,} / {int(u.get('output_tokens', 0) or 0):,}")
        if u.get("reasoning_tokens"):
            _kv(lines, "Reasoning Tokens", f"{int(u.get('reasoning_tokens', 0) or 0):,}")
        _kv(lines, "Cost", f"${float(r.get('cost_usd', 0) or 0.0):.4f}")
        _kv(lines, "Elapsed", f"{float(r.get('elapsed_s', 0) or 0.0):.1f}s")
        lines.append("")

        signal = r.get("signal") or {}
        if signal:
            lines.append("#### Agent Prediction")
            lines.append("")
            _kv(lines, "Direction", signal.get("direction"))
            if signal.get("confidence") is not None:
                conf_val = float(signal.get("confidence", 0) or 0)
                _kv(lines, "Confidence", f"{conf_val*100:.0f}% ({conf_val})")
            _kv(lines, "Horizon", signal.get("horizon"))
            _kv(lines, "Magnitude Estimate", signal.get("magnitude_estimate"))
            _kv(lines, "Key Catalyst", signal.get("key_catalyst"))
            _kv(lines, "Bull Case", signal.get("bull_case"))
            _kv(lines, "Bear Case", signal.get("bear_case"))
            risks = [x for x in (signal.get("risk_factors") or []) if x != "a"]
            if risks:
                lines.append("- **Risk Factors:**")
                for risk in risks:
                    lines.append(f"  - {risk}")
            lines.append("")
        elif r.get("findings"):
            lines.append("#### Agent Findings")
            lines.append("")
            lines.append(str(r.get("findings", "")).strip())
            lines.append("")

        if r.get("thinking_summary"):
            lines.append("#### Reasoning Summary")
            lines.append("")
            lines.append(str(r.get("thinking_summary", "")).strip())
            lines.append("")

        lines.append("#### Usage")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(u, indent=2, default=str, ensure_ascii=False))
        lines.append("```")
        lines.append("")

        lines.append("#### Prompts")
        lines.append("")
        lines.append("##### System Prompt")
        lines.append("")
        lines.append(str(r.get("system_prompt", "") or "").strip())
        lines.append("")
        lines.append("##### User Message")
        lines.append("")
        lines.append(_normalize_news_event_md(str(r.get("user_message", "") or "").strip()))
        lines.append("")

        lines.append("#### Raw Output")
        lines.append("")
        lines.append(str(r.get("raw_output", "") or "").strip())
        lines.append("")

        traces = r.get("tool_traces") or []
        lines.append(f"#### Tool Calls ({len(traces)})")
        lines.append("")
        if traces:
            for j, t in enumerate(traces, 1):
                _render_tool_trace_md(lines, t, j)
        else:
            lines.append("_No tool calls recorded for this round._")
            lines.append("")

    # Include non-round timeline nodes (watch/follow-up/etc.) for parity with UI
    record = build_eval_record(snapshot, watch, follow_ups=follow_ups)
    extra_nodes = [
        n for n in (record.get("nodes") or [])
        if n.get("type") not in ("triage", "agent_round", "prediction")
    ]
    if extra_nodes:
        lines.append("### Additional Stages")
        lines.append("")
        for node in extra_nodes:
            _render_node_md(node, depth=0, lines=lines)
        lines.append("")

    # ------------------------------------------------------------------
    # Trigger + Market + Price
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Trigger")
    lines.append("")
    if trigger.get("summary"):
        lines.append(str(trigger["summary"]))
        lines.append("")
    _kv(lines, "Source", trigger.get("source"))
    _kv(lines, "Type", trigger.get("type"))
    _kv(lines, "Article Time", trigger.get("timestamp") or trigger.get("alpaca_timestamp"))
    _kv(lines, "Source File", trigger.get("source_file"))
    raw = trigger.get("raw") or {}
    if raw.get("url"):
        _kv(lines, "Article URL", raw["url"])
    lines.append("")

    mc = snapshot.get("market_context") or {}
    if mc:
        lines.append("## Market Context")
        lines.append("")
        session = mc.get("session", "closed")
        market_open = mc.get("market_is_open", False)
        _kv(lines, "Session", f"{session} ({'open' if market_open else 'closed'})")
        if mc.get("spy_last") is not None:
            spy_str = f"${mc['spy_last']:.2f}"
            if mc.get("spy_net_pct_change") is not None:
                spy_str += f" ({mc['spy_net_pct_change']:+.2f}%)"
            _kv(lines, "SPY", spy_str)
        if mc.get("vix_level") is not None:
            _kv(lines, "VIX", f"{mc['vix_level']:.2f}")
        _kv(lines, "Source", mc.get("source"))
        lines.append("")

    pc = snapshot.get("price_context") or {}
    per_sym = pc.get("per_symbol") or {}
    if per_sym:
        lines.append("## Price at Trigger")
        lines.append("")
        for sym, q in per_sym.items():
            lines.append(f"### {sym}")
            lines.append("")
            if q.get("last_price") is not None:
                price_str = f"${q['last_price']:.2f}"
                if q.get("net_change_pct") is not None:
                    price_str += f" ({q['net_change_pct']:+.2f}%)"
                _kv(lines, "Last", price_str)
            if q.get("total_volume"):
                _kv(lines, "Volume", f"{q['total_volume']:,}")
            if q.get("bid") is not None and q.get("ask") is not None:
                _kv(lines, "Bid/Ask", f"${q['bid']:.2f} / ${q['ask']:.2f}")
            lines.append("")

    # ------------------------------------------------------------------
    # Final Prediction
    # ------------------------------------------------------------------
    if pred:
        lines.append("---")
        lines.append("")
        lines.append("## Final Prediction")
        lines.append("")
        _kv(lines, "Direction", pred.get("direction"))
        if pred.get("confidence") is not None:
            _kv(lines, "Confidence", f"{pred['confidence']*100:.0f}%")
        _kv(lines, "Horizon", pred.get("horizon"))
        _kv(lines, "Key Catalyst", pred.get("key_catalyst"))
        _kv(lines, "Magnitude", pred.get("magnitude_estimate"))
        _kv(lines, "Bull Case", pred.get("bull_case"))
        _kv(lines, "Bear Case", pred.get("bear_case"))
        risks = [r for r in (pred.get("risk_factors") or []) if r != "a"]
        if risks:
            lines.append("- **Risks:**")
            for r in risks:
                lines.append(f"  - {r}")
        lines.append("")

    # ------------------------------------------------------------------
    # X Stream Burst
    # ------------------------------------------------------------------
    x_burst = snapshot.get("x_stream_burst")
    if x_burst:
        lines.append("---")
        lines.append("")
        lines.append("## X Stream Burst")
        lines.append("")
        _kv(lines, "Rules", ", ".join(x_burst.get("rules", [])))
        _kv(lines, "Duration", f"{x_burst.get('duration_s', 0)}s")
        _kv(lines, "Posts Collected", x_burst.get("posts_collected", 0))
        _kv(lines, "Posts in Cache", x_burst.get("posts_in_cache", 0))
        _kv(lines, "Quality Attempts", x_burst.get("quality_attempts", 0))
        verdict = x_burst.get("quality_verdict")
        if verdict:
            _kv(lines, "Quality Relevant", verdict.get("relevant"))
            _kv(lines, "Quality Confidence", verdict.get("confidence"))
            _kv(lines, "Quality Reasoning", verdict.get("reasoning"))
            if verdict.get("revised_rule_values"):
                _kv(lines, "Revised Rules", ", ".join(verdict["revised_rule_values"]))
        posts = x_burst.get("posts", [])
        if posts:
            lines.append("")
            lines.append(f"### Collected Posts ({len(posts)})")
            lines.append("")
            for p in posts[:20]:
                data = p.get("data", {})
                text = data.get("text", p.get("text", "?"))
                author = data.get("author_id", "?")
                tags = [r.get("tag") for r in (p.get("matching_rules") or [])]
                lines.append(f"- [{', '.join(tags)}] @{author}: {text[:300]}")
        lines.append("")

    # ------------------------------------------------------------------
    # Cost + Metadata
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Cost Summary")
    lines.append("")
    _kv(lines, "Total", f"${cost.get('total_usd', 0):.4f}")
    by_tool = cost.get("by_tool") or {}
    if by_tool:
        lines.append("- **By Tool:**")
        for tool, c in by_tool.items():
            lines.append(f"  - {tool}: ${c:.4f}")
    lines.append("")

    lines.append("## Metadata")
    lines.append("")
    _kv(lines, "Snapshot ID", snapshot.get("snapshot_id"))
    _kv(lines, "Version", snapshot.get("version"))
    _kv(lines, "Created", snapshot.get("created_at"))
    budget = snapshot.get("exploration_budget") or {}
    if budget:
        _kv(lines, "Budget Max Hops", budget.get("max_hops"))
        if budget.get("max_cost_usd") is not None:
            _kv(lines, "Budget Max Cost", f"${budget['max_cost_usd']:.2f}")
    modalities = snapshot.get("data_modalities") or {}
    if modalities:
        lines.append("- **Modalities:**")
        for mod, indices in modalities.items():
            lines.append(f"  - {mod} ({len(indices)})")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown export helpers
# ---------------------------------------------------------------------------


def _kv(lines: list[str], key: str, value: Any) -> None:
    """Append a key-value line if value is truthy."""
    if value is not None and value != "" and value != []:
        lines.append(f"- **{key}:** {value}")


def _normalize_news_event_md(text: str) -> str:
    """Ensure The news event fields render on separate lines in markdown."""
    marker = "## The news event"
    idx = text.find(marker)
    if idx == -1:
        return text

    head = text[: idx + len(marker)]
    tail = text[idx + len(marker):]
    fields = [
        "**Headline:**",
        "**Summary:**",
        "**Symbols:**",
        "**Source:**",
        "**Timestamp:**",
        "**URL:**",
    ]
    for field in fields:
        tail = tail.replace(field, f"\n{field}")
    return head + tail


def _tool_summary_line(trace: dict[str, Any], index: int) -> str:
    """Build a one-line summary for a collapsed tool call."""
    action = trace.get("action") or {}
    execution = trace.get("execution") or {}
    tool = action.get("tool", "?")
    args = action.get("args") or {}
    error = trace.get("error")

    # Pick the most informative arg to display.
    display_arg = ""
    for key in ("query", "symbol", "url", "pattern"):
        if key in args:
            val = str(args[key])
            if len(val) > 60:
                val = val[:57] + "..."
            display_arg = f"{key}='{val}'"
            break
    else:
        for k, v in args.items():
            if isinstance(v, str) and v:
                val = v if len(v) <= 60 else v[:57] + "..."
                display_arg = f"{k}='{val}'"
                break

    call_str = f"{tool}({display_arg})"
    status = "ERROR" if error else "ok"
    cost = execution.get("cost_usd", 0)
    duration = execution.get("duration_s", 0)

    return f"{index}. <code>{call_str}</code> &mdash; {status}, ${cost:.4f}, {duration:.1f}s"


def _render_tool_trace_md(
    lines: list[str],
    trace: dict[str, Any],
    index: int,
    *,
    show_agent: bool = False,
) -> None:
    """Render a single tool trace as a collapsible details block."""
    action = trace.get("action") or {}
    execution = trace.get("execution") or {}
    args = action.get("args") or {}
    error = trace.get("error")

    summary = _tool_summary_line(trace, index)
    if show_agent and trace.get("agent"):
        summary += f" [{trace['agent']}]"

    lines.append("<details>")
    lines.append(f"<summary>{summary}</summary>")
    lines.append("")

    _kv(lines, "Duration", f"{execution.get('duration_s', 0):.3f}s")
    _kv(lines, "Modality", trace.get("modality"))
    _kv(lines, "Status", "error" if error else "ok")
    _kv(lines, "Cost", f"${execution.get('cost_usd', 0):.4f}")
    start = execution.get("start_time", "")
    end = execution.get("end_time", "")
    if start or end:
        _kv(lines, "Time", f"{start} -> {end}")
    if trace.get("hop_index") is not None:
        _kv(lines, "Hop Index", trace["hop_index"])
    if error:
        lines.append(f"- **Error:** {error}")
    lines.append("")

    if args:
        lines.append("**Input:**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(args, indent=2, default=str, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    output = trace.get("raw_tool_output")
    if output is not None:
        lines.append("**Output:**")
        lines.append("")
        if isinstance(output, (dict, list)):
            lines.append("```json")
            lines.append(json.dumps(output, indent=2, default=str, ensure_ascii=False))
            lines.append("```")
        else:
            lines.append("```")
            lines.append(str(output))
            lines.append("```")
        lines.append("")

    lines.append("</details>")
    lines.append("")
