"""X API usage polling.

Docs (Context7): GET /2/usage/tweets?days=7..90
"""

from __future__ import annotations

from typing import Any

from trader.xapi.client import XApiClient


USAGE_PATH = "/2/usage/tweets"


def get_usage(*, client: XApiClient, days: int = 7, usage_fields: str | None = None) -> dict[str, Any]:
    if not (1 <= days <= 90):
        raise ValueError("days must be 1..90")
    params: dict[str, Any] = {"days": days}
    # Docs vary between usage.fields string vs enum list. Use string form.
    if usage_fields:
        params["usage.fields"] = usage_fields
    return client.get(USAGE_PATH, params=params).json()
