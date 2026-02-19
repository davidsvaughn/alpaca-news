"""Quick test to verify X stream burst collects tweets with the fixed rules.

Run: uv run python tests/test_x_stream_burst.py
"""
import json
import time
import threading
from pathlib import Path

def test_burst():
    from trader.online.x_stream_service import XStreamService, XStreamGuards, build_rules_for_symbols
    from trader.online.event_bus import EventBus

    bus = EventBus()
    guards = XStreamGuards(burst_ttl_minutes=1)  # 1 minute burst
    svc = XStreamService(bus=bus, data_dir=Path("data"), guards=guards, enabled=True)

    # Test rule generation (verify no context:166.*)
    symbols = ["NVDA", "AAPL", "UP"]
    rules = build_rules_for_symbols(symbols=symbols)
    print("=== Generated Rules ===")
    for r in rules:
        print(f"  {r.tag}: {r.value}")
        assert "context:166" not in r.value, f"FAIL: context:166 still in rule for {r.tag}!"

    # Collect events
    posts_received: list[dict] = []
    def on_event(event):
        if event.type == "x_post":
            posts_received.append(event.payload.get("post", {}))

    bus.subscribe(on_event)

    # Start burst
    print(f"\n=== Starting burst for {symbols} (30s timeout) ===")
    try:
        svc.start_burst(rules=rules, remove_rules_after=True)
    except Exception as e:
        print(f"FAIL: Could not start burst: {e}")
        return

    # Wait up to 30 seconds for tweets
    start = time.time()
    timeout = 30
    while time.time() - start < timeout:
        if posts_received:
            break
        time.sleep(1)
        elapsed = int(time.time() - start)
        if elapsed % 5 == 0:
            print(f"  Waiting... {elapsed}s, posts so far: {len(posts_received)}")

    # Stop burst
    svc.stop_burst()
    time.sleep(1)  # let it clean up

    # Check cache
    for sym in symbols:
        cached = svc.get_recent_posts(key=sym, limit=5)
        print(f"\nCache for {sym}: {len(cached)} posts")

    cached_all = svc.get_recent_posts(key="_all", limit=10)
    print(f"Cache for _all: {len(cached_all)} posts")

    elapsed = time.time() - start
    print(f"\n=== Results ===")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Total posts received via events: {len(posts_received)}")

    if posts_received:
        print(f"\nFirst post sample:")
        p = posts_received[0]
        text = p.get("data", {}).get("text", p.get("text", "?"))
        print(f"  Text: {text[:200]}")
        tags = [r.get("tag") for r in (p.get("matching_rules") or [])]
        print(f"  Matched tags: {tags}")
        print("\n*** SUCCESS: Burst collected tweets! ***")
    else:
        print("\n*** WARNING: No tweets collected in 30s ***")
        print("This could mean:")
        print("  - No one is tweeting about these tickers right now")
        print("  - X API connection issue")
        print("  - Rules not matching (check X API tier)")

    svc.stop()


if __name__ == "__main__":
    test_burst()
