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
    """Generate comprehensive markdown containing ALL snapshot data.

    Unlike ``eval_record_to_markdown`` (which truncates for prompt use), this
    function preserves every field — system prompts, user messages, reasoning
    tokens, full tool I/O, watch lifecycle, and follow-up collections.
    """
    lines: list[str] = []
    trigger = snapshot.get("trigger") or {}
    symbols = trigger.get("symbols") or []

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    lines.append(f"# Snapshot Export: {snapshot.get('snapshot_id', '?')}")
    lines.append("")
    lines.append(f"**Headline:** {trigger.get('headline', '?')}")
    lines.append(f"**Symbols:** {', '.join(symbols)}")
    lines.append(f"**Created:** {snapshot.get('created_at', '?')}")
    lines.append(f"**Version:** {snapshot.get('version', '?')}")
    cost = snapshot.get("cost_summary") or {}
    if cost.get("total_usd") is not None:
        lines.append(f"**Total Cost:** ${cost['total_usd']:.4f}")
    pred = snapshot.get("prediction") or {}
    if pred.get("direction"):
        conf = pred.get("confidence", 0)
        lines.append(f"**Direction:** {pred['direction']} ({conf*100:.0f}% confidence)")
    lines.append("")

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## Trigger")
    lines.append("")
    if trigger.get("summary"):
        lines.append(f"{trigger['summary']}")
        lines.append("")
    _kv(lines, "Source", trigger.get("source"))
    _kv(lines, "Type", trigger.get("type"))
    _kv(lines, "Article Time", trigger.get("alpaca_timestamp"))
    _kv(lines, "Source File", trigger.get("source_file"))
    raw = trigger.get("raw") or {}
    if raw.get("url"):
        _kv(lines, "Article URL", raw["url"])
    lines.append("")
    if raw:
        lines.append("### Trigger Raw Data")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(raw, indent=2, default=str, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    # ------------------------------------------------------------------
    # Market Context
    # ------------------------------------------------------------------
    mc = snapshot.get("market_context") or {}
    if mc:
        lines.append("---")
        lines.append("")
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

    # ------------------------------------------------------------------
    # Price at Trigger
    # ------------------------------------------------------------------
    pc = snapshot.get("price_context") or {}
    per_sym = pc.get("per_symbol") or {}
    if per_sym:
        lines.append("---")
        lines.append("")
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
    # Pre-fetched Market Data
    # ------------------------------------------------------------------
    prefetch_md = snapshot.get("prefetched_market_data") or ""
    if prefetch_md:
        lines.append("---")
        lines.append("")
        # The prefetch text starts with "## Pre-fetched market data\n\n"
        # Include it directly — it's already markdown-formatted
        lines.append(prefetch_md.strip())
        lines.append("")

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------
    triage = snapshot.get("triage") or {}
    if triage:
        lines.append("---")
        lines.append("")
        lines.append("## Triage")
        lines.append("")
        for k, v in triage.items():
            _kv(lines, k, v)
        lines.append("")

    # ------------------------------------------------------------------
    # Exploration Budget
    # ------------------------------------------------------------------
    budget = snapshot.get("exploration_budget") or {}
    if budget:
        lines.append("---")
        lines.append("")
        lines.append("## Exploration Budget")
        lines.append("")
        _kv(lines, "Max Hops", budget.get("max_hops"))
        if budget.get("max_cost_usd") is not None:
            _kv(lines, "Max Cost", f"${budget['max_cost_usd']:.2f}")
        lines.append("")

    # ------------------------------------------------------------------
    # Pipeline Execution Summary (table)
    # ------------------------------------------------------------------
    rounds = snapshot.get("rounds") or []
    if rounds:
        lines.append("---")
        lines.append("")
        lines.append("## Pipeline Execution Summary")
        lines.append("")
        lines.append("| # | Agent | Model | Tools | Tokens (in/out) | Reasoning | Reqs | Cost | Time |")
        lines.append("|---|-------|-------|------:|----------------:|----------:|-----:|-----:|-----:|")
        total_tools = total_tokens = total_reasoning = 0
        total_cost = total_time = 0.0
        for i, r in enumerate(rounds, 1):
            u = r.get("usage") or {}
            tc = u.get("tool_calls", 0)
            inp = u.get("input_tokens", 0)
            out = u.get("output_tokens", 0)
            rtok = u.get("reasoning_tokens", 0)
            reqs = u.get("requests", 0)
            rcost = r.get("cost_usd", 0)
            rtime = r.get("elapsed_s", 0)
            total_tools += tc
            total_tokens += u.get("total_tokens", 0)
            total_reasoning += rtok
            total_cost += rcost
            total_time += rtime
            rtok_str = f"{rtok:,}" if rtok else ""
            lines.append(
                f"| {i} | {r.get('agent', '?')} | `{r.get('model', '?')}` "
                f"| {tc} | {inp:,} / {out:,} | {rtok_str} | {reqs} "
                f"| ${rcost:.4f} | {rtime:.1f}s |"
            )
        rtot_str = f"{total_reasoning:,}" if total_reasoning else ""
        lines.append(
            f"| | **Total** | | **{total_tools}** | **{total_tokens:,}** "
            f"| **{rtot_str}** | | **${total_cost:.4f}** | **{total_time:.1f}s** |"
        )
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
        if pred.get("key_catalyst") and pred["key_catalyst"] != "a":
            _kv(lines, "Key Catalyst", pred["key_catalyst"])
        if pred.get("magnitude_estimate") and pred["magnitude_estimate"] != "a":
            _kv(lines, "Magnitude", pred["magnitude_estimate"])
        if pred.get("bull_case") and pred["bull_case"] != "a":
            _kv(lines, "Bull Case", pred["bull_case"])
        if pred.get("bear_case") and pred["bear_case"] != "a":
            _kv(lines, "Bear Case", pred["bear_case"])
        risks = [r for r in (pred.get("risk_factors") or []) if r != "a"]
        if risks:
            lines.append("- **Risk Factors:**")
            for r in risks:
                lines.append(f"  - {r}")
        lines.append("")

    # ------------------------------------------------------------------
    # Agent Rounds (detailed)
    # ------------------------------------------------------------------
    if rounds:
        lines.append("---")
        lines.append("")
        lines.append("## Agent Rounds")
        lines.append("")
        for i, r in enumerate(rounds, 1):
            u = r.get("usage") or {}
            lines.append(f"### Round {i}: {r.get('agent', '?')}")
            lines.append("")
            _kv(lines, "Model", f"`{r.get('model', '?')}`")
            _kv(lines, "Tokens", f"{u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out")
            if u.get("reasoning_tokens"):
                _kv(lines, "Reasoning Tokens", f"{u['reasoning_tokens']:,}")
            _kv(lines, "Tool Calls", u.get("tool_calls", 0))
            _kv(lines, "Requests", u.get("requests", 0))
            _kv(lines, "Cost", f"${r.get('cost_usd', 0):.4f}")
            _kv(lines, "Elapsed", f"{r.get('elapsed_s', 0):.1f}s")
            lines.append("")

            # System prompt
            if r.get("system_prompt"):
                lines.append("#### System Prompt")
                lines.append("")
                open_f, close_f = _safe_code_fence(r["system_prompt"])
                lines.append("<details>")
                lines.append(f"<summary>Show system prompt ({len(r['system_prompt'])} chars)</summary>")
                lines.append("")
                lines.append(open_f)
                lines.append(r["system_prompt"])
                lines.append(close_f)
                lines.append("")
                lines.append("</details>")
                lines.append("")

            # User message
            if r.get("user_message"):
                lines.append("#### User Message")
                lines.append("")
                open_f, close_f = _safe_code_fence(r["user_message"])
                lines.append("<details>")
                lines.append(f"<summary>Show user message ({len(r['user_message'])} chars)</summary>")
                lines.append("")
                lines.append(open_f)
                lines.append(r["user_message"])
                lines.append(close_f)
                lines.append("")
                lines.append("</details>")
                lines.append("")

            # Reasoning / thinking summary
            if r.get("thinking_summary"):
                lines.append("#### Reasoning Summary")
                lines.append("")
                for ts_line in r["thinking_summary"].splitlines():
                    lines.append(f"> {ts_line}")
                lines.append("")

            # Agent findings
            if r.get("findings"):
                lines.append("#### Agent Findings")
                lines.append("")
                lines.append(r["findings"])
                lines.append("")

            # Raw output (if different from findings)
            if r.get("raw_output") and r["raw_output"] != r.get("findings"):
                lines.append("#### Raw Output")
                lines.append("")
                open_f, close_f = _safe_code_fence(r["raw_output"])
                lines.append("<details>")
                lines.append(f"<summary>Show raw output ({len(r['raw_output'])} chars)</summary>")
                lines.append("")
                lines.append(open_f)
                lines.append(r["raw_output"])
                lines.append(close_f)
                lines.append("")
                lines.append("</details>")
                lines.append("")

            # Signal (parsed TradingSignal)
            if r.get("signal"):
                lines.append("#### Parsed Signal")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(r["signal"], indent=2, default=str, ensure_ascii=False))
                lines.append("```")
                lines.append("")

            # Per-agent tool calls
            traces = r.get("tool_traces") or []
            if traces:
                lines.append(f"#### Tool Calls ({len(traces)})")
                lines.append("")
                for j, t in enumerate(traces, 1):
                    _render_tool_trace_md(lines, t, j)

    # ------------------------------------------------------------------
    # Cost Summary
    # ------------------------------------------------------------------
    if cost:
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

    # ------------------------------------------------------------------
    # Data Modalities
    # ------------------------------------------------------------------
    modalities = snapshot.get("data_modalities") or {}
    if modalities:
        lines.append("---")
        lines.append("")
        lines.append("## Data Modalities")
        lines.append("")
        for mod, indices in modalities.items():
            lines.append(f"- **{mod}:** {len(indices)} traces (indices: {indices})")
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
    # Watch
    # ------------------------------------------------------------------
    if watch:
        lines.append("---")
        lines.append("")
        lines.append("## Watch")
        lines.append("")
        _kv(lines, "Watch ID", watch.get("watch_id"))
        _kv(lines, "Symbol", watch.get("symbol"))
        _kv(lines, "Status", watch.get("status"))
        _kv(lines, "Created", watch.get("created_at"))
        _kv(lines, "Last Check-in", watch.get("last_checkin_at"))
        _kv(lines, "Sealed", watch.get("lifecycle_sealed_at"))
        lines.append("")

        entry = watch.get("entry") or {}
        if entry:
            lines.append("### Entry")
            lines.append("")
            _kv(lines, "Price", f"${entry.get('price', 0):.2f}" if entry.get("price") is not None else None)
            _kv(lines, "Time", entry.get("time"))
            _kv(lines, "Direction", entry.get("direction"))
            if entry.get("confidence") is not None:
                _kv(lines, "Confidence", f"{entry['confidence']*100:.0f}%")
            _kv(lines, "Horizon", entry.get("horizon"))
            _kv(lines, "Thesis", entry.get("thesis"))
            _kv(lines, "Snapshot ID", entry.get("snapshot_id"))
            lines.append("")

        checkins = watch.get("checkin_history") or []
        if checkins:
            lines.append("### Check-in History")
            lines.append("")
            for ci_idx, ci in enumerate(checkins, 1):
                lines.append(f"#### Check-in {ci_idx}")
                lines.append("")
                _kv(lines, "Time", ci.get("checkin_at"))
                _kv(lines, "Action", ci.get("action"))
                if ci.get("pnl_pct") is not None:
                    _kv(lines, "P&L", f"{ci['pnl_pct']:+.2f}%")
                _kv(lines, "Depth", ci.get("depth"))
                _kv(lines, "Reason", ci.get("reason"))
                _kv(lines, "Snapshot ID", ci.get("snapshot_id"))
                resp = ci.get("agent_response")
                if resp:
                    lines.append("")
                    lines.append("**Agent Response:**")
                    lines.append("")
                    if isinstance(resp, dict):
                        lines.append("```json")
                        lines.append(json.dumps(resp, indent=2, default=str, ensure_ascii=False))
                        lines.append("```")
                    else:
                        lines.append(str(resp))
                lines.append("")

        exit_data = watch.get("exit")
        if exit_data:
            lines.append("### Exit")
            lines.append("")
            _kv(lines, "Price", f"${exit_data.get('price', 0):.2f}" if exit_data.get("price") is not None else None)
            _kv(lines, "Time", exit_data.get("time"))
            _kv(lines, "Reason", exit_data.get("reason"))
            if exit_data.get("realized_pnl_pct") is not None:
                _kv(lines, "Realized P&L", f"{exit_data['realized_pnl_pct']:+.2f}%")
            _kv(lines, "Snapshot ID", exit_data.get("snapshot_id"))
            lines.append("")

        retro = watch.get("retrospective_data")
        if retro:
            lines.append("### Retrospective")
            lines.append("")
            if isinstance(retro, dict):
                for k, v in retro.items():
                    _kv(lines, k, v)
            else:
                lines.append(str(retro))
            lines.append("")

        # Monitoring + retrospective snapshot IDs
        mon_ids = watch.get("monitoring_snapshot_ids") or []
        if mon_ids:
            lines.append("### Monitoring Snapshots")
            lines.append("")
            for sid in mon_ids:
                lines.append(f"- `{sid}`")
            lines.append("")
        retro_ids = watch.get("retrospective_snapshot_ids") or []
        if retro_ids:
            lines.append("### Retrospective Snapshots")
            lines.append("")
            for sid in retro_ids:
                lines.append(f"- `{sid}`")
            lines.append("")

    # ------------------------------------------------------------------
    # Follow-ups
    # ------------------------------------------------------------------
    fu_list = follow_ups or []
    if fu_list:
        lines.append("---")
        lines.append("")
        lines.append("## Follow-ups")
        lines.append("")
        for fu_idx, fu_raw in enumerate(fu_list, 1):
            fu = fu_raw.get("follow_up_json") or fu_raw
            if isinstance(fu, str):
                fu = json.loads(fu)
            lines.append(f"### Follow-up {fu_idx}: {fu.get('reason', '?')}")
            lines.append("")
            _kv(lines, "ID", fu.get("follow_up_id"))
            _kv(lines, "Symbols", ", ".join(fu.get("symbols", [])))
            _kv(lines, "Status", fu.get("status"))
            _kv(lines, "Schedule", ", ".join(fu.get("schedule", [])))
            _kv(lines, "Started", fu.get("started_at"))
            if fu.get("total_cost_usd") is not None:
                _kv(lines, "Total Cost", f"${fu['total_cost_usd']:.4f}")
            _kv(lines, "Watch ID", fu.get("watch_id"))
            _kv(lines, "Headline", fu.get("headline"))
            lines.append("")

            config = fu.get("config")
            if config:
                lines.append("#### Config")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(config, indent=2, default=str, ensure_ascii=False))
                lines.append("```")
                lines.append("")

            for c_idx, coll in enumerate(fu.get("collections") or [], 1):
                lines.append(f"#### Collection {c_idx}: {coll.get('offset_label', '?')}")
                lines.append("")
                _kv(lines, "Collected At", coll.get("collected_at"))
                if coll.get("cost_usd") is not None:
                    _kv(lines, "Cost", f"${coll['cost_usd']:.4f}")
                lines.append("")

                # Price data
                price = coll.get("price") or {}
                if price:
                    lines.append("##### Price Data")
                    lines.append("")
                    for sym, pdata in price.items():
                        if isinstance(pdata, dict):
                            lines.append(f"**{sym}:**")
                            for pk, pv in pdata.items():
                                _kv(lines, pk, pv)
                        else:
                            _kv(lines, sym, pdata)
                    lines.append("")

                # News
                news = coll.get("news") or []
                if news:
                    lines.append(f"##### News ({len(news)} items)")
                    lines.append("")
                    for n in news:
                        if isinstance(n, dict):
                            title = n.get("title") or n.get("headline", "?")
                            lines.append(f"- **{title}**")
                            if n.get("url") or n.get("link"):
                                lines.append(f"  - URL: {n.get('url') or n.get('link')}")
                            if n.get("source"):
                                lines.append(f"  - Source: {n['source']}")
                            if n.get("published") or n.get("datetime"):
                                lines.append(f"  - Time: {n.get('published') or n.get('datetime')}")
                        else:
                            lines.append(f"- {n}")
                    lines.append("")

                # Query plan
                qp = coll.get("query_plan") or {}
                if qp:
                    lines.append("##### Query Plan")
                    lines.append("")
                    if qp.get("reasoning"):
                        _kv(lines, "Reasoning", qp["reasoning"])
                    if qp.get("web_queries"):
                        lines.append("- **Web Queries:**")
                        for q in qp["web_queries"]:
                            lines.append(f"  - {q}")
                    if qp.get("x_queries"):
                        lines.append("- **X Queries:**")
                        for q in qp["x_queries"]:
                            lines.append(f"  - {q}")
                    lines.append("")

                # Web results
                web = coll.get("web_results") or []
                if web:
                    lines.append(f"##### Web Results ({len(web)})")
                    lines.append("")
                    for wr in web:
                        lines.append(f"- **Query:** {wr.get('query', '?')}")
                        if wr.get("answer"):
                            lines.append(f"  - **Answer:** {wr['answer']}")
                        if wr.get("citations"):
                            lines.append(f"  - **Citations:** {', '.join(wr['citations'])}")
                        if wr.get("quality") is not None:
                            lines.append(f"  - **Quality:** {wr['quality']}")
                    lines.append("")

                # X results
                xr = coll.get("x_results") or []
                if xr:
                    lines.append(f"##### X Results ({len(xr)})")
                    lines.append("")
                    for xres in xr:
                        lines.append(f"- **Query:** {xres.get('query', '?')}")
                        if xres.get("answer"):
                            lines.append(f"  - **Answer:** {xres['answer']}")
                        if xres.get("citations"):
                            lines.append(f"  - **Citations:** {', '.join(xres['citations'])}")
                        if xres.get("quality") is not None:
                            lines.append(f"  - **Quality:** {xres['quality']}")
                    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown export helpers
# ---------------------------------------------------------------------------


def _kv(lines: list[str], key: str, value: Any) -> None:
    """Append a key-value line if value is truthy."""
    if value is not None and value != "" and value != []:
        lines.append(f"- **{key}:** {value}")


def _safe_code_fence(content: str, lang: str = "") -> tuple[str, str]:
    """Return (open_fence, close_fence) safe for *content*.

    Scans for the longest consecutive backtick run and uses one more.
    """
    max_run = current = 0
    for ch in content:
        if ch == "`":
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    fence = "`" * max(3, max_run + 1)
    open_fence = f"{fence}{lang}" if lang else fence
    return open_fence, fence


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
