#!/usr/bin/env python
"""Standalone Schwab reauthorization script.

Run this BEFORE starting the main app when your Schwab refresh token
has expired (every 7 days). It initializes schwabdev.Client which
triggers the OAuth flow (opens browser, waits for you to paste the
callback URL), then exits. The refreshed tokens are saved to
~/.schwabdev/tokens.db and will be picked up by the main app.

Usage:
    uv run python scripts/schwab_reauth.py
"""
import os
import sys

# Ensure project root is on path so .env gets loaded
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

app_key = os.getenv("SCHWAB_APP_KEY")
app_secret = os.getenv("SCHWAB_APP_SECRET")

if not app_key or not app_secret:
    print("ERROR: SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env")
    input("Press Enter to close...")
    sys.exit(1)

print("Initializing Schwab client (this will trigger reauth if needed)...")
print()

import schwabdev
client = schwabdev.Client(app_key, app_secret)

# Quick validation: make a simple API call
try:
    resp = client.market_hours("equity")
    if resp.ok:
        print()
        print("SUCCESS: Schwab authentication is working.")
    else:
        print(f"WARNING: Auth succeeded but test call returned {resp.status_code}")
except Exception as e:
    print(f"WARNING: Auth may have succeeded but test call failed: {e}")

print()
input("Press Enter to close...")
