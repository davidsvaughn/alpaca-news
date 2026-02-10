"""FastAPI application for monitoring pipeline progress."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trader.config import Settings
from trader.online.event_bus import EventBus
from trader.web.sse import sse_response


def create_app(*, settings: Settings, bus: EventBus) -> FastAPI:
    app = FastAPI(title="alpaca-news dashboard")

    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("feed.html", {"request": request})

    @app.get("/events")
    async def events():
        return sse_response(bus=bus, ping_interval_s=settings.sse_ping_interval_s)

    return app
