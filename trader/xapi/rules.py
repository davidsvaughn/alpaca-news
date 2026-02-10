"""X filtered stream rule management.

Docs (Context7):
- GET  /2/tweets/search/stream/rules
- POST /2/tweets/search/stream/rules
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trader.xapi.client import XApiClient


RULES_PATH = "/2/tweets/search/stream/rules"


@dataclass(frozen=True)
class StreamRule:
    value: str
    tag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"value": self.value}
        if self.tag:
            d["tag"] = self.tag
        return d


def get_rules(*, client: XApiClient) -> dict[str, Any]:
    return client.get(RULES_PATH).json()


def add_rules(*, client: XApiClient, rules: list[StreamRule], dry_run: bool = False) -> dict[str, Any]:
    payload = {"add": [r.to_dict() for r in rules]}
    params = {"dry_run": "true"} if dry_run else None
    return client.post(RULES_PATH, json=payload, params=params).json()


def delete_rules(*, client: XApiClient, ids: list[str], dry_run: bool = False) -> dict[str, Any]:
    payload = {"delete": {"ids": ids}}
    params = {"dry_run": "true"} if dry_run else None
    return client.post(RULES_PATH, json=payload, params=params).json()


def delete_all_rules(*, client: XApiClient, dry_run: bool = False) -> dict[str, Any]:
    # Per docs, delete_all should be specified without other params.
    params = {"delete_all": "true"}
    if dry_run:
        params["dry_run"] = "true"
    return client.post(RULES_PATH, json={}, params=params).json()
