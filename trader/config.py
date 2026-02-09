"""Runtime configuration for the trading research assistant.

Phase 1 goal:
- Centralize env var loading
- Provide strongly-typed accessors with sensible defaults
- Keep config dependency-light (stdlib + python-dotenv)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv


ProviderName = Literal["openai", "grok", "gemini"]


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError as e:
        raise ValueError(f"Env var {name} must be a float; got {val!r}") from e


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError as e:
        raise ValueError(f"Env var {name} must be an int; got {val!r}") from e


def _env_str(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


@dataclass(frozen=True)
class Settings:
    # Modes
    learning_mode: bool
    trading_mode: Literal["paper", "live"]
    debug: bool

    # Paths
    alpaca_output_dir: str
    data_dir: str
    sqlite_path: str
    snapshots_dir: str

    # Models/providers (stage configs)
    triage_provider: ProviderName
    triage_model: str
    research_provider: ProviderName
    research_model: str
    sentiment_provider: ProviderName
    sentiment_model: str
    xsearch_provider: ProviderName
    xsearch_model: str

    # Budgets/limits
    max_daily_cost: float
    max_cost_per_news_item: float
    max_total_hops: int
    max_web_searches_per_item: int
    max_x_searches_per_item: int

    # SSE
    sse_ping_interval_s: float


def load_settings(*, dotenv_path: str | None = None) -> Settings:
    """Load settings from environment.

    If dotenv_path is None, loads from default `.env` if present.
    """

    load_dotenv(dotenv_path=dotenv_path)

    learning_mode = _env_bool("LEARNING_MODE", False)
    trading_mode = (os.getenv("TRADING_MODE") or "paper").strip().lower()
    if trading_mode not in ("paper", "live"):
        raise ValueError("TRADING_MODE must be 'paper' or 'live'")

    debug = _env_bool("DEBUG", False)

    alpaca_output_dir = _env_str("ALPACA_OUTPUT_DIR", "output/alpaca") or "output/alpaca"
    data_dir = _env_str("DATA_DIR", "data") or "data"
    sqlite_path = _env_str("SQLITE_PATH", os.path.join(data_dir, "trader.db")) or os.path.join(
        data_dir, "trader.db"
    )
    snapshots_dir = _env_str("SNAPSHOTS_DIR", os.path.join(data_dir, "snapshots")) or os.path.join(
        data_dir, "snapshots"
    )

    triage_provider = (os.getenv("TRIAGE_PROVIDER") or "grok").strip().lower()  # type: ignore[assignment]
    research_provider = (os.getenv("RESEARCH_PROVIDER") or "openai").strip().lower()  # type: ignore[assignment]
    sentiment_provider = (os.getenv("SENTIMENT_PROVIDER") or "gemini").strip().lower()  # type: ignore[assignment]
    xsearch_provider = (os.getenv("XSEARCH_PROVIDER") or "grok").strip().lower()  # type: ignore[assignment]
    for p in (triage_provider, research_provider, sentiment_provider, xsearch_provider):
        if p not in ("openai", "grok", "gemini"):
            raise ValueError(f"Unknown provider: {p}")

    triage_model = os.getenv("TRIAGE_MODEL") or "grok-4.1-fast-reasoning"
    research_model = os.getenv("RESEARCH_MODEL") or "gpt-5-mini"
    sentiment_model = os.getenv("SENTIMENT_MODEL") or "gemini-3-flash-preview"
    xsearch_model = os.getenv("XSEARCH_MODEL") or "grok-4.1-fast-reasoning"

    max_daily_cost = _env_float("MAX_DAILY_COST", 5.00)
    max_cost_per_news_item = _env_float("MAX_COST_PER_NEWS_ITEM", 0.50)
    max_total_hops = _env_int("MAX_TOTAL_HOPS", 3)
    max_web_searches_per_item = _env_int("MAX_WEB_SEARCHES_PER_ITEM", 3)
    max_x_searches_per_item = _env_int("MAX_X_SEARCHES_PER_ITEM", 2)

    sse_ping_interval_s = _env_float("SSE_PING_INTERVAL_S", 10.0)

    return Settings(
        learning_mode=learning_mode,
        trading_mode=trading_mode,  # type: ignore[arg-type]
        debug=debug,
        alpaca_output_dir=alpaca_output_dir,
        data_dir=data_dir,
        sqlite_path=sqlite_path,
        snapshots_dir=snapshots_dir,
        triage_provider=triage_provider,  # type: ignore[arg-type]
        triage_model=triage_model,
        research_provider=research_provider,  # type: ignore[arg-type]
        research_model=research_model,
        sentiment_provider=sentiment_provider,  # type: ignore[arg-type]
        sentiment_model=sentiment_model,
        xsearch_provider=xsearch_provider,  # type: ignore[arg-type]
        xsearch_model=xsearch_model,
        max_daily_cost=max_daily_cost,
        max_cost_per_news_item=max_cost_per_news_item,
        max_total_hops=max_total_hops,
        max_web_searches_per_item=max_web_searches_per_item,
        max_x_searches_per_item=max_x_searches_per_item,
        sse_ping_interval_s=sse_ping_interval_s,
    )
