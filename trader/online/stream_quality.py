"""Quality gate for X stream bursts.

Checks a sample of incoming tweets against the original news context
to determine if the stream is capturing relevant content or noise.
If irrelevant, suggests revised X API filter rule values for retry.
"""

from __future__ import annotations

from typing import Any

from trader.llm.client import LLMClient
from trader.llm.extract import extract_json
from trader.online.x_stream_service import QualityVerdict
from trader.xapi.rules import StreamRule


QUALITY_PROMPT = """You are evaluating whether an X/Twitter filtered stream is capturing relevant tweets.

## News Context
Headline: {headline}
Symbols: {symbols}
Summary: {summary}

## Current Filter Rules
{rules_text}

## Tweets Received (sample)
{tweets_text}

## Task
1. Determine if these tweets are RELEVANT to the news/symbols above.
   Irrelevant examples: ticker matching a common word (e.g. "PARA" matching Spanish "para"),
   unrelated topics, spam, etc.

2. If irrelevant, suggest revised X API filtered stream rule value strings that would
   better capture tweets about this company/stock. Use X API v2 filtered stream syntax
   (operators: OR, -, $cashtag, #hashtag, lang:, -is:retweet, from:, context:, entity:).
   Include the company name, not just the ticker, if the ticker is ambiguous.

Return STRICT JSON:
{{
  "relevant": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "short explanation",
  "revised_rule_values": null or ["rule value 1", "rule value 2"]
}}

Set revised_rule_values to null if tweets are relevant or if no better rules are obvious.
Each revised rule value is a complete X API rule string (e.g. "$PARA OR Paramount -is:retweet lang:en").
"""


def _format_tweets(posts: list[dict[str, Any]], max_tweets: int = 5) -> str:
    """Format tweet objects into readable text for the prompt."""
    lines = []
    for i, post in enumerate(posts[:max_tweets], 1):
        data = post.get("data", {})
        text = data.get("text", "")
        users = (post.get("includes") or {}).get("users", [])
        handle = users[0].get("username", "?") if users else "?"
        rules = [r.get("tag", "") for r in (post.get("matching_rules") or [])]
        lines.append(f"{i}. @{handle} (matched: {', '.join(rules)}): {text[:280]}")
    return "\n".join(lines)


def _format_rules(rules: list[StreamRule]) -> str:
    """Format current rules for the prompt."""
    lines = []
    for i, r in enumerate(rules, 1):
        lines.append(f'{i}. "{r.value}" (tag: {r.tag or "none"})')
    return "\n".join(lines)


def check_stream_quality(
    *,
    posts: list[dict[str, Any]],
    headline: str,
    symbols: list[str],
    summary: str,
    current_rules: list[StreamRule],
    llm: LLMClient,
    model: str = "gemini-3-flash",
    provider: str = "gemini",
) -> QualityVerdict:
    """Evaluate whether stream tweets are relevant to the news context."""
    prompt = QUALITY_PROMPT.format(
        headline=headline,
        symbols=", ".join(symbols),
        summary=summary or "(no summary)",
        rules_text=_format_rules(current_rules),
        tweets_text=_format_tweets(posts),
    )

    if provider == "gemini":
        res = llm.query_gemini(model=model, input_text=prompt, stage="stream_quality", purpose="stream_quality")
    elif provider == "grok":
        res = llm.query_grok(model=model, input_text=prompt, stage="stream_quality", purpose="stream_quality")
    elif provider == "openai":
        res = llm.query_openai(model=model, input_text=prompt, stage="stream_quality", purpose="stream_quality")
    else:
        raise ValueError(f"Unknown provider for stream quality check: {provider}")

    data = extract_json(res.text)

    revised = data.get("revised_rule_values")
    if revised is not None:
        revised = [str(v) for v in revised if v]

    return QualityVerdict(
        relevant=bool(data.get("relevant", True)),
        confidence=float(data.get("confidence", 0.5)),
        reasoning=str(data.get("reasoning", "")),
        revised_rule_values=revised if revised else None,
    )
