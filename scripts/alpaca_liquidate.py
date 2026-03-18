#!/usr/bin/env python3
"""Cancel all orders and close all positions on Alpaca paper accounts.

Usage:
    uv run python scripts/alpaca_liquidate.py          # all accounts
    uv run python scripts/alpaca_liquidate.py 1         # Paper1 only
    uv run python scripts/alpaca_liquidate.py 1 2       # Paper1 and Paper2
"""

import os
import sys
import time

from dotenv import load_dotenv
from trader.market.alpaca_env import ALPACA_ACCOUNT_NUMBERS, get_alpaca_account_env

load_dotenv(override=True)

from alpaca.trading.client import TradingClient


def get_accounts():
    """Return list of (account_number, name, key, secret) for configured paper accounts."""
    accounts = []
    for account_number in ALPACA_ACCOUNT_NUMBERS:
        key = get_alpaca_account_env("ALPACA_API_KEY", account_number)
        secret = get_alpaca_account_env("ALPACA_SECRET_KEY", account_number)
        if key and secret:
            name = get_alpaca_account_env(
                "ALPACA_PAPER_NAME",
                account_number,
                default=f"AlpacaPaper{account_number}",
            )
            accounts.append((account_number, name, key, secret))
    return accounts


def liquidate_account(name: str, key: str, secret: str):
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")
    client = TradingClient(key, secret, paper=True)

    # --- Cancel all orders ---
    orders = client.get_orders()
    print(f"\nOpen orders: {len(orders)}")
    for o in orders:
        print(f"  {o.symbol} {o.side.value} {o.type.value} status={o.status.value} id={o.id}")

    if orders:
        print("\nCancelling all orders...")
        try:
            responses = client.cancel_orders()
            for r in responses:
                print(f"  {r.id}: status={r.status}")
        except Exception as e:
            print(f"  Error: {e}")
        time.sleep(3)

    # --- Close all positions ---
    positions = client.get_all_positions()
    print(f"\nOpen positions: {len(positions)}")
    for p in positions:
        print(f"  {p.symbol}: qty={p.qty} market_value={p.market_value}")

    if positions:
        # Try bulk close first
        print("\nClosing all positions (cancel_orders=True)...")
        try:
            client.close_all_positions(cancel_orders=True)
            print("  Bulk close request sent.")
        except Exception as e:
            print(f"  Bulk close failed: {e}")
            # Fall back to individual closes
            print("  Falling back to individual closes...")
            for p in positions:
                try:
                    client.close_position(p.symbol)
                    print(f"    {p.symbol}: close request sent")
                except Exception as e2:
                    print(f"    {p.symbol}: FAILED — {e2}")
        time.sleep(3)

    # --- Final status ---
    remaining_orders = client.get_orders()
    remaining_pos = client.get_all_positions()
    print(f"\nFinal: {len(remaining_orders)} orders, {len(remaining_pos)} positions")

    stuck = False
    for o in remaining_orders:
        print(f"  STUCK ORDER: {o.symbol} {o.side.value} {o.type.value} status={o.status.value} id={o.id}")
        stuck = True
    for p in remaining_pos:
        print(f"  STUCK POSITION: {p.symbol} qty={p.qty}")
        stuck = True

    if stuck:
        print("\n  *** Some orders/positions could not be cleared. ***")
        print("  *** Reset the paper account in the Alpaca dashboard, ***")
        print("  *** or contact support@alpaca.markets with the order IDs above. ***")

    return not stuck


def main():
    accounts = get_accounts()
    if not accounts:
        print("No Alpaca accounts found in .env")
        sys.exit(1)

    # Filter by account numbers if specified
    if len(sys.argv) > 1:
        selected = {int(x) for x in sys.argv[1:]}
        accounts = [account for account in accounts if account[0] in selected]

    if not accounts:
        print("No matching accounts found.")
        sys.exit(1)

    print(f"Liquidating {len(accounts)} account(s): {', '.join(a[1] for a in accounts)}")

    all_clean = True
    for _account_number, name, key, secret in accounts:
        if not liquidate_account(name, key, secret):
            all_clean = False

    print(f"\n{'=' * 60}")
    if all_clean:
        print("All accounts fully liquidated.")
    else:
        print("Some accounts have stuck orders/positions. See above.")
    print()


if __name__ == "__main__":
    main()
