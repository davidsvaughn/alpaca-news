"""Low-level X API client.

We intentionally keep this dependency-light (requests only) and explicit.

Auth:
- Bearer token (OAuth 2.0 app-only) is sufficient for read-only endpoints:
  - filtered stream rules
  - filtered stream
  - usage endpoints

Base URL (Context7 / X docs): https://api.x.com
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class XApiConfig:
    base_url: str
    bearer_token: str
    user_agent: str = "alpaca-news-trader/0.1"


def load_xapi_config() -> XApiConfig:
    base_url = (os.getenv("X_API_BASE_URL") or "https://api.x.com").rstrip("/")
    bearer = os.getenv("X_BEARER_TOKEN") or ""
    if not bearer:
        raise RuntimeError("Missing X_BEARER_TOKEN in environment")
    return XApiConfig(base_url=base_url, bearer_token=bearer)


class XApiClient:
    def __init__(self, *, config: XApiConfig | None = None, session: requests.Session | None = None) -> None:
        self.config = config or load_xapi_config()
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.bearer_token}",
            "User-Agent": self.config.user_agent,
        }

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | tuple[float, float] = (3.05, 30.0),
    ) -> requests.Response:
        url = f"{self.config.base_url}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=timeout)
        resp.raise_for_status()
        return resp

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        params: dict[str, Any] | None = None,
        timeout: float | tuple[float, float] = (3.05, 30.0),
    ) -> requests.Response:
        url = f"{self.config.base_url}{path}"
        resp = self.session.post(url, headers=self._headers(), params=params, json=json, timeout=timeout)
        resp.raise_for_status()
        return resp
