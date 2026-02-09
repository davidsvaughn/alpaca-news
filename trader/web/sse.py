"""Server-Sent Events helpers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import AsyncIterator

from fastapi.responses import StreamingResponse

from trader.online.orchestrator import EventBus, PipelineEvent


def sse_format(*, event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_response(*, bus: EventBus, ping_interval_s: float = 10.0) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        q: asyncio.Queue[PipelineEvent] = asyncio.Queue()

        def on_event(evt: PipelineEvent) -> None:
            q.put_nowait(evt)

        unsub = bus.subscribe(on_event)

        try:
            # Initial hello
            yield sse_format(event="hello", data={"ok": True})
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=ping_interval_s)
                    yield sse_format(event=evt.type, data=evt.payload)
                except asyncio.TimeoutError:
                    yield sse_format(event="ping", data={"ok": True})
        finally:
            unsub()

    return StreamingResponse(gen(), media_type="text/event-stream")
