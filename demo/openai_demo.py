#!/usr/bin/env python3
"""OpenAI Responses API demo — shows web_search and function tools.

Usage:
    uv run python demo/openai_demo.py [QUERY]

Requires: OPENAI_API_KEY in .env or environment.
See docs/src/OPENAI.md for full documentation.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

QUERY = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What is the latest news about TSLA stock?"


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if isinstance(data, str):
        print(data[:2000])
        if len(data) > 2000:
            print(f"  ... ({len(data)} chars, truncated)")
    else:
        print(json.dumps(data, indent=2, default=str))


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: Set OPENAI_API_KEY in .env or environment")
        sys.exit(1)

    from openai import OpenAI

    model = os.getenv("RESEARCH_MODEL", "gpt-5.1")
    client = OpenAI(api_key=api_key)

    print(f"OpenAI Responses API Demo")
    print(f"Model: {model}")
    print(f"Query: {QUERY}")

    # -----------------------------------------------------------------------
    # Demo 1: Server-side web_search
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 1: web_search (server-side)")
    print(f"{'='*60}")
    print("Sending query with web_search tool enabled...")

    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "You are a financial research assistant. Be concise."},
            {"role": "user", "content": QUERY},
        ],
        tools=[{"type": "web_search"}],
    )

    # Show tool calls
    print("\n--- Output Items ---")
    for item in response.output:
        item_type = getattr(item, "type", "unknown")
        if item_type == "web_search_call":
            print(f"  [web_search] query: {getattr(item, 'query', '?')}")
        elif item_type == "message":
            pass  # handled below

    pp("Final Response", getattr(response, "output_text", ""))

    if response.usage:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        details = getattr(response.usage, "output_tokens_details", None)
        if details:
            usage["reasoning_tokens"] = getattr(details, "reasoning_tokens", 0)
        pp("Token Usage", usage)

    # -----------------------------------------------------------------------
    # Demo 2: Reasoning with effort control
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 2: Reasoning (effort=medium)")
    print(f"{'='*60}")

    response2 = client.responses.create(
        model=model,
        input=[
            {"role": "user", "content": "Should I buy NVDA? Give a brief bull and bear case."},
        ],
        reasoning={"effort": "medium", "summary": "auto"},
    )

    # Show reasoning
    for item in response2.output:
        if getattr(item, "type", "") == "reasoning":
            summaries = getattr(item, "summary", []) or []
            for s in summaries:
                text = getattr(s, "text", "")
                if text:
                    print(f"  [Reasoning] {text[:200]}")

    pp("Response", getattr(response2, "output_text", ""))

    # -----------------------------------------------------------------------
    # Demo 3: Function tool calling
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 3: Function Tool Calling")
    print(f"{'='*60}")

    response3 = client.responses.create(
        model=model,
        input=[{"role": "user", "content": "Check the current price of MSFT"}],
        tools=[
            {
                "type": "function",
                "name": "check_price",
                "description": "Get current stock price.",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            },
        ],
    )

    for item in response3.output:
        if getattr(item, "type", "") == "function_call":
            print(f"  Called: {item.name}({item.arguments})")
            print(f"  (Pipeline would execute this locally and continue the conversation)")

    # Cost summary
    print(f"\n{'='*60}")
    print(f"  Cost Summary")
    print(f"{'='*60}")
    from trader.llm.pricing import OPENAI_PRICING

    rates = OPENAI_PRICING.get(model, {})
    if rates and response.usage:
        ic = response.usage.input_tokens * rates.get("input", 0) / 1_000_000
        oc = response.usage.output_tokens * rates.get("output", 0) / 1_000_000
        print(f"  Model: {model}")
        print(f"  Input:  {response.usage.input_tokens} tokens × ${rates.get('input', 0)}/1M = ${ic:.6f}")
        print(f"  Output: {response.usage.output_tokens} tokens × ${rates.get('output', 0)}/1M = ${oc:.6f}")
        print(f"  web_search: $0.01 per call")
        print(f"  Total (tokens only): ${ic + oc:.6f}")


if __name__ == "__main__":
    main()
