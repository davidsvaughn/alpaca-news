"""Quick test to verify x_search works with previous_response_id.

Run: uv run python tests/test_grok_x_search.py
"""
import json
import os
import time

def test_grok_with_previous_response_id():
    """Test that Grok uses x_search when previous_response_id is restored."""
    from openai import OpenAI

    key = os.environ.get("XAI_API_KEY")
    if not key:
        print("SKIP: XAI_API_KEY not set")
        return

    client = OpenAI(api_key=key, base_url="https://api.x.ai/v1/")
    model = os.getenv("XSEARCH_MODEL", "grok-4-1-fast-reasoning")

    tools = [
        {"type": "x_search"},
        {"type": "web_search"},
        {
            "type": "function",
            "name": "dummy_tool",
            "description": "A dummy tool that returns a static string. Call this AFTER doing your web/x searches.",
            "parameters": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        },
    ]

    system_prompt = (
        "You are a financial research analyst. "
        "Search X/Twitter for sentiment about the stock using x_search, "
        "and search the web using web_search. "
        "Then call dummy_tool with a summary. Be brief."
    )
    user_message = "What is the current sentiment around NVDA stock? Use x_search and web_search."

    print(f"=== Testing with previous_response_id (restored approach) ===")
    print(f"Model: {model}")

    input_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    t0 = time.time()
    # store=True (or omit) so previous_response_id works
    response = client.responses.create(
        model=model,
        input=input_messages,
        tools=tools,
    )

    # Collect tool types seen across all turns
    all_tool_types = set()
    turn = 0
    max_turns = 5

    _X_SEARCH_NAMES = {"x_keyword_search", "x_semantic_search"}

    def extract_tool_types(resp):
        for item in resp.output:
            item_type = getattr(item, "type", None)
            item_name = getattr(item, "name", "") or ""
            if item_type == "web_search_call":
                action = getattr(item, "action", None)
                query = getattr(action, "query", "") if action else ""
                print(f"  [web_search] query={query!r}")
                all_tool_types.add("web_search")
            elif item_type == "custom_tool_call" and item_name in _X_SEARCH_NAMES:
                raw_input = getattr(item, "input", "") or ""
                print(f"  [x_search/{item_name}] input={raw_input!r}")
                all_tool_types.add("x_search")
            elif item_type == "x_search_call":
                action = getattr(item, "action", None)
                query = getattr(action, "query", "") if action else ""
                print(f"  [x_search] query={query!r}")
                all_tool_types.add("x_search")
            elif item_type == "function_call":
                print(f"  [function_call] {getattr(item, 'name', '?')}({getattr(item, 'arguments', '')[:100]})")
                all_tool_types.add(f"function:{getattr(item, 'name', '?')}")

    extract_tool_types(response)

    while turn < max_turns:
        function_calls = [
            item for item in response.output
            if getattr(item, "type", None) == "function_call"
        ]
        if not function_calls:
            break
        turn += 1

        tool_results = []
        for call in function_calls:
            tool_results.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps({"status": "ok", "message": "dummy response"}),
            })

        # KEY: use previous_response_id for stateful continuation
        response = client.responses.create(
            model=model,
            input=tool_results,
            previous_response_id=response.id,
            tools=tools,
        )
        extract_tool_types(response)

    elapsed = time.time() - t0
    output_text = getattr(response, "output_text", "") or ""

    print(f"\n--- Results ---")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Tool types used: {all_tool_types}")
    print(f"x_search called: {'x_search' in all_tool_types}")
    print(f"web_search called: {'web_search' in all_tool_types}")
    print(f"Output (first 200 chars): {output_text[:200]}")

    if "x_search" not in all_tool_types:
        print("\n*** WARNING: x_search was NOT called! ***")
    else:
        print("\n*** SUCCESS: x_search WAS called! ***")


if __name__ == "__main__":
    test_grok_with_previous_response_id()
