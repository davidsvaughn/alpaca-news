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


def infer_provider_from_model(model: str) -> ProviderName:
    """Infer provider from model prefix.

    Supported:
    - gemini* -> gemini
    - gpt* / o1* / o3* / o4* -> openai
    - grok* -> grok
    """
    m = (model or "").strip().lower()
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("grok"):
        return "grok"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    raise ValueError(
        f"Cannot infer provider from model {model!r}. "
        "Expected model starting with 'gemini', 'grok', or 'gpt'/'o1'/'o3'/'o4'."
    )


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
    online: bool
    online_auto_market_hours: bool

    # Paths
    news_watch_dirs: list[str]
    data_dir: str
    sqlite_path: str
    snapshots_dir: str

    # Models (stage configs; providers inferred from model prefixes)
    triage_model: str
    research_model: str
    xsearch_model: str

    # Budgets/limits
    max_daily_cost: float
    max_cost_per_news_item: float
    max_total_hops: int
    max_web_searches_per_item: int
    max_x_searches_per_item: int
    triage_symbol_cooldown_minutes: int
    triage_max_symbols: int              # max symbols to explore per news item
    triage_web_search: bool              # enable web search during triage LLM call

    # Explorer v1 (Phase 2)
    max_phase1_actions: int
    max_phase2_branches: int

    # Snapshot delayed-price capture
    price_delay_minutes: int

    # Backtest stats resolution (minutes) for equity curve sampling
    stats_resolution_minutes: int

    # SSE
    sse_ping_interval_s: float

    # Dev/testing
    mock_llm: bool
    backfill_on_start: bool
    backfill_limit: int

    # News websocket subprocesses (auto-start with trader app)
    ws_insight_sentry: bool
    ws_alpaca: bool

    # X API stream (optional)
    x_stream_enabled: bool
    x_stream_mode: Literal["burst", "off"]
    x_max_posts_per_day: int
    x_max_bursts_per_day: int
    x_burst_ttl_minutes: int
    x_usage_poll_interval_s: int
    x_min_triage_confidence_for_burst: float
    x_stream_quality_check_enabled: bool
    x_stream_quality_check_after: int
    x_stream_quality_check_model: str
    x_stream_quality_max_retries: int
    x_stream_market_hours_only: bool

    # Evidence acquisition (optional)
    evidence_acquire_enabled: bool
    evidence_max_docs_per_item: int
    evidence_extractor: Literal["trafilatura", "newspaper_fulltext", "readability_lxml"]

    # Watch lifecycle
    watch_enabled: bool
    watch_confidence_threshold: float
    watch_max_concurrent: int
    watch_monitoring_budget: float
    watch_max_hold_minutes: int
    watch_max_retro_minutes: int
    watch_checkin_model: str

    # Follow-up data collection
    follow_up_enabled: bool
    follow_up_schedule: str                     # comma-separated: "+1h,+4h,+1d,+3d,+5d"
    follow_up_web_searches: int                 # web searches per collection
    follow_up_x_searches: int                   # x searches per collection
    follow_up_max_cost: float                   # max USD per follow-up
    follow_up_max_concurrent: int
    follow_up_collector_interval_s: int          # polling interval in seconds
    follow_up_planner_model: str                # model for query planning

    # Pipeline per-agent limits
    pipeline_request_limit: int
    pipeline_tool_calls_limit: int
    # Cost-based budget: skip remaining intermediate agents when cumulative
    # pipeline cost exceeds this threshold (USD). Final agent always runs.
    pipeline_max_cost_usd: float
    # Per-agent timeout (seconds). 0 = no timeout.
    pipeline_agent_timeout_s: float
    # Include the OpenAI middle agent in the pipeline (default: False).
    # When False, pipeline is Grok → Gemini (2 agents).
    pipeline_include_openai: bool
    # OpenAI (gpt-5-mini) web_search cap (0 = unlimited). Prompt-enforced.
    openai_web_search_limit: int
    # Max concurrent exploration workers. 1 = sequential (no parallelism).
    max_parallel_explores: int
    # Hard timeout (seconds) for the entire triage phase (LLM call + polling).
    # If exceeded, triage is treated as a skip.
    triage_timeout_s: float
    # Max concurrent triage LLM calls (semaphore).  Prevents thundering-herd
    # when many news items arrive at once and all workers hit the API together.
    triage_concurrency: int

    # Reasoning / thinking controls
    openai_reasoning_effort: str   # 'low', 'medium', 'high'
    gemini_thinking_level: str     # 'off', 'low', 'medium', 'high', 'dynamic'

    # Pattern review (periodic skip-pattern proposals)
    pattern_review_enabled: bool
    pattern_review_interval_s: int
    pattern_review_model: str
    pattern_review_lookback_hours: int

    # Reflection / Evaluation
    reflection_model: str


