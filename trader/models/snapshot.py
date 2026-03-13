"""Snapshot artifact (atomic learning unit).

The online pipeline creates one Snapshot per trigger news event and seals it.
Snapshots are immutable once sealed.

Uses a builder pattern: SnapshotBuilder accumulates data during the pipeline,
then .seal() produces a frozen Snapshot.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class ExplorationBudget:
    max_hops: int = 3
    max_cost_usd: float = 0.35


@dataclass(frozen=True)
class Trigger:
    type: str
    timestamp: str | None
    headline: str
    summary: str | None
    source: str | None
    symbols: list[str]
    raw: dict[str, Any] = field(default_factory=dict)
    source_file: str | None = None  # original news JSON filename


@dataclass(frozen=True)
class CostSummary:
    total_usd: float = 0.0
    by_tool: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """Immutable, sealed snapshot. Created only via SnapshotBuilder.seal()."""

    snapshot_id: str
    version: str
    created_at: str
    decision_at: str
    trigger: Trigger
    market_context: dict[str, Any]
    price_context: dict[str, Any]
    exploration_budget: ExplorationBudget
    triage: dict[str, Any]
    tool_traces: list[dict[str, Any]]
    rounds: list[dict[str, Any]]
    data_modalities: dict[str, list[int]]
    prediction: dict[str, Any]
    cost_summary: CostSummary
    prefetched_market_data: str = ""
    x_stream_burst: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, path: str | Path) -> None:
        from trader.models import atomic_write_text

        atomic_write_text(Path(path), self.to_json(indent=2) + "\n")


# Known US exchanges — symbols with these prefixes are auto-verified.
_US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}


def _strip_exchange_prefix(sym: str) -> tuple[str, str | None]:
    """Strip exchange prefix from symbol. Returns (ticker, exchange_or_None)."""
    if ":" in sym:
        exchange, ticker = sym.split(":", 1)
        return ticker, exchange.upper()
    return sym, None


def normalize_news(news: dict[str, Any]) -> dict[str, Any]:
    """Normalize a news dict to canonical field names (in-place).

    Detects InsightSentry format (has ``title`` key) vs Alpaca format (has
    ``headline`` key) and maps fields so downstream code always sees:
    ``headline``, ``symbols``, ``created_at``, ``url``, ``source``.

    Also populates ``symbol_exchanges`` mapping preserved exchange info.
    """
    if "title" in news and "headline" not in news:
        # InsightSentry format
        news["headline"] = news.pop("title")
        news.setdefault("summary", None)

        # Convert unix timestamp → ISO 8601
        published_at = news.get("published_at")
        if published_at is not None and "created_at" not in news:
            news["created_at"] = datetime.fromtimestamp(
                int(published_at), tz=timezone.utc
            ).isoformat()

        # Strip exchange prefix from symbols, preserve exchange info
        raw_symbols = news.pop("related_symbols", []) or []
        symbols = []
        exchanges: dict[str, str] = {}
        for s in raw_symbols:
            ticker, exchange = _strip_exchange_prefix(str(s))
            symbols.append(ticker)
            if exchange:
                exchanges[ticker] = exchange
        news["symbols"] = symbols
        news["symbol_exchanges"] = exchanges

        # Map link → url
        if "link" in news and "url" not in news:
            news["url"] = news.pop("link")

        news.setdefault("_news_type", "insight_sentry_news")
    else:
        news.setdefault("symbol_exchanges", {})
        news.setdefault("_news_type", "alpaca_news")

    # Compute news age (minutes since timestamp) for both formats.
    # This lets triage and pipeline prompts surface staleness.
    #
    # IMPORTANT caveat: for Alpaca/Benzinga, created_at is when Benzinga
    # published their *rewrite* — the underlying event may be hours older.
    # For InsightSentry, published_at comes from the original source (Reuters,
    # DJ, etc.) so it's closer to the true event time.
    created_at = news.get("created_at")
    if created_at and "news_age_minutes" not in news:
        try:
            pub_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 60
            news["news_age_minutes"] = round(age_min, 1)
        except (ValueError, TypeError):
            pass

    # Flag the timestamp provenance so prompts can qualify the age signal.
    if "news_timestamp_source" not in news:
        if news.get("_news_type") == "insight_sentry_news":
            news["news_timestamp_source"] = "original_publisher"
        else:
            # Alpaca feeds Benzinga rewrites; created_at is Benzinga pub time,
            # which can lag the actual event by minutes to hours.
            news["news_timestamp_source"] = "benzinga_rewrite"

    return news


def deterministic_snapshot_id(news: dict[str, Any], source_file: str | None = None) -> str:
    """Derive a snapshot_id from the news article.

    Uses the Alpaca article ``id`` directly if present. For InsightSentry
    articles, extracts the 8-char hash from the filename. Falls back to a
    random 8-char hex string for manual explores.
    """
    article_id = news.get("id")
    if article_id is not None:
        return str(article_id)

    # InsightSentry: extract hash from filename (e.g. "2026-02-28T13-23-20Z_ef8d9e0a.json")
    if source_file:
        stem = source_file.rsplit(".", 1)[0]  # strip .json
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and len(parts[1]) == 8:
            return parts[1]

    # Fallback: random 8-char hex
    return uuid.uuid4().hex[:8]


def snapshot_id_for_symbol(base_id: str, symbol: str) -> str:
    """Derive a per-symbol snapshot ID from a base news ID.

    When exploring multiple symbols from the same news item, each
    exploration needs a unique snapshot_id.  Appends the symbol
    as a suffix: ``{base_id}_{SYMBOL}``.
    """
    return f"{base_id}_{symbol.upper()}"


class SnapshotBuilder:
    """Mutable accumulator that produces a sealed Snapshot.

    Usage::

        builder = SnapshotBuilder(trigger=trigger, snapshot_id=det_id)
        builder.add_tool_trace(trace_dict)
        builder.set_prediction(...)
        snapshot = builder.seal()
    """

    def __init__(
        self,
        *,
        trigger: Trigger,
        snapshot_id: str | None = None,
        version: str = "v1",
        exploration_budget: ExplorationBudget | None = None,
    ) -> None:
        self.snapshot_id = snapshot_id or str(uuid.uuid4())
        self.version = version
        self.created_at = utc_now().isoformat()
        self.decision_at = ""
        self.trigger = trigger
        self.market_context: dict[str, Any] = {}
        self.price_context: dict[str, Any] = {}
        self.exploration_budget = exploration_budget or ExplorationBudget()
        self.triage: dict[str, Any] = {}
        self.tool_traces: list[dict[str, Any]] = []
        self.rounds: list[dict[str, Any]] = []
        self.prediction: dict[str, Any] = {}
        self.prefetched_market_data: str = ""
        self.x_stream_burst: dict[str, Any] | None = None
        self._cost_by_tool: dict[str, float] = {}
        self._cost_total: float = 0.0

    # ------------------------------------------------------------------
    # Builder methods
    # ------------------------------------------------------------------

    def add_tool_trace(self, trace: dict[str, Any]) -> None:
        self.tool_traces.append(trace)
        # Accumulate cost from the trace execution block
        cost = float((trace.get("execution") or {}).get("cost_usd", 0.0))
        tool_name = (trace.get("action") or {}).get("tool", "unknown")
        self._cost_total += cost
        self._cost_by_tool[tool_name] = self._cost_by_tool.get(tool_name, 0.0) + cost

    def add_round(self, round_dict: dict[str, Any]) -> None:
        self.rounds.append(round_dict)

    def set_market_context(self, ctx: dict[str, Any]) -> None:
        self.market_context = ctx

    def set_price_context(self, ctx: dict[str, Any]) -> None:
        self.price_context = ctx

    def set_triage(self, triage: dict[str, Any]) -> None:
        self.triage = triage

    def set_prediction(self, pred: dict[str, Any]) -> None:
        self.prediction = pred

    def mark_decision_now(self) -> None:
        """Capture the final decision timestamp used for entry semantics."""
        self.decision_at = utc_now().isoformat()

    def set_cost_total(self, total: float) -> None:
        """Override the auto-accumulated total (e.g. from CostTracker)."""
        self._cost_total = total

    def set_cost_by_tool(self, by_tool: dict[str, float]) -> None:
        """Override the per-tool cost breakdown (e.g. from CostTracker)."""
        self._cost_by_tool = dict(by_tool)

    def set_cost_summary(self, *, total: float, by_tool: dict[str, float]) -> None:
        """Override both total and per-tool cost breakdown."""
        self._cost_total = float(total)
        self._cost_by_tool = dict(by_tool)

    # ------------------------------------------------------------------
    # Seal
    # ------------------------------------------------------------------

    def seal(self) -> Snapshot:
        """Produce a frozen Snapshot from accumulated state."""
        # Build data_modalities index from tool trace modality tags
        modalities: dict[str, list[int]] = {}
        for i, trace in enumerate(self.tool_traces):
            modality = trace.get("modality", "other")
            modalities.setdefault(modality, []).append(i)

        return Snapshot(
            snapshot_id=self.snapshot_id,
            version=self.version,
            created_at=self.created_at,
            decision_at=self.decision_at or utc_now().isoformat(),
            trigger=self.trigger,
            triage=self.triage,
            market_context=self.market_context,
            price_context=self.price_context,
            exploration_budget=self.exploration_budget,
            tool_traces=list(self.tool_traces),
            rounds=list(self.rounds),
            data_modalities=modalities,
            prediction=self.prediction,
            cost_summary=CostSummary(
                total_usd=round(self._cost_total, 6),
                by_tool={k: round(v, 6) for k, v in self._cost_by_tool.items()},
            ),
            prefetched_market_data=self.prefetched_market_data,
            x_stream_burst=self.x_stream_burst,
        )
