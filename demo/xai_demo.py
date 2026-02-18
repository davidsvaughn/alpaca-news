#!/usr/bin/env python3
"""xAI / Grok API demo — shows server-side x_search and web_search.

Usage:
    uv run python demo/xai_demo.py [QUERY]

Requires: XAI_API_KEY in .env or environment.
See docs/src/X-AI.md for full documentation.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

QUERY = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What is the latest news about NVDA stock?"


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
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        print("ERROR: Set XAI_API_KEY in .env or environment")
        sys.exit(1)

    from openai import OpenAI

    base_url = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1/")
    model = os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")

    client = OpenAI(api_key=api_key, base_url=base_url)

    print(f"xAI / Grok Demo")
    print(f"Model: {model}")
    print(f"Query: {QUERY}")

    # -----------------------------------------------------------------------
    # Demo 1: x_search (X/Twitter search — unique to Grok)
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 1: x_search + web_search (server-side)")
    print(f"{'='*60}")
    print("Sending query with both x_search and web_search tools enabled...")

    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "You are a financial research assistant. Be concise."},
            {"role": "user", "content": QUERY},
        ],
        tools=[
            {"type": "x_search"},
            {"type": "web_search"},
        ],
    )

    # Show what tools were called
    print("\n--- Response Output Items ---")
    for item in response.output:
        item_type = getattr(item, "type", "unknown")
        if item_type == "x_search_call":
            query = getattr(item, "query", "?")
            print(f"  [x_search] query: {query}")
        elif item_type == "web_search_call":
            query = getattr(item, "query", "?")
            print(f"  [web_search] query: {query}")
        elif item_type == "message":
            pass  # handled below
        else:
            print(f"  [{item_type}]")

    # Show the final text
    output_text = getattr(response, "output_text", "")
    pp("Final Response", output_text)

    # Show usage
    if response.usage:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        pp("Token Usage", usage)

    # -----------------------------------------------------------------------
    # Demo 2: Function tool calling (like our pipeline does)
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 2: Function Tool Calling")
    print(f"{'='*60}")
    print("Sending query with a custom function tool...")

    response2 = client.responses.create(
        model=model,
        input=[
            {"role": "user", "content": "What is the current price of AAPL?"},
        ],
        tools=[
            {
                "type": "function",
                "name": "check_price",
                "description": "Get current stock price quote.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Stock ticker"},
                    },
                    "required": ["symbol"],
                },
            },
        ],
    )

    for item in response2.output:
        item_type = getattr(item, "type", "unknown")
        if item_type == "function_call":
            print(f"  Function called: {item.name}({item.arguments})")
            print(f"  Call ID: {item.call_id}")
            print("  (In our pipeline, we'd execute this locally and send the result back)")
        elif item_type == "message":
            text = "".join(
                getattr(p, "text", "") for p in getattr(item, "content", [])
            )
            if text:
                print(f"  Message: {text[:200]}")

    # Cost estimate
    print(f"\n{'='*60}")
    print(f"  Cost Estimate")
    print(f"{'='*60}")
    from trader.llm.pricing import GROK_PRICING

    rates = GROK_PRICING.get(model, {})
    if rates and response.usage:
        input_cost = response.usage.input_tokens * rates.get("input", 0) / 1_000_000
        output_cost = response.usage.output_tokens * rates.get("output", 0) / 1_000_000
        print(f"  Model: {model}")
        print(f"  Input:  {response.usage.input_tokens} tokens × ${rates.get('input', 0)}/1M = ${input_cost:.6f}")
        print(f"  Output: {response.usage.output_tokens} tokens × ${rates.get('output', 0)}/1M = ${output_cost:.6f}")
        print(f"  x_search/web_search: $0.005 per call")
        print(f"  Total (tokens only): ${input_cost + output_cost:.6f}")


if __name__ == "__main__":
    main()
