#!/usr/bin/env python3
"""X API v2 demo — shows stream rules, usage tracking, and rule building.

Usage:
    # Show current rules and usage (read-only, safe)
    uv run python demo/x_api_demo.py

    # Dry-run: build rules for symbols without actually adding them
    uv run python demo/x_api_demo.py --rules NVDA PARA GOOGL

    # Live burst demo (adds rules, streams for 30s, deletes rules)
    uv run python demo/x_api_demo.py --burst NVDA

Requires: X_BEARER_TOKEN in .env or environment.
See docs/src/X-API.md for full documentation.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


def pp(label: str, data):
    """Pretty-print a section."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2, default=str))


def demo_status():
    """Show current rules and API usage (read-only)."""
    from trader.xapi.client import XApiClient
    from trader.xapi.rules import get_rules

    client = XApiClient()

    # Current rules
    rules = get_rules(client=client)
    pp("Current Stream Rules", rules)

    # API usage
    try:
        usage_resp = client.get("/2/usage/tweets", params={"usage.fields": "cap_reset_day"})
        pp("API Usage (7-day window)", usage_resp.json())
    except Exception as e:
        print(f"\n  Usage endpoint error: {e}")
        print("  (Usage endpoint may not be available on all tiers)")


def demo_rules(symbols: list[str]):
    """Show what rules would be built for given symbols (dry-run)."""
    from trader.online.x_stream_service import build_rules_for_symbols

    print(f"\nBuilding rules for symbols: {symbols}")

    rules = build_rules_for_symbols(symbols=symbols)

    print(f"\nGenerated {len(rules)} rules:")
    for r in rules:
        print(f"  Tag: {r.tag}")
        print(f"  Value: {r.value}")
        print()

    # Dry-run via API
    from trader.xapi.client import XApiClient
    from trader.xapi.rules import add_rules

    client = XApiClient()
    result = add_rules(client=client, rules=rules, dry_run=True)
    pp("Dry-Run Result (rules NOT actually added)", result)


def demo_burst(symbol: str):
    """Run a short burst: add rules, stream 30s, remove rules."""
    from trader.online.x_stream_service import build_rules_for_symbols
    from trader.xapi.client import XApiClient
    from trader.xapi.rules import add_rules, delete_rules
    from trader.xapi.stream import StreamParams

    client = XApiClient()
    rules = build_rules_for_symbols(symbols=[symbol])

    if not rules:
        print(f"No rules generated for {symbol}")
        return

    print(f"\nBurst Demo — Symbol: {symbol}")
    print(f"Rule: {rules[0].value}")
    print("Adding rules...")

    result = add_rules(client=client, rules=rules)
    rule_ids = [r["id"] for r in result.get("data", [])]
    pp("Rules Added", result)

    if not rule_ids:
        print("ERROR: No rules were created")
        return

    print(f"\nStreaming for 30 seconds... (Ctrl+C to stop early)\n")

    params = StreamParams()
    try:
        resp = client.session.get(
            f"{client.config.base_url}/2/tweets/search/stream",
            headers={
                "Authorization": f"Bearer {client.config.bearer_token}",
                "User-Agent": client.config.user_agent,
            },
            params={
                "tweet.fields": params.tweet_fields,
                "expansions": params.expansions,
                "user.fields": params.user_fields,
            },
            stream=True,
            timeout=(3.05, 90),
        )
        resp.raise_for_status()

        start = time.time()
        count = 0
        for line in resp.iter_lines():
            if time.time() - start > 30:
                break
            if not line:
                continue  # keep-alive
            try:
                tweet = json.loads(line)
                data = tweet.get("data", {})
                text = data.get("text", "")[:120]
                username = "?"
                for u in tweet.get("includes", {}).get("users", []):
                    username = u.get("username", "?")
                    break
                tags = [r.get("tag", "?") for r in tweet.get("matching_rules", [])]
                count += 1
                print(f"  [{count}] @{username} [{','.join(tags)}]: {text}")
            except json.JSONDecodeError:
                continue

        print(f"\nReceived {count} tweets in {time.time() - start:.1f}s")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        print(f"\nStream error: {e}")
    finally:
        print(f"\nDeleting rules: {rule_ids}")
        delete_result = delete_rules(client=client, ids=rule_ids)
        pp("Rules Deleted", delete_result)


def main():
    if not os.getenv("X_BEARER_TOKEN"):
        print("ERROR: Set X_BEARER_TOKEN in .env or environment")
        sys.exit(1)

    args = sys.argv[1:]

    if "--burst" in args:
        idx = args.index("--burst")
        symbol = args[idx + 1] if idx + 1 < len(args) else "NVDA"
        demo_burst(symbol)
    elif "--rules" in args:
        idx = args.index("--rules")
        symbols = args[idx + 1:] or ["NVDA"]
        demo_rules(symbols)
    else:
        demo_status()


if __name__ == "__main__":
    main()
