#!/usr/bin/env python3
"""Google Gemini API demo — shows Google Search grounding and function tools.

Usage:
    uv run python demo/gemini_demo.py [QUERY]

Requires: GOOGLE_API_KEY in .env or environment.
See docs/src/GEMINI.md for full documentation.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

QUERY = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "What is happening with AMZN stock today?"


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
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: Set GOOGLE_API_KEY in .env or environment")
        sys.exit(1)

    from google import genai
    from google.genai import types

    model = os.getenv("SENTIMENT_MODEL", "gemini-3-flash-preview")
    client = genai.Client(api_key=api_key)

    print(f"Google Gemini Demo")
    print(f"Model: {model}")
    print(f"Query: {QUERY}")

    # -----------------------------------------------------------------------
    # Demo 1: Google Search grounding (unique to Gemini)
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 1: Google Search Grounding")
    print(f"{'='*60}")
    print("Sending query with GoogleSearch grounding enabled...")

    response = client.models.generate_content(
        model=model,
        contents=[
            types.Content(role="user", parts=[types.Part.from_text(text=QUERY)]),
        ],
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction="You are a financial research assistant. Be concise.",
        ),
    )

    # Show grounding metadata
    for candidate in response.candidates or []:
        gm = getattr(candidate, "grounding_metadata", None)
        if gm:
            queries = getattr(gm, "web_search_queries", []) or []
            print(f"\n  Google Search queries ({len(queries)}):")
            for q in queries:
                print(f"    - {q}")

            chunks = getattr(gm, "grounding_chunks", []) or []
            if chunks:
                print(f"\n  Grounding sources ({len(chunks)}):")
                for chunk in chunks[:5]:
                    web = getattr(chunk, "web", None)
                    if web:
                        print(f"    - {getattr(web, 'title', '?')}: {getattr(web, 'uri', '?')}")

    pp("Response", getattr(response, "text", ""))

    # Usage
    um = getattr(response, "usage_metadata", None)
    if um:
        usage = {
            "prompt_tokens": getattr(um, "prompt_token_count", 0),
            "candidates_tokens": getattr(um, "candidates_token_count", 0),
            "total_tokens": getattr(um, "total_token_count", 0),
            "thoughts_tokens": getattr(um, "thoughts_token_count", 0),
        }
        pp("Token Usage", usage)

    # -----------------------------------------------------------------------
    # Demo 2: Function tools + Google Search (combined — our key advantage)
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 2: Google Search + Function Tools (combined)")
    print(f"{'='*60}")
    print("This is what makes our Gemini runner special — PydanticAI can't do this.\n")

    func_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="check_price",
                description="Get current stock price quote for a ticker.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Stock ticker"},
                    },
                    "required": ["symbol"],
                },
            ),
        ],
    )

    response2 = client.models.generate_content(
        model=model,
        contents=[
            types.Content(role="user", parts=[
                types.Part.from_text(text="What is the latest on MSFT? Also check the current price."),
            ]),
        ],
        config=types.GenerateContentConfig(
            tools=[
                types.Tool(google_search=types.GoogleSearch()),  # Grounding
                func_tool,                                        # Function tools
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    # Show function calls
    function_calls = getattr(response2, "function_calls", None) or []
    if function_calls:
        for fc in function_calls:
            print(f"  Function called: {fc.name}({dict(fc.args) if fc.args else {}})")
            print("  (Pipeline would execute this locally and send result back)")
    else:
        print("  No function calls made (model answered from grounding alone)")

    # Show grounding
    for candidate in response2.candidates or []:
        gm = getattr(candidate, "grounding_metadata", None)
        if gm:
            queries = getattr(gm, "web_search_queries", []) or []
            if queries:
                print(f"\n  Google Search queries: {queries}")

    text = getattr(response2, "text", "")
    if text:
        pp("Response (may be partial if function calls pending)", text)

    # -----------------------------------------------------------------------
    # Demo 3: Thinking / reasoning
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Demo 3: Thinking (ThinkingConfig)")
    print(f"{'='*60}")

    response3 = client.models.generate_content(
        model=model,
        contents=[
            types.Content(role="user", parts=[
                types.Part.from_text(text="Is GOOGL overvalued? Think step by step."),
            ]),
        ],
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
    )

    # Show thinking parts
    if response3.candidates:
        for part in response3.candidates[0].content.parts or []:
            if getattr(part, "thought", False) and part.text:
                print(f"  [Thinking] {part.text[:300]}...")
                break

    pp("Response", getattr(response3, "text", ""))

    # Cost estimate
    print(f"\n{'='*60}")
    print(f"  Cost Summary")
    print(f"{'='*60}")
    from trader.llm.pricing import GEMINI_PRICING

    rates = GEMINI_PRICING.get(model, {})
    if rates and um:
        input_rate = rates.get("input_low", 0)
        output_rate = rates.get("output", 0)
        prompt_tok = getattr(um, "prompt_token_count", 0) or 0
        cand_tok = getattr(um, "candidates_token_count", 0) or 0
        ic = prompt_tok * input_rate / 1_000_000
        oc = cand_tok * output_rate / 1_000_000
        print(f"  Model: {model}")
        print(f"  Input:  {prompt_tok} tokens × ${input_rate}/1M = ${ic:.6f}")
        print(f"  Output: {cand_tok} tokens × ${output_rate}/1M = ${oc:.6f}")
        print(f"  Google Search grounding: ~$0.014 per query")
        print(f"  Total (tokens only): ${ic + oc:.6f}")


if __name__ == "__main__":
    main()
