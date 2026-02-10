"""X filtered stream consumer.

Docs (Context7): GET /2/tweets/search/stream

Implementation notes:
- Use requests streaming (iter_lines)
- Expect keep-alive newlines
- Reconnect with exponential backoff on disconnect and on 429/5xx
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Generator

import requests

from trader.xapi.client import XApiClient


STREAM_PATH = "/2/tweets/search/stream"


@dataclass(frozen=True)
class StreamParams:
    tweet_fields: str = "created_at,author_id,lang,public_metrics"
    expansions: str = "author_id"
    user_fields: str = "username"

    def to_query(self) -> dict[str, str]:
        return {
            "tweet.fields": self.tweet_fields,
            "expansions": self.expansions,
            "user.fields": self.user_fields,
        }


def stream_posts(
    *,
    client: XApiClient,
    params: StreamParams | None = None,
    stop_after_s: float | None = None,
    max_backoff_s: float = 60.0,
) -> Generator[dict[str, Any], None, None]:
    """Yield decoded JSON objects from the stream.

    This generator reconnects indefinitely unless stop_after_s is set.
    """

    q = (params or StreamParams()).to_query()
    start = time.time()
    backoff = 1.0

    while True:
        if stop_after_s is not None and (time.time() - start) >= stop_after_s:
            return

        url = f"{client.config.base_url}{STREAM_PATH}"
        try:
            with client.session.get(
                url,
                headers={
                    "Authorization": f"Bearer {client.config.bearer_token}",
                    "User-Agent": client.config.user_agent,
                },
                params=q,
                stream=True,
                timeout=(3.05, 90.0),
            ) as resp:
                resp.raise_for_status()
                backoff = 1.0
                for line in resp.iter_lines():
                    if stop_after_s is not None and (time.time() - start) >= stop_after_s:
                        return
                    if not line:
                        continue
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except Exception:
                        # Fail loudly: emit a structured error object so caller can log it and decide.
                        # We do not silently drop malformed payloads.
                        raise RuntimeError(f"Failed to decode stream line: {line[:200]!r}")

        except requests.HTTPError as e:
            # 429/5xx common; backoff and retry.
            time.sleep(backoff)
            backoff = min(backoff * 2.0, max_backoff_s)
            continue
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2.0, max_backoff_s)
            continue