def load_settings(*, dotenv_path: str | None = None, override: bool = False) -> Settings:
    """Load settings from environment.

    If dotenv_path is None, loads from default `.env` if present.
    Set override=True to re-read a changed .env file (by default,
    existing env vars are NOT overwritten by load_dotenv).
    """

    load_dotenv(dotenv_path=dotenv_path, override=override)

    learning_mode = _env_bool("LEARNING_MODE", False)
    trading_mode = (os.getenv("TRADING_MODE") or "paper").strip().lower()
    if trading_mode not in ("paper", "live"):
        raise ValueError("TRADING_MODE must be 'paper' or 'live'")

    debug = _env_bool("DEBUG", False)
    online = _env_bool("ONLINE", False)
    online_auto_market_hours = _env_bool("ONLINE_AUTO_MARKET_HOURS", False)

    # NEWS_WATCH_DIRS (comma-separated) takes priority; fall back to legacy ALPACA_OUTPUT_DIR
    _watch_dirs_raw = _env_str("NEWS_WATCH_DIRS", "") or ""
    if _watch_dirs_raw.strip():
        news_watch_dirs = [d.strip() for d in _watch_dirs_raw.split(",") if d.strip()]
    else:
        news_watch_dirs = [_env_str("ALPACA_OUTPUT_DIR", "output/alpaca") or "output/alpaca"]
    data_dir = _env_str("DATA_DIR", "data") or "data"
    sqlite_path = _env_str("SQLITE_PATH", os.path.join(data_dir, "trader.db")) or os.path.join(
        data_dir, "trader.db"
    )
    snapshots_dir = _env_str("SNAPSHOTS_DIR", os.path.join(data_dir, "snapshots")) or os.path.join(
        data_dir, "snapshots"
    )

    triage_model = os.getenv("TRIAGE_MODEL") or "grok-4.1-fast-reasoning"
    research_model = os.getenv("RESEARCH_MODEL") or "gpt-5-mini"
    xsearch_model = os.getenv("XSEARCH_MODEL") or "grok-4.1-fast-reasoning"

    # Validate model prefixes early so misconfiguration fails fast.
    infer_provider_from_model(triage_model)
    infer_provider_from_model(research_model)
    infer_provider_from_model(xsearch_model)

    max_daily_cost = _env_float("MAX_DAILY_COST", 5.00)
    max_cost_per_news_item = _env_float("MAX_COST_PER_NEWS_ITEM", 0.50)
    max_total_hops = _env_int("MAX_TOTAL_HOPS", 3)
    max_web_searches_per_item = _env_int("MAX_WEB_SEARCHES_PER_ITEM", 3)
    max_x_searches_per_item = _env_int("MAX_X_SEARCHES_PER_ITEM", 2)
    triage_symbol_cooldown_minutes = _env_int("TRIAGE_SYMBOL_COOLDOWN_MINUTES", 60)
    triage_max_symbols = _env_int("TRIAGE_MAX_SYMBOLS", 1)
    triage_web_search = _env_bool("TRIAGE_WEB_SEARCH", False)

    # Phase 2 explorer controls
    max_phase1_actions = _env_int("MAX_PHASE1_ACTIONS", 4)
    max_phase2_branches = _env_int("MAX_PHASE2_BRANCHES", 2)

    price_delay_minutes = _env_int("PRICE_DELAY_MINUTES", 10)
    stats_resolution_minutes = _env_int("STATS_RESOLUTION_MINUTES", 60)

    sse_ping_interval_s = _env_float("SSE_PING_INTERVAL_S", 10.0)

    mock_llm = _env_bool("MOCK_LLM", False)
    backfill_on_start = _env_bool("BACKFILL_ON_START", False)
    backfill_limit = _env_int("BACKFILL_LIMIT", 50)

    ws_insight_sentry = _env_bool("WS_INSIGHT_SENTRY", True)
    ws_alpaca = _env_bool("WS_ALPACA", False)

    # X stream (conservative defaults)
    x_stream_enabled = _env_bool("X_STREAM_ENABLED", False)
    x_stream_mode = (os.getenv("X_STREAM_MODE") or "burst").strip().lower()
    if x_stream_mode not in ("burst", "off"):
        raise ValueError("X_STREAM_MODE must be 'burst' or 'off'")
    x_max_posts_per_day = _env_int("X_MAX_POSTS_PER_DAY", 1000)
    x_max_bursts_per_day = _env_int("X_MAX_BURSTS_PER_DAY", 10)
    x_burst_ttl_minutes = _env_int("X_BURST_TTL_MINUTES", 5)
    x_usage_poll_interval_s = _env_int("X_USAGE_POLL_INTERVAL_S", 300)
    x_min_triage_confidence_for_burst = _env_float("X_MIN_TRIAGE_CONFIDENCE_FOR_BURST", 0.75)
    x_stream_quality_check_enabled = _env_bool("X_STREAM_QUALITY_CHECK_ENABLED", True)
    x_stream_quality_check_after = _env_int("X_STREAM_QUALITY_CHECK_AFTER", 3)
    x_stream_quality_check_model = _env_str("X_STREAM_QUALITY_CHECK_MODEL", "gemini-3-flash-preview") or "gemini-3-flash-preview"
    x_stream_quality_max_retries = _env_int("X_STREAM_QUALITY_MAX_RETRIES", 3)
    x_stream_market_hours_only = _env_bool("X_STREAM_MARKET_HOURS_ONLY", True)

    evidence_acquire_enabled = _env_bool("EVIDENCE_ACQUIRE_ENABLED", False)
    evidence_max_docs_per_item = _env_int("EVIDENCE_MAX_DOCS_PER_ITEM", 3)
    evidence_extractor = (os.getenv("EVIDENCE_EXTRACTOR") or "trafilatura").strip().lower()
    if evidence_extractor not in ("trafilatura", "newspaper_fulltext", "readability_lxml"):
        raise ValueError("EVIDENCE_EXTRACTOR must be one of: trafilatura, newspaper_fulltext, readability_lxml")

    # Watch lifecycle
    watch_enabled = _env_bool("WATCH_ENABLED", True)
    watch_confidence_threshold = _env_float("WATCH_CONFIDENCE_THRESHOLD", 0.7)
    watch_max_concurrent = _env_int("MAX_CONCURRENT_WATCHES", 5)
    watch_monitoring_budget = _env_float("WATCH_MONITORING_BUDGET", 0.50)
    watch_max_hold_minutes = _env_int("WATCH_MAX_HOLD_MINUTES", 240)
    watch_max_retro_minutes = _env_int("WATCH_MAX_RETRO_MINUTES", 60)
    watch_checkin_model = _env_str("WATCH_CHECKIN_MODEL", "gemini-3-flash-preview") or "gemini-3-flash-preview"

    # Follow-up data collection
    follow_up_enabled = _env_bool("FOLLOW_UP_ENABLED", True)
    follow_up_schedule = _env_str("FOLLOW_UP_SCHEDULE", "+1h,+4h,+1d,+3d,+5d") or "+1h,+4h,+1d,+3d,+5d"
    follow_up_web_searches = _env_int("FOLLOW_UP_WEB_SEARCHES", 2)
    follow_up_x_searches = _env_int("FOLLOW_UP_X_SEARCHES", 1)
    follow_up_max_cost = _env_float("FOLLOW_UP_MAX_COST", 0.20)
    follow_up_max_concurrent = _env_int("FOLLOW_UP_MAX_CONCURRENT", 20)
    follow_up_collector_interval_s = _env_int("FOLLOW_UP_COLLECTOR_INTERVAL_S", 300)
    follow_up_planner_model = _env_str("FOLLOW_UP_PLANNER_MODEL", "gemini-3-flash-preview") or "gemini-3-flash-preview"

    # Pipeline per-agent limits
    pipeline_request_limit = _env_int("PIPELINE_REQUEST_LIMIT", 15)
    pipeline_tool_calls_limit = _env_int("PIPELINE_TOOL_CALLS_LIMIT", 25)
    pipeline_max_cost_usd = _env_float("PIPELINE_MAX_COST_USD", 0.50)
    pipeline_agent_timeout_s = _env_float("PIPELINE_AGENT_TIMEOUT_S", 120.0)
    pipeline_include_openai = _env_bool("PIPELINE_INCLUDE_OPENAI", False)
    openai_web_search_limit = _env_int("OPENAI_WEB_SEARCH_LIMIT", 10)
    max_parallel_explores = _env_int("MAX_PARALLEL_EXPLORES", 1)
    triage_timeout_s = _env_float("TRIAGE_TIMEOUT_S", 90.0)
    triage_concurrency = _env_int("TRIAGE_CONCURRENCY", 2)

    # Reasoning / thinking controls
    openai_reasoning_effort = _env_str("OPENAI_REASONING_EFFORT", "medium") or "medium"
    gemini_thinking_level = _env_str("GEMINI_THINKING_LEVEL", "dynamic") or "dynamic"

    # Pattern review
    pattern_review_enabled = _env_bool("PATTERN_REVIEW_ENABLED", False)
    pattern_review_interval_s = _env_int("PATTERN_REVIEW_INTERVAL_S", 10800)
    pattern_review_model = _env_str("PATTERN_REVIEW_MODEL", "gpt-5.2") or "gpt-5.2"
    infer_provider_from_model(pattern_review_model)
    pattern_review_lookback_hours = _env_int("PATTERN_REVIEW_LOOKBACK_HOURS", 6)

    reflection_model = _env_str("REFLECTION_MODEL", "gemini-3-flash-preview") or "gemini-3-flash-preview"

    return Settings(
        learning_mode=learning_mode,
        trading_mode=trading_mode,  # type: ignore[arg-type]
        debug=debug,
        online=online,
        online_auto_market_hours=online_auto_market_hours,
        news_watch_dirs=news_watch_dirs,
        data_dir=data_dir,
        sqlite_path=sqlite_path,
        snapshots_dir=snapshots_dir,
        triage_model=triage_model,
        research_model=research_model,
        xsearch_model=xsearch_model,
        max_daily_cost=max_daily_cost,
        max_cost_per_news_item=max_cost_per_news_item,
        max_total_hops=max_total_hops,
        max_web_searches_per_item=max_web_searches_per_item,
        max_x_searches_per_item=max_x_searches_per_item,
        triage_symbol_cooldown_minutes=triage_symbol_cooldown_minutes,
        triage_max_symbols=triage_max_symbols,
        triage_web_search=triage_web_search,
        max_phase1_actions=max_phase1_actions,
        max_phase2_branches=max_phase2_branches,
        price_delay_minutes=price_delay_minutes,
        stats_resolution_minutes=stats_resolution_minutes,
        sse_ping_interval_s=sse_ping_interval_s,
        mock_llm=mock_llm,
        backfill_on_start=backfill_on_start,
        backfill_limit=backfill_limit,
        ws_insight_sentry=ws_insight_sentry,
        ws_alpaca=ws_alpaca,

        x_stream_enabled=x_stream_enabled,
        x_stream_mode=x_stream_mode,  # type: ignore[arg-type]
        x_max_posts_per_day=x_max_posts_per_day,
        x_max_bursts_per_day=x_max_bursts_per_day,
        x_burst_ttl_minutes=x_burst_ttl_minutes,
        x_usage_poll_interval_s=x_usage_poll_interval_s,
        x_min_triage_confidence_for_burst=x_min_triage_confidence_for_burst,
        x_stream_quality_check_enabled=x_stream_quality_check_enabled,
        x_stream_quality_check_after=x_stream_quality_check_after,
        x_stream_quality_check_model=x_stream_quality_check_model,
        x_stream_quality_max_retries=x_stream_quality_max_retries,
        x_stream_market_hours_only=x_stream_market_hours_only,

        evidence_acquire_enabled=evidence_acquire_enabled,
        evidence_max_docs_per_item=evidence_max_docs_per_item,
        evidence_extractor=evidence_extractor,  # type: ignore[arg-type]

        watch_enabled=watch_enabled,
        watch_confidence_threshold=watch_confidence_threshold,
        watch_max_concurrent=watch_max_concurrent,
        watch_monitoring_budget=watch_monitoring_budget,
        watch_max_hold_minutes=watch_max_hold_minutes,
        watch_max_retro_minutes=watch_max_retro_minutes,
        watch_checkin_model=watch_checkin_model,

        follow_up_enabled=follow_up_enabled,
        follow_up_schedule=follow_up_schedule,
        follow_up_web_searches=follow_up_web_searches,
        follow_up_x_searches=follow_up_x_searches,
        follow_up_max_cost=follow_up_max_cost,
        follow_up_max_concurrent=follow_up_max_concurrent,
        follow_up_collector_interval_s=follow_up_collector_interval_s,
        follow_up_planner_model=follow_up_planner_model,

        pipeline_request_limit=pipeline_request_limit,
        pipeline_tool_calls_limit=pipeline_tool_calls_limit,
        pipeline_max_cost_usd=pipeline_max_cost_usd,
        pipeline_agent_timeout_s=pipeline_agent_timeout_s,
        pipeline_include_openai=pipeline_include_openai,
        openai_web_search_limit=openai_web_search_limit,
        max_parallel_explores=max_parallel_explores,
        triage_timeout_s=triage_timeout_s,
        triage_concurrency=triage_concurrency,

        openai_reasoning_effort=openai_reasoning_effort,
        gemini_thinking_level=gemini_thinking_level,

        pattern_review_enabled=pattern_review_enabled,
        pattern_review_interval_s=pattern_review_interval_s,
        pattern_review_model=pattern_review_model,
        pattern_review_lookback_hours=pattern_review_lookback_hours,

        reflection_model=reflection_model,
    )
