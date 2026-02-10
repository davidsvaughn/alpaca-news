"""Stage 2: exploration (Phase 2).

Explorer v1 implements:
- Finite action menu (learnable)
- Two-phase exploration:
  - Phase 1: wide / cheap / shallow → generate hypotheses
  - Phase 2: narrow / gated → confirm/deny top-K hypotheses
- Orthogonality enforcement (actions use different tools/providers when possible)
- ToolTrace logging per hop

The online orchestrator calls :func:`explore_two_phase` and adds returned tool
traces into the SnapshotBuilder.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trader.llm.client import LLMClient
from trader.llm.extract import extract_json
from trader.market.schwab_client import SchwabMarketClient
from trader.models.actions import (
    ActionTemplate,
    ActionTool,
    Hypothesis,
    PHASE1_ACTIONS,
    PHASE2_ACTIONS,
    StopReason,
    render_query,
)
from trader.models.tool_trace import TraceExecution, new_tool_trace, utc_now_iso

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


@dataclass(frozen=True)
class ExploreResult:
    traces: list[dict[str, Any]]
    cost_usd: float
    hypotheses: list[dict[str, Any]]


def _read_prompt(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8")


PROMPT_PHASE1 = _read_prompt("trader/prompts/explore_phase1.md")
PROMPT_PHASE2 = _read_prompt("trader/prompts/explore_phase2.md")
PROMPT_RANK = _read_prompt("trader/prompts/hypothesis_rank.md")


def _action_menu_str(actions: list[ActionTemplate]) -> str:
    # Keep this compact so it doesn't bloat prompts.
    lines: list[str] = []
    for a in actions:
        lines.append(f"- {a.action_id}: tool={a.tool.value} provider={a.provider} purpose={a.purpose}")
    return "\n".join(lines)


def _choose_phase1_actions(
    *,
    symbols: list[str],
    max_actions: int,
    allow_x: bool,
    allow_google: bool,
) -> list[ActionTemplate]:
    """Pick a small set of Phase 1 actions with basic orthogonality."""

    candidates: list[ActionTemplate] = []
    for a in PHASE1_ACTIONS:
        if a.tool == ActionTool.X_SEARCH and not allow_x:
            continue
        if a.tool == ActionTool.GOOGLE_SEARCH and not allow_google:
            continue
        # If no symbols, prefer broad web/google actions.
        candidates.append(a)

    # Heuristic: try to include one market-data check, one web/google, and optionally x.
    market = [a for a in candidates if a.tool in (ActionTool.PRICE_CHECK, ActionTool.VOLUME_CHECK)]
    web = [a for a in candidates if a.tool in (ActionTool.WEB_SEARCH, ActionTool.GOOGLE_SEARCH)]
    x = [a for a in candidates if a.tool == ActionTool.X_SEARCH]

    picked: list[ActionTemplate] = []
    if market:
        picked.append(market[0])
    if web:
        picked.append(web[0])
    if x and len(picked) < max_actions:
        picked.append(x[0])

    # Fill remaining slots randomly but keep tools unique when possible.
    used_tools = {a.tool for a in picked}
    pool = [a for a in candidates if a.tool not in used_tools]
    random.shuffle(pool)
    for a in pool:
        if len(picked) >= max_actions:
            break
        picked.append(a)

    return picked[:max_actions]


def _call_llm_for_action(
    *,
    llm: LLMClient,
    provider: str,
    model: str,
    tool: ActionTool,
    input_text: str,
    stage: str,
    purpose: str,
) -> tuple[Any, str]:
    """Invoke the unified LLM client with correct tool flags.

    Returns (LLMResult-like object, tool_name used in trace.action.tool).
    """

    if provider == "openai":
        # Only OpenAI web_search supported here
        res = llm.query_openai(
            model=model,
            input_text=input_text,
            web_search=(tool == ActionTool.WEB_SEARCH),
            stage=stage,
            purpose=purpose,
        )
        tool_name = "web_search" if tool == ActionTool.WEB_SEARCH else "none"
        return res, tool_name

    if provider == "grok":
        res = llm.query_grok(
            model=model,
            input_text=input_text,
            web_search=(tool == ActionTool.WEB_SEARCH),
            x_search=(tool == ActionTool.X_SEARCH),
            stage=stage,
            purpose=purpose,
        )
        tool_name = "x_search" if tool == ActionTool.X_SEARCH else ("web_search" if tool == ActionTool.WEB_SEARCH else "none")
        return res, tool_name

    if provider == "gemini":
        res = llm.query_gemini(
            model=model,
            input_text=input_text,
            google_search=(tool == ActionTool.GOOGLE_SEARCH),
            stage=stage,
            purpose=purpose,
        )
        tool_name = "GoogleSearch" if tool == ActionTool.GOOGLE_SEARCH else "none"
        return res, tool_name

    raise ValueError(f"Unknown provider: {provider}")


def _market_action(
    *,
    market: SchwabMarketClient,
    tool: ActionTool,
    symbol: str,
) -> dict[str, Any]:
    if tool == ActionTool.PRICE_CHECK:
        return market.check_price_spike(symbol)
    if tool == ActionTool.VOLUME_CHECK:
        return market.check_volume_regime(symbol)
    raise ValueError(f"Unsupported market action tool: {tool}")


def _dedupe_evidence(items: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    seen = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = (str(it.get("url") or ""), str(it.get("title") or ""), str(it.get("snippet") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= max_items:
            break
    return out


def explore_two_phase(
    *,
    llm: LLMClient,
    market: SchwabMarketClient | None,
    news: dict[str, Any],
    symbols: list[str],
    # Providers/models
    research_provider: str,
    research_model: str,
    xsearch_provider: str,
    xsearch_model: str,
    sentiment_provider: str,
    sentiment_model: str,
    # Limits
    max_phase1_actions: int,
    max_phase2_branches: int,
    max_total_hops: int,
) -> ExploreResult:
    """Run Phase 1 + Phase 2 exploration and return tool traces."""

    traces: list[dict[str, Any]] = []
    hypotheses: list[Hypothesis] = []
    total_cost = 0.0

    symbols_str = ", ".join(symbols) if symbols else "(all mentioned)"
    headline = str(news.get("headline") or "")
    headline_short = headline[:80]
    company = symbols[0] if symbols else ""

    # ------------------------------------------------------------------
    # Phase 1: execute a few broad actions and then ask LLM to propose hypotheses
    # ------------------------------------------------------------------

    allow_x = True  # we can still choose, provider gating happens per action
    allow_google = True
    phase1_actions = _choose_phase1_actions(
        symbols=symbols,
        max_actions=max_phase1_actions,
        allow_x=allow_x,
        allow_google=allow_google,
    )

    evidence_items: list[dict[str, Any]] = []

    hop_index = 1
    for a in phase1_actions:
        if hop_index > max_total_hops:
            break

        trace_id = f"trace_{hop_index}"
        start = utc_now_iso()

        # Render query context for logging
        ctx = {
            "symbol": symbols[0] if symbols else "",
            "headline": headline,
            "headline_short": headline_short,
            "company": company,
            "trusted_handles": "",
        }
        rendered_query = render_query(a, ctx)

        if a.provider == "none":
            if market is None or not market.available:
                # Market actions are optional; if unavailable, log empty evidence.
                obs = {"symbol": symbols[0] if symbols else "", "unavailable": True}
            else:
                sym = symbols[0] if symbols else ""
                obs = _market_action(market=market, tool=a.tool, symbol=sym)

            end = utc_now_iso()
            results = [
                {
                    "source_type": "market",
                    "title": a.action_id,
                    "timestamp": end,
                    "snippet": json.dumps(obs, ensure_ascii=False),
                }
            ]
            evidence_items.extend(results)
            traces.append(
                new_tool_trace(
                    trace_id=trace_id,
                    hop_index=hop_index,
                    parent_trace_id=None,
                    decision_context={
                        "state_summary": "",
                        "reason_for_action": f"Phase1 action: {a.purpose}",
                        "symbols": symbols,
                    },
                    action={
                        "tool": a.tool.value,
                        "provider": "market",
                        "query_template": a.action_id,
                        "query": rendered_query,
                        "filters": {},
                    },
                    execution=TraceExecution(model="market", start_time=start, end_time=end, cost_usd=0.0),
                    results=results,
                    extracted_signals={"observation": obs},
                    stop_signal={"should_stop": False, "reason": ""},
                )
            )
            hop_index += 1
            continue

        # Provider/model selection by tool
        if a.tool == ActionTool.X_SEARCH:
            provider = xsearch_provider
            model = xsearch_model
        elif a.tool == ActionTool.GOOGLE_SEARCH:
            provider = sentiment_provider
            model = sentiment_model
        else:
            provider = research_provider
            model = research_model

        # Keep the Phase 1 prompt minimal: ask model to gather evidence.
        prompt = (
            "You are a trading research assistant.\n\n"
            f"Execute action_id={a.action_id} to gather evidence.\n"
            f"Query: {rendered_query}\n\n"
            "Return STRICT JSON with keys: state_summary, evidence (<=8), takeaways (<=6).\n\n"
            f"News JSON:\n{json.dumps(news, ensure_ascii=False)}\n"
        )

        res, tool_name = _call_llm_for_action(
            llm=llm,
            provider=provider,
            model=model,
            tool=a.tool,
            input_text=prompt,
            stage="explore",
            purpose=f"phase1:{a.action_id}",
        )
        end = utc_now_iso()
        total_cost += float(res.cost_usd)

        data = extract_json(res.text)
        items = [dict(x) for x in (data.get("evidence") or [])]
        items = _dedupe_evidence(items, max_items=8)
        evidence_items.extend(items)

        traces.append(
            new_tool_trace(
                trace_id=trace_id,
                hop_index=hop_index,
                parent_trace_id=None,
                decision_context={
                    "state_summary": str(data.get("state_summary") or ""),
                    "reason_for_action": f"Phase1 action: {a.purpose}",
                    "symbols": symbols,
                },
                action={
                    "tool": tool_name,
                    "provider": provider,
                    "query_template": a.action_id,
                    "query": rendered_query,
                    "filters": {"max_items": 8},
                },
                execution=TraceExecution(model=model, start_time=start, end_time=end, cost_usd=float(res.cost_usd)),
                results=items,
                extracted_signals={"takeaways": data.get("takeaways") or []},
                stop_signal={"should_stop": False, "reason": ""},
            )
        )
        hop_index += 1

    # Now ask LLM to generate hypotheses from Phase 1 evidence
    evidence_so_far = json.dumps(_dedupe_evidence(evidence_items, max_items=12), ensure_ascii=False)
    prompt1 = PROMPT_PHASE1.format(
        symbols=symbols_str,
        news_json=json.dumps(news, ensure_ascii=False),
        evidence_so_far=evidence_so_far,
        action_menu=_action_menu_str(PHASE2_ACTIONS),
    )

    res_h, tool_name_h = _call_llm_for_action(
        llm=llm,
        provider=research_provider,
        model=research_model,
        tool=ActionTool.WEB_SEARCH if research_provider in ("openai", "grok") else ActionTool.GOOGLE_SEARCH,
        input_text=prompt1,
        stage="explore",
        purpose="phase1:hypotheses",
    )
    total_cost += float(res_h.cost_usd)

    data_h = extract_json(res_h.text)
    hyp_list = [dict(x) for x in (data_h.get("hypotheses") or [])]
    for h in hyp_list:
        hypotheses.append(Hypothesis.from_dict(h))

    # Log this hypothesis generation as a trace (no external tool necessarily, but LLM call cost matters)
    traces.append(
        new_tool_trace(
            trace_id=f"trace_{hop_index}",
            hop_index=hop_index,
            parent_trace_id=None,
            decision_context={
                "state_summary": str(data_h.get("state_summary") or ""),
                "reason_for_action": "Phase1: propose competing hypotheses",
                "symbols": symbols,
            },
            action={
                "tool": tool_name_h,
                "provider": research_provider,
                "query_template": "phase1_hypotheses",
                "query": "(prompted)",
                "filters": {"max_hypotheses": 5},
            },
            execution=TraceExecution(
                model=research_model,
                start_time=utc_now_iso(),
                end_time=utc_now_iso(),
                cost_usd=float(res_h.cost_usd),
            ),
            results=[h.to_dict() for h in hypotheses][:5],
            extracted_signals={
                "freshness": data_h.get("freshness"),
                "takeaways": data_h.get("takeaways") or [],
            },
            stop_signal={"should_stop": False, "reason": ""},
        )
    )
    hop_index += 1

    if not hypotheses:
        # Nothing to do
        return ExploreResult(traces=traces, cost_usd=total_cost, hypotheses=[])

    # ------------------------------------------------------------------
    # Rank hypotheses and assign one action each (top-K)
    # ------------------------------------------------------------------

    top_k = max_phase2_branches
    prompt_rank = PROMPT_RANK.format(
        top_k=top_k,
        hypotheses_json=json.dumps([h.to_dict() for h in hypotheses], ensure_ascii=False, indent=2),
        action_menu=_action_menu_str(PHASE2_ACTIONS),
    )
    res_r, tool_name_r = _call_llm_for_action(
        llm=llm,
        provider=research_provider,
        model=research_model,
        tool=ActionTool.WEB_SEARCH if research_provider in ("openai", "grok") else ActionTool.GOOGLE_SEARCH,
        input_text=prompt_rank,
        stage="explore",
        purpose="phase2:rank",
    )
    total_cost += float(res_r.cost_usd)
    rank_data = extract_json(res_r.text)

    selected = [dict(x) for x in (rank_data.get("selected") or [])]

    traces.append(
        new_tool_trace(
            trace_id=f"trace_{hop_index}",
            hop_index=hop_index,
            parent_trace_id=None,
            decision_context={
                "state_summary": "",
                "reason_for_action": "Phase2: select top hypotheses and assign follow-up actions",
                "symbols": symbols,
            },
            action={
                "tool": tool_name_r,
                "provider": research_provider,
                "query_template": "hypothesis_rank",
                "query": "(prompted)",
                "filters": {"top_k": top_k},
            },
            execution=TraceExecution(
                model=research_model,
                start_time=utc_now_iso(),
                end_time=utc_now_iso(),
                cost_usd=float(res_r.cost_usd),
            ),
            results=selected,
            extracted_signals={"orthogonality_check": rank_data.get("orthogonality_check")},
            stop_signal={"should_stop": False, "reason": ""},
        )
    )
    hop_index += 1

    # Map action_id → template
    phase2_by_id = {a.action_id: a for a in PHASE2_ACTIONS}
    hyp_by_id = {h.hypothesis_id: h for h in hypotheses}

    used_tools: set[ActionTool] = set()

    # ------------------------------------------------------------------
    # Phase 2: Execute one follow-up per selected hypothesis
    # ------------------------------------------------------------------

    for item in selected[:max_phase2_branches]:
        if hop_index > max_total_hops:
            break

        hid = str(item.get("hypothesis_id") or "")
        action_id = str(item.get("assigned_action_id") or "")
        hyp = hyp_by_id.get(hid)
        action = phase2_by_id.get(action_id)
        if hyp is None or action is None:
            continue

        # Orthogonality enforcement: if tool already used, try to find alternative
        chosen_action = action
        if chosen_action.tool in used_tools:
            alt = None
            for a in PHASE2_ACTIONS:
                if a.action_id in hyp.suggested_action_ids and a.tool not in used_tools:
                    alt = a
                    break
            if alt is not None:
                chosen_action = alt
        used_tools.add(chosen_action.tool)

        ctx = {
            "symbol": symbols[0] if symbols else "",
            "headline": headline,
            "headline_short": headline_short,
            "company": company,
            "trusted_handles": "",
        }
        rendered = render_query(chosen_action, ctx)

        # Provider/model routing
        if chosen_action.tool == ActionTool.X_SEARCH:
            provider = xsearch_provider
            model = xsearch_model
        elif chosen_action.tool == ActionTool.GOOGLE_SEARCH:
            provider = sentiment_provider
            model = sentiment_model
        else:
            provider = research_provider
            model = research_model

        prompt2 = PROMPT_PHASE2.format(
            symbols=symbols_str,
            headline=headline,
            hypothesis_id=hyp.hypothesis_id,
            hypothesis_label=hyp.label,
            hypothesis_description=hyp.description,
            hypothesis_confidence=hyp.confidence,
            hypothesis_category=hyp.category,
            action_id=chosen_action.action_id,
            action_purpose=chosen_action.purpose,
            rendered_query=rendered,
            prior_evidence=evidence_so_far,
        )

        trace_id = f"trace_{hop_index}"
        start = utc_now_iso()
        res2, tool_name2 = _call_llm_for_action(
            llm=llm,
            provider=provider,
            model=model,
            tool=chosen_action.tool,
            input_text=prompt2,
            stage="explore",
            purpose=f"phase2:{chosen_action.action_id}",
        )
        end = utc_now_iso()
        total_cost += float(res2.cost_usd)

        data2 = extract_json(res2.text)
        items2 = _dedupe_evidence([dict(x) for x in (data2.get("evidence") or [])], max_items=8)

        stop_signal = dict(data2.get("stop_signal") or {})
        reason = str(stop_signal.get("reason") or "")
        should_stop = bool(stop_signal.get("should_stop"))
        if reason and reason not in {r.value for r in StopReason}:
            # Enforce known stop reasons
            raise RuntimeError(f"Invalid stop reason: {reason}")

        traces.append(
            new_tool_trace(
                trace_id=trace_id,
                hop_index=hop_index,
                parent_trace_id=None,
                decision_context={
                    "state_summary": str(data2.get("state_summary") or ""),
                    "reason_for_action": f"Phase2 follow-up for hypothesis {hid}",
                    "symbols": symbols,
                    "hypothesis": hyp.to_dict(),
                },
                action={
                    "tool": tool_name2,
                    "provider": provider,
                    "query_template": chosen_action.action_id,
                    "query": rendered,
                    "filters": {"max_items": 8},
                },
                execution=TraceExecution(model=model, start_time=start, end_time=end, cost_usd=float(res2.cost_usd)),
                results=items2,
                extracted_signals=dict(data2.get("extracted_signals") or {}),
                stop_signal={"should_stop": should_stop, "reason": reason},
            )
        )

        hop_index += 1
        evidence_items.extend(items2)

        if should_stop:
            break

    return ExploreResult(
        traces=traces,
        cost_usd=total_cost,
        hypotheses=[h.to_dict() for h in hypotheses],
    )
