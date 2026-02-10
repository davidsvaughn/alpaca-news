"""Finite action menu for the explorer.

Phase 2 design: the explorer selects from a *finite* set of actions rather
than inventing arbitrary queries.  This makes learning tractable — each action
is a (tool × query_template × constraints) triple.

The menu also includes **stop actions** which are first-class decisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Stop signals
# ---------------------------------------------------------------------------

class StopReason(str, Enum):
    """First-class stopping decisions."""
    CONFIRMED = "STOP_CONFIRMED"       # sufficient confirmation gathered
    LOW_SIGNAL = "STOP_LOW_SIGNAL"     # evidence quality too weak to continue
    BUDGET = "STOP_BUDGET"             # marginal value < cost
    REDUNDANT = "STOP_REDUNDANT"       # no new info vs prior hops


# ---------------------------------------------------------------------------
# Action definitions
# ---------------------------------------------------------------------------

class ActionTool(str, Enum):
    WEB_SEARCH = "web_search"          # OpenAI web_search
    GOOGLE_SEARCH = "google_search"    # Gemini GoogleSearch grounding
    X_SEARCH = "x_search"             # Grok x_search
    PRICE_CHECK = "price_check"       # non-LLM market data check
    VOLUME_CHECK = "volume_check"     # non-LLM market data check


@dataclass(frozen=True)
class ActionTemplate:
    """A single entry in the finite action menu.

    Attributes:
        action_id: unique identifier (used for logging + learning)
        tool: which search tool to invoke
        provider: which LLM provider to use (or 'none' for market-data-only)
        query_template: a Python format-string with placeholders like {symbol},
                        {headline}, {company}, {trusted_handles}
        purpose: human-readable description of why this action exists
        phase: which exploration phase this action is appropriate for
        default_weight: initial selection weight (higher = more likely to be chosen)
    """
    action_id: str
    tool: ActionTool
    provider: Literal["openai", "grok", "gemini", "none"]
    query_template: str
    purpose: str
    phase: Literal["phase1", "phase2", "any"]
    default_weight: float = 1.0


# ---------------------------------------------------------------------------
# Built-in action menu
# ---------------------------------------------------------------------------

# -- Web search actions (OpenAI web_search / Gemini google_search) ----------

NEWS_CONFIRMATION = ActionTemplate(
    action_id="news_confirmation",
    tool=ActionTool.WEB_SEARCH,
    provider="openai",
    query_template='latest confirmation of "{headline}" {symbol}',
    purpose="rumor → confirmation",
    phase="phase2",
    default_weight=1.2,
)

BREAKING_FOLLOWUP = ActionTemplate(
    action_id="breaking_followup",
    tool=ActionTool.WEB_SEARCH,
    provider="openai",
    query_template="breaking {symbol} today",
    purpose="freshness check",
    phase="phase1",
    default_weight=1.0,
)

FILING_CHECK = ActionTemplate(
    action_id="filing_check",
    tool=ActionTool.WEB_SEARCH,
    provider="openai",
    query_template="site:sec.gov {company} 8-K",
    purpose="regulatory catalyst check",
    phase="phase2",
    default_weight=0.8,
)

ANALYST_REACTION = ActionTemplate(
    action_id="analyst_reaction",
    tool=ActionTool.WEB_SEARCH,
    provider="openai",
    query_template="analyst reaction {symbol}",
    purpose="secondary effects / analyst coverage",
    phase="phase2",
    default_weight=0.9,
)

GOOGLE_BROAD_SEARCH = ActionTemplate(
    action_id="google_broad_search",
    tool=ActionTool.GOOGLE_SEARCH,
    provider="gemini",
    query_template="{symbol} {headline_short} latest news",
    purpose="broad high-recall search via Google grounding",
    phase="phase1",
    default_weight=1.1,
)

GOOGLE_SENTIMENT = ActionTemplate(
    action_id="google_sentiment",
    tool=ActionTool.GOOGLE_SEARCH,
    provider="gemini",
    query_template="{symbol} market sentiment analysis today",
    purpose="sentiment via Google grounding",
    phase="phase2",
    default_weight=0.7,
)

# -- X search actions (Grok x_search) ------------------------------------

X_REALTIME_RUMOR = ActionTemplate(
    action_id="x_realtime_rumor",
    tool=ActionTool.X_SEARCH,
    provider="grok",
    query_template="{symbol} rumor OR hearing OR channel checks",
    purpose="social rumor detection",
    phase="phase1",
    default_weight=1.0,
)

X_VOLUME_ALERTS = ActionTemplate(
    action_id="x_volume_alerts",
    tool=ActionTool.X_SEARCH,
    provider="grok",
    query_template="{symbol} unusual volume",
    purpose="detect abnormal activity chatter",
    phase="phase2",
    default_weight=0.8,
)

X_INSIDER_ACCOUNTS = ActionTemplate(
    action_id="x_insider_accounts",
    tool=ActionTool.X_SEARCH,
    provider="grok",
    query_template='"{symbol}" insider OR institutional',
    purpose="insider / institutional flow chatter",
    phase="phase2",
    default_weight=0.7,
)

# -- Market-data-only actions (non-LLM) ----------------------------------

PRICE_SPIKE_CHECK = ActionTemplate(
    action_id="price_spike_check",
    tool=ActionTool.PRICE_CHECK,
    provider="none",
    query_template="{symbol}",
    purpose="confirm price move vs noise",
    phase="phase1",
    default_weight=1.3,
)

VOLUME_REGIME_SHIFT = ActionTemplate(
    action_id="volume_regime_shift",
    tool=ActionTool.VOLUME_CHECK,
    provider="none",
    query_template="{symbol}",
    purpose="detect abnormal volume regime",
    phase="phase1",
    default_weight=1.0,
)


# ---------------------------------------------------------------------------
# Action registry
# ---------------------------------------------------------------------------

# All built-in actions
ALL_ACTIONS: list[ActionTemplate] = [
    # Phase 1 (broad, cheap, shallow)
    BREAKING_FOLLOWUP,
    GOOGLE_BROAD_SEARCH,
    X_REALTIME_RUMOR,
    PRICE_SPIKE_CHECK,
    VOLUME_REGIME_SHIFT,
    # Phase 2 (narrow, selective, deeper)
    NEWS_CONFIRMATION,
    FILING_CHECK,
    ANALYST_REACTION,
    GOOGLE_SENTIMENT,
    X_VOLUME_ALERTS,
    X_INSIDER_ACCOUNTS,
]

PHASE1_ACTIONS = [a for a in ALL_ACTIONS if a.phase in ("phase1", "any")]
PHASE2_ACTIONS = [a for a in ALL_ACTIONS if a.phase in ("phase2", "any")]


@dataclass
class ActionWeights:
    """Learnable weights over the action menu.

    Higher weight = more likely to be selected.  Weights are loaded from
    ``data/knowledge/search_strategies.json`` and updated by the offline
    policy learner.
    """
    weights: dict[str, float] = field(default_factory=dict)

    def get(self, action_id: str, default: float = 1.0) -> float:
        return self.weights.get(action_id, default)

    def set(self, action_id: str, weight: float) -> None:
        self.weights[action_id] = weight

    def to_dict(self) -> dict[str, float]:
        return dict(self.weights)

    @classmethod
    def from_defaults(cls) -> "ActionWeights":
        """Initialize weights from the built-in action defaults."""
        return cls(weights={a.action_id: a.default_weight for a in ALL_ACTIONS})

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> "ActionWeights":
        return cls(weights=dict(d))


def render_query(template: ActionTemplate, context: dict[str, str]) -> str:
    """Fill in a query template with context values.

    Missing keys are left as literal placeholders rather than crashing,
    so the LLM can still make sense of it.
    """
    try:
        return template.query_template.format_map(context)
    except KeyError:
        # Partial fill — leave unresolved placeholders
        result = template.query_template
        for k, v in context.items():
            result = result.replace("{" + k + "}", v)
        return result


# ---------------------------------------------------------------------------
# Hypothesis schema
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    """A competing narrative/explanation generated during Phase 1.

    Phase 2 selects top-K hypotheses and does one targeted follow-up each.
    """
    hypothesis_id: str
    label: str          # short label, e.g. "rumor_confirmation", "macro_spillover"
    description: str    # one-sentence description
    confidence: float   # 0-1 how plausible based on Phase 1 evidence
    suggested_action_ids: list[str]  # which Phase 2 actions could help confirm/deny
    evidence_trace_ids: list[str]    # which Phase 1 traces support this
    category: str = ""  # e.g. "fundamental", "technical", "sentiment", "macro"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "label": self.label,
            "description": self.description,
            "confidence": self.confidence,
            "suggested_action_ids": self.suggested_action_ids,
            "evidence_trace_ids": self.evidence_trace_ids,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hypothesis":
        return cls(
            hypothesis_id=str(d.get("hypothesis_id", "")),
            label=str(d.get("label", "")),
            description=str(d.get("description", "")),
            confidence=float(d.get("confidence", 0.0)),
            suggested_action_ids=list(d.get("suggested_action_ids") or []),
            evidence_trace_ids=list(d.get("evidence_trace_ids") or []),
            category=str(d.get("category", "")),
        )
