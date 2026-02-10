"""HTTP fetching for evidence acquisition.

Uses httpx (already a project dependency) for robust timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str | None
    content: bytes


def fetch_url(
    *,
    url: str,
    timeout_s: float = 20.0,
    max_bytes: int = 2_000_000,
    user_agent: str = "alpaca-news-trader/0.1",
) -> FetchResult:
    """Fetch a URL with strict bounds.

    Fails loudly on unexpected conditions.
    """

    headers = {"User-Agent": user_agent}
    with httpx.Client(follow_redirects=True, timeout=timeout_s, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        content = resp.content
        if len(content) > max_bytes:
            raise RuntimeError(f"Fetched content too large: bytes={len(content)} max={max_bytes} url={url}")
        return FetchResult(
            url=url,
            final_url=str(resp.url),
            status_code=int(resp.status_code),
            content_type=resp.headers.get("content-type"),
            content=content,
        )
