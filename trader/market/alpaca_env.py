"""Helpers for resolving Alpaca multi-account environment variables.

`*_1` is the canonical name for the first paper account. Legacy unsuffixed
vars are still accepted as a fallback for account 1 so older local env files
keep working during migration.
"""

from __future__ import annotations

import os
from typing import Final

ALPACA_ACCOUNT_NUMBERS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5)


def alpaca_account_env_keys(base_name: str, account_number: int = 1) -> tuple[str, ...]:
    """Return env var names to try for an Alpaca account field."""
    if account_number < 1:
        raise ValueError(f"account_number must be >= 1, got {account_number}")
    canonical = f"{base_name}_{account_number}"
    if account_number == 1:
        return (canonical, base_name)
    return (canonical,)


def get_alpaca_account_env(
    base_name: str,
    account_number: int = 1,
    *,
    default: str | None = None,
) -> str | None:
    """Return the first configured value for the given Alpaca account field."""
    for env_name in alpaca_account_env_keys(base_name, account_number):
        value = os.getenv(env_name)
        if value:
            return value
    return default
