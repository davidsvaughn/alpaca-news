"""Live exit monitor and portfolio manager.

Replaces the legacy LLM-based WatchMonitor with mechanical exit
strategies (same math as backtest). Runs as a daemon thread.

LiveExitMonitor:
  - Evaluates exit strategies on 1-min Schwab bars for each holding watch
  - Manages cooling_off → sealed transitions

LivePortfolioManager:
  - Evaluates new snapshots against the active LiveConfig
  - Applies filters and allocation strategy
  - Creates watches for qualifying snapshots
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trader.db.database import (
    Database,
    count_holding_watches,
    get_active_live_configs,
    get_all_live_configs,
    get_watch,
    get_live_config,
    get_active_watches,
    insert_watch,
    update_watch,
    update_watch_if_current_status,
)
from trader.market.backtest import (
    normalize_rank_method,
    RANK_METHOD_CONFIDENCE, RANK_METHOD_UNREAL_PL,
    RANK_METHOD_TRAILING_SLOPE, RANK_METHOD_VOLUME_TREND,
    RANK_METHOD_RSI_CURRENT, RANK_METHOD_TECH_SCORE,
    _FEATURE_BASED_METHODS, _score_new_signal, _normalize_scores_0_1,
    compute_ranking_features, compute_ranking_features_from_tick,
)
from trader.market.market_hours import ET, add_market_hours, is_market_open, is_trading_session_open
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder
from trader.valuation import (
    extract_quote_price,
    position_size_for_config,
    summarize_sim_portfolio,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LiveExitMonitor
# ---------------------------------------------------------------------------


class LiveExitMonitor:
    """Evaluates exit strategies on live bar data for holding watches.

    Call run_cycle() periodically (every ~60s) from a daemon thread.
    """

    def __init__(
        self,
        *,
        db: Database,
        bus: Any = None,
        data_dir: str = "data",
        collector: Any = None,  # VolumeDeltaCollector (optional)
        broker_pool: Any = None,  # AlpacaBrokerPool (optional)
        market: Any = None,     # MarketDataService (optional, for sim equity)
    ) -> None:
        self.db = db
        self.bus = bus
        self.data_dir = data_dir
        self.collector = collector
        self.broker_pool = broker_pool  # When set, exits close Alpaca positions
        self.market = market
        # Per-symbol indicator cache (reused across cycles to avoid
        # recomputing indicators on unchanged bar history).
        self._indicator_caches: dict[str, dict[tuple[Any, ...], Any]] = {}

    @staticmethod
    def _quote_price(q: dict[str, Any] | None) -> float | None:
        """Extract a usable price from mixed vendor quote payloads."""
        return extract_quote_price(q)

    def _get_broker_for_watch(self, watch_dict: dict[str, Any]) -> Any:
        """Look up the Alpaca broker for a watch's LiveConfig. Returns None if not linked."""
        if not self.broker_pool:
            return None
        config_dict = self._get_config_for_watch(watch_dict)
        if not config_dict:
            return None
        acct_id = config_dict.get("alpaca_account_id")
        if not acct_id:
            return None
        return self.broker_pool.get(acct_id)

    def _get_config_for_watch(self, watch_dict: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the watch's own config by explicit live_config_id only."""
        watch_id = watch_dict.get("watch_id", "?")
        symbol = watch_dict.get("symbol", "?")
        config_id = watch_dict.get("live_config_id")
        if not config_id:
            log.error(
                "Watch %s (%s) has no live_config_id; refusing fallback config resolution",
                watch_id, symbol,
            )
            return None
        cfg = get_live_config(self.db, str(config_id))
        if cfg:
            return cfg
        log.error(
            "Watch %s (%s) references missing config %s; refusing fallback config resolution",
            watch_id, symbol, config_id,
        )
        return None

    def _persist_watch_builder(self, watch_id: str, builder: WatchBuilder) -> None:
        """Write the current builder state unconditionally."""
        update_watch(self.db, watch_id, builder.to_watch().to_dict())

    def _mark_exit_pending(
        self,
        *,
        watch_id: str,
        builder: WatchBuilder,
        reason: str,
    ) -> None:
        """Persist the intended exit reason before submitting the sell order."""
        builder.mark_exit_pending(reason)
        self._persist_watch_builder(watch_id, builder)

    def _watch_already_exited_for_sell(
        self,
        *,
        watch_id: str,
        sell_order_id: str | None,
        expected_reason: str,
    ) -> bool:
        """Check whether another path already completed this sell for the same watch."""
        current = get_watch(self.db, watch_id)
        if not current:
            return False
        if current.get("status") not in {"exited", "cooling_off", "retrospective", "sealed"}:
            return False
        if sell_order_id and current.get("alpaca_sell_order_id") != sell_order_id:
            return False
        exit_reason = ((current.get("exit") or {}).get("reason") or "")
        return exit_reason in {expected_reason, "sell_fill"}

    def run_cycle(self) -> None:
        """Run one check cycle across all active watches."""
        watches = get_active_watches(self.db)
        if not watches:
            return

        cfg_rows = get_all_live_configs(self.db)
        active_config_ids = {
            str(c.get("config_id"))
            for c in cfg_rows
            if c.get("active")
        }
        known_config_ids = {
            str(c.get("config_id"))
            for c in cfg_rows
            if c.get("config_id")
        }
        # Tracking-mode configs mirror external holdings read-only: their
        # watches must never be evaluated for exit (no auto-sell).
        tracking_config_ids = {
            str(c.get("config_id"))
            for c in cfg_rows
            if str(c.get("mode") or "live") == "tracking"
        }

        for watch_dict in watches:
            try:
                status = watch_dict.get("status")
                cfg_id = str(watch_dict.get("live_config_id") or "")
                if status == "holding" and cfg_id:
                    if cfg_id not in known_config_ids:
                        continue
                    if cfg_id not in active_config_ids:
                        continue
                    if cfg_id in tracking_config_ids:
                        continue
                if status == "holding":
                    self._check_holding(watch_dict)
                elif status == "exited":
                    self._transition_to_cooling_off(watch_dict)
                elif status == "cooling_off":
                    self._check_cooling_off(watch_dict)
                # "retrospective" and "sealed" are ignored
            except Exception:
                log.exception("Error processing watch %s", watch_dict.get("watch_id"))

    # ------------------------------------------------------------------
    # Tick-based VDD (optional, from TimescaleDB)
    # ------------------------------------------------------------------

    def _try_tick_vdd(
        self, symbol: str, exit_params: dict, live_overrides: dict,
    ) -> bool | None:
        """Try tick-based VDD check. Returns True/False or None if unavailable."""
        if not live_overrides.get("vdd_tick"):
            return None
        try:
            from tick_collector.vdd import check_vdd_exit, get_pool

            loop = getattr(self, "_loop", None)
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
                self._loop = loop

            pool = loop.run_until_complete(get_pool())
            if pool is None:
                return None

            # lookback_m from exit_params (shared with bar-based); tick params from live_overrides
            lookback_m = float(exit_params.get("lookback_m")
                               or exit_params.get("lookback", 80))
            bucket_s = int(live_overrides.get("bucket_s", 30))
            min_trades = int(live_overrides.get("min_trades_per_bucket", 3))
            volume_mode = live_overrides.get("volume_mode", "proportional")

            result = loop.run_until_complete(
                check_vdd_exit(pool, symbol, lookback_m, bucket_s, min_trades,
                               volume_mode=volume_mode)
            )
            log.debug("VDD tick check %s: signal=%s", symbol, result)
            return result
        except Exception:
            log.warning("VDD tick check failed for %s, falling back to bar-based",
                        symbol, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Holding: evaluate exit strategy
    # ------------------------------------------------------------------

    def _check_holding(self, watch_dict: dict[str, Any]) -> None:
        """Evaluate exit strategy for a holding watch."""
        from trader.market.backtest import (
            ExitResult,
            _filter_trading_hours,
            _get_ohlcv_1m,
            evaluate_exit,
        )

        symbol = watch_dict["symbol"]
        entry = watch_dict["entry"]
        watch_id = watch_dict["watch_id"]

        # Get exit strategy from watch or active config
        strategy_key = watch_dict.get("exit_strategy")
        exit_params = watch_dict.get("exit_params") or {}
        guard_stop_pct = 0.0
        guard_target_pct = 0.0
        guard_trail_pct = 0.0
        min_hold = 5
        market_close: str | None = "16:00"
        live_overrides: dict[str, Any] = {}

        config_dict = self._get_config_for_watch(watch_dict)
        if not config_dict:
            log.debug(
                "Watch %s (%s): cannot evaluate holding without valid config; skipping",
                watch_id, symbol,
            )
            return
        if not config_dict.get("active", False):
            log.debug(
                "Watch %s (%s): config %s is inactive; skipping holding evaluation",
                watch_id, symbol, config_dict.get("config_id"),
            )
            return

        cfg = LiveConfig.from_dict(config_dict)
        if not strategy_key:
            strategy_key = cfg.exit_strategy
            exit_params = cfg.exit_params
        guard_stop_pct = cfg.guard_stop_pct
        guard_target_pct = cfg.guard_target_pct
        guard_trail_pct = cfg.guard_trail_pct
        min_hold = cfg.min_hold
        market_close = cfg.market_close
        live_overrides = cfg.live_overrides

        if not strategy_key:
            log.warning("Watch %s has no exit strategy configured", watch_id)
            return

        entry_price = entry["price"]
        entry_time_str = entry["time"]

        # Fetch bars: from day before entry to today
        try:
            entry_dt = datetime.fromisoformat(entry_time_str)
        except (ValueError, TypeError):
            log.warning("Watch %s: bad entry time %r", watch_id, entry_time_str)
            return

        start_date = (entry_dt - timedelta(days=2)).strftime("%Y-%m-%d")
        bars = _get_ohlcv_1m(symbol, start_date)
        if bars is None or bars.empty:
            log.debug("Watch %s: no bar data for %s", watch_id, symbol)
            return

        bars = _filter_trading_hours(bars, market_close)
        if bars.empty:
            return

        # Find entry bar index
        # Entry time is UTC ISO; bars index is tz-naive Eastern
        if entry_dt.tzinfo is not None:
            entry_dt_et = entry_dt.astimezone(ET).replace(tzinfo=None)
        else:
            entry_dt_et = entry_dt

        entry_ts = pd.Timestamp(entry_dt_et).floor("s")
        entry_idx = bars.index.searchsorted(entry_ts)
        if entry_idx >= len(bars):
            # Entry is after all available bars — can't evaluate exit yet,
            # but still update peak/trough P&L from latest bar
            current_price = float(bars.iloc[-1]["Close"])
            if entry_price and entry_price > 0:
                current_pnl = ((current_price - entry_price) / entry_price) * 100.0
                builder = WatchBuilder.from_dict(watch_dict)
                if builder.peak_pnl_pct is None or current_pnl > builder.peak_pnl_pct:
                    builder.peak_pnl_pct = round(current_pnl, 4)
                if builder.trough_pnl_pct is None or current_pnl < builder.trough_pnl_pct:
                    builder.trough_pnl_pct = round(current_pnl, 4)
                # Guard against concurrent reconcile/stream exits resurrecting holding state.
                update_watch_if_current_status(
                    self.db,
                    watch_id,
                    builder.to_watch().to_dict(),
                    expected_status="holding",
                )
            return

        # Get or create indicator cache for this symbol;
        # invalidate when bar count changes (indices become stale)
        cache = self._indicator_caches.setdefault(symbol, {})
        if cache.get("__len__") != len(bars):
            cache.clear()
            cache["__len__"] = len(bars)

        result: ExitResult = evaluate_exit(
            strategy_key=strategy_key,
            params=exit_params,
            bars=bars,
            entry_idx=entry_idx,
            entry_price=entry_price,
            guard_stop_pct=guard_stop_pct,
            guard_target_pct=guard_target_pct,
            guard_trail_pct=guard_trail_pct,
            min_hold=min_hold,
            indicator_cache=cache,
        )

        # If bar-based VDD didn't fire, try tick-based VDD as supplement
        if (
            not result.should_exit
            and strategy_key == "volume_delta_divergence"
        ):
            tick_signal = self._try_tick_vdd(symbol, exit_params, live_overrides)
            if tick_signal is True:
                current_price = float(bars.iloc[-1]["Close"])
                bars_held = len(bars) - entry_idx
                result = ExitResult(
                    should_exit=True,
                    exit_price=current_price,
                    reason="signal_tick",
                    bars_held=bars_held,
                )
                log.info("VDD tick signal fired for %s (bar-based did not)", symbol)

        # Update last_checkin_at
        builder = WatchBuilder.from_dict(watch_dict)
        builder.last_checkin_at = datetime.now(tz=timezone.utc).isoformat()

        # Track peak/trough P&L during holding period
        current_price = float(bars.iloc[-1]["Close"])
        if entry_price and entry_price > 0:
            current_pnl = ((current_price - entry_price) / entry_price) * 100.0
            if builder.peak_pnl_pct is None or current_pnl > builder.peak_pnl_pct:
                builder.peak_pnl_pct = round(current_pnl, 4)
            if builder.trough_pnl_pct is None or current_pnl < builder.trough_pnl_pct:
                builder.trough_pnl_pct = round(current_pnl, 4)

        if result.should_exit:
            exit_price = result.exit_price or float(bars.iloc[-1]["Close"])

            # If broker is connected, close Alpaca position and cancel stop order
            broker = self._get_broker_for_watch(watch_dict)
            if broker and watch_dict.get("alpaca_buy_order_id"):
                # Cancel the server-side stop order FIRST (prevent race with stop fill)
                # and wait for it to settle so shares aren't held when the sell submits.
                stop_id = watch_dict.get("alpaca_stop_order_id")
                if stop_id:
                    broker.cancel_order(stop_id)
                    builder.alpaca_stop_order_id = None
                    from trader.market.alpaca_broker import ALPACA_STOP_CANCEL_SETTLE_TIMEOUT
                    broker._wait_for_sell_orders_clear(symbol, timeout_s=ALPACA_STOP_CANCEL_SETTLE_TIMEOUT)

                self._mark_exit_pending(
                    watch_id=watch_id,
                    builder=builder,
                    reason=result.reason,
                )

                # Submit sell order and store its ID on the watch so the
                # stream handler can retroactively update the exit price
                # if the fill arrives after our timeout.
                try:
                    sell_result = broker.close_position(symbol)
                    if sell_result:
                        builder.alpaca_sell_order_id = sell_result.order_id
                        self._persist_watch_builder(watch_id, builder)

                        confirmed = broker.wait_for_fill(
                            sell_result.order_id,
                            timeout_s=broker._resolve_fill_timeout(None),
                        )
                        if confirmed and confirmed.filled_avg_price:
                            exit_price = confirmed.filled_avg_price
                            builder.clear_exit_pending()
                            log.info("ALPACA SELL CONFIRMED: %s price=%.2f qty=%s",
                                     symbol, exit_price, confirmed.filled_qty)
                except TimeoutError:
                    log.warning("Alpaca sell timed out for %s — using bar price %.2f (order left open, stream will update)",
                                symbol, exit_price)
                except Exception:
                    if not builder.alpaca_sell_order_id:
                        builder.clear_exit_pending()
                        self._persist_watch_builder(watch_id, builder)
                    log.exception("Alpaca sell failed for %s — using bar price %.2f", symbol, exit_price)

            builder.record_exit(price=exit_price, reason=result.reason)
            log.info(
                "LIVE EXIT: %s %s — reason=%s, price=%.2f, bars_held=%d",
                symbol, watch_id, result.reason, exit_price, result.bars_held,
            )
            # Clean up indicator cache
            self._indicator_caches.pop(symbol, None)

            if self.bus:
                from trader.online.event_bus import PipelineEvent
                entry_price = entry["price"]
                pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
                self.bus.publish(PipelineEvent(
                    type="watch_exited",
                    payload={
                        "watch_id": watch_id,
                        "symbol": symbol,
                        "exit_price": exit_price,
                        "reason": result.reason,
                        "pnl_pct": round(pnl_pct, 2),
                        "bars_held": result.bars_held,
                    },
                ))

        updated = builder.to_watch()
        ok = update_watch_if_current_status(
            self.db,
            watch_id,
            updated.to_dict(),
            expected_status="holding",
        )
        if not ok:
            if self._watch_already_exited_for_sell(
                watch_id=watch_id,
                sell_order_id=builder.alpaca_sell_order_id,
                expected_reason=result.reason if result.should_exit else "",
            ):
                log.info("Sell for %s (%s) already recorded by concurrent stream/update path", symbol, watch_id)
            else:
                log.info("Skipping stale holding update for %s (%s): status changed concurrently", symbol, watch_id)

    # ------------------------------------------------------------------
    # Exited → cooling_off transition
    # ------------------------------------------------------------------

    def _transition_to_cooling_off(self, watch_dict: dict[str, Any]) -> None:
        """Transition an exited watch to cooling_off."""
        builder = WatchBuilder.from_dict(watch_dict)
        watch_id = watch_dict["watch_id"]

        # Determine cooling_off duration from config
        cooling_hours = 24.0  # default
        config_dict = self._get_config_for_watch(watch_dict)
        if config_dict:
            cooling_hours = config_dict.get("cooling_off_market_hours", 24.0)

        # Compute when cooling_off expires
        now = datetime.now(tz=ET)
        expires = add_market_hours(now, cooling_hours)
        builder.start_cooling_off(expires.isoformat())

        log.info(
            "COOLING OFF: %s %s — until %s (%.1f market hours)",
            watch_dict["symbol"], watch_id, expires.strftime("%Y-%m-%d %H:%M ET"), cooling_hours,
        )

        updated = builder.to_watch()
        ok = update_watch_if_current_status(
            self.db,
            watch_id,
            updated.to_dict(),
            expected_status="exited",
        )
        if not ok:
            log.info("Skipping stale exited->cooling transition for %s (%s)", watch_dict["symbol"], watch_id)

        if self.bus:
            from trader.online.event_bus import PipelineEvent
            self.bus.publish(PipelineEvent(
                type="watch_cooling_off",
                payload={
                    "watch_id": watch_id,
                    "symbol": watch_dict["symbol"],
                    "cooling_off_until": expires.isoformat(),
                },
            ))

    # ------------------------------------------------------------------
    # Cooling off → sealed
    # ------------------------------------------------------------------

    def _check_cooling_off(self, watch_dict: dict[str, Any]) -> None:
        """Check if a cooling_off watch should be sealed."""
        until_str = watch_dict.get("cooling_off_until")
        if not until_str:
            # No expiry set — seal immediately
            self._seal_watch(watch_dict)
            return

        try:
            until_dt = datetime.fromisoformat(until_str)
        except (ValueError, TypeError):
            self._seal_watch(watch_dict)
            return

        now = datetime.now(tz=timezone.utc)
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=timezone.utc)

        if now >= until_dt:
            self._seal_watch(watch_dict)

    def _seal_watch(self, watch_dict: dict[str, Any]) -> None:
        """Seal a watch (final state). Saves shadow data and cleans up streaming."""
        builder = WatchBuilder.from_dict(watch_dict)
        watch_id = watch_dict["watch_id"]
        symbol = watch_dict["symbol"]
        builder.seal()

        log.info("SEALED: %s %s", symbol, watch_id)

        updated = builder.to_watch()
        ok = update_watch_if_current_status(
            self.db,
            watch_id,
            updated.to_dict(),
            expected_status="cooling_off",
        )
        if not ok:
            log.info("Skipping stale cooling->sealed transition for %s (%s)", symbol, watch_id)
            return

        # Clean up indicator cache
        self._indicator_caches.pop(symbol, None)

        # Save shadow collector data and check if symbol can be removed
        if self.collector is not None:
            try:
                self.collector.save_daily(symbol)
                log.info("Shadow data saved for %s", symbol)
            except Exception:
                log.exception("Failed to save shadow data for %s", symbol)

            # Only remove symbol if no other active watches need it
            other_watches = get_active_watches(self.db)
            still_needed = any(
                w.get("symbol") == symbol and w.get("watch_id") != watch_id
                for w in other_watches
            )
            if not still_needed:
                self.collector.remove_symbol(symbol)
                log.info("Removed %s from shadow collector (no active watches)", symbol)

        if self.bus:
            from trader.online.event_bus import PipelineEvent
            self.bus.publish(PipelineEvent(
                type="watch_sealed",
                payload={
                    "watch_id": watch_id,
                    "symbol": symbol,
                },
            ))


# ---------------------------------------------------------------------------
# LivePortfolioManager
# ---------------------------------------------------------------------------


def _cfg_tag(cfg: "LiveConfig") -> str:
    """Short identifier for log lines: config_id + optional account name."""
    acct = f" acct={cfg.alpaca_account_name}" if cfg.alpaca_account_name else ""
    return f"{cfg.config_id}{acct}"


class LivePortfolioManager:
    """Evaluates new snapshots against all active LiveConfigs.

    Supports multiple portfolios: each active config is evaluated
    independently. A single snapshot can create watches in multiple
    portfolios if it passes each config's filters.

    Called from the orchestrator when a snapshot is sealed.
    """

    def __init__(
        self,
        *,
        db: Database,
        bus: Any = None,
        data_dir: str = "data",
        collector: Any = None,  # VolumeDeltaCollector (optional)
        market: Any = None,     # MarketDataService (optional, for streaming)
        broker_pool: Any = None,  # AlpacaBrokerPool (optional)
    ) -> None:
        self.db = db
        self.bus = bus
        self.data_dir = data_dir
        self.collector = collector
        self.market = market
        self.broker_pool = broker_pool  # When set, buys execute Alpaca orders

    def _persist_watch_builder(self, watch_id: str, builder: WatchBuilder) -> None:
        """Write the current builder state unconditionally."""
        update_watch(self.db, watch_id, builder.to_watch().to_dict())

    def _mark_exit_pending(
        self,
        *,
        watch_id: str,
        builder: WatchBuilder,
        reason: str,
    ) -> None:
        """Persist the intended exit reason before submitting the sell order."""
        builder.mark_exit_pending(reason)
        self._persist_watch_builder(watch_id, builder)

    def _watch_already_exited_for_sell(
        self,
        *,
        watch_id: str,
        sell_order_id: str | None,
        expected_reason: str,
    ) -> bool:
        """Check whether another path already completed this sell for the same watch."""
        current = get_watch(self.db, watch_id)
        if not current:
            return False
        if current.get("status") not in {"exited", "cooling_off", "retrospective", "sealed"}:
            return False
        if sell_order_id and current.get("alpaca_sell_order_id") != sell_order_id:
            return False
        exit_reason = ((current.get("exit") or {}).get("reason") or "")
        return exit_reason in {expected_reason, "sell_fill"}

    def evaluate_snapshot(
        self,
        snapshot: dict[str, Any],
        symbol: str,
    ) -> bool:
        """Evaluate a sealed snapshot against ALL active LiveConfigs.

        Returns True if at least one watch was created across any portfolio.
        """
        from trader.db.database import get_active_live_configs

        active_configs = get_active_live_configs(self.db)
        if not active_configs:
            log.debug("LIVE-PM: %s — no active configs", symbol)
            return False
        log.debug("LIVE-PM: %s — checking %d config(s)", symbol, len(active_configs))

        any_created = False
        for config_dict in active_configs:
            try:
                cfg = LiveConfig.from_dict(config_dict)
                if cfg.mode == "tracking":
                    log.debug("LIVE-PM: %s config=%s TRACKING — read-only, skipping", symbol, _cfg_tag(cfg))
                    continue
                if cfg.paused:
                    log.debug("LIVE-PM: %s config=%s PAUSED — skipping new buys", symbol, _cfg_tag(cfg))
                    continue
                result = self._evaluate_for_config(snapshot, symbol, cfg)
                log.info("LIVE-PM: %s config=%s -> %s", symbol, _cfg_tag(cfg), "BUY" if result else "SKIP")
                if result:
                    any_created = True
            except Exception as e:
                cid = config_dict.get('config_id', '?')
                acct = config_dict.get('alpaca_account_name')
                tag = f"{cid} acct={acct}" if acct else cid
                log.error("LIVE-PM: %s config=%s ERROR: %s", symbol, tag, e, exc_info=True)

        return any_created

    def _evaluate_for_config(
        self,
        snapshot: dict[str, Any],
        symbol: str,
        cfg: LiveConfig,
    ) -> bool:
        """Evaluate a snapshot against a single LiveConfig.

        Returns True if a watch was created for this portfolio.
        """
        # Extract prediction from snapshot
        prediction = snapshot.get("prediction") or {}
        confidence = prediction.get("confidence", 0.0)
        direction = (prediction.get("direction") or "neutral").lower()
        log.debug("LIVE-EVAL: %s dir=%s conf=%s config=%s", symbol, direction, confidence, _cfg_tag(cfg))

        # --- Apply filters ---

        if direction == "neutral":
            log.debug("LIVE-EVAL: %s SKIP neutral", symbol)
            return False

        # Apply filters — uses the same keys as the backtest UI:
        # conf_min, symbol, created_after, price_min, price_max,
        # avg_vol_min, avg_vol_max, mkt_cap_min, mkt_cap_max, pe_min, pe_max
        filters = cfg.filters or {}

        # Confidence filter (key: conf_min, value: signed percentage string)
        conf_min_str = filters.get("conf_min") or filters.get("confidence_min")
        if conf_min_str:
            try:
                conf_min_val = float(conf_min_str) / 100.0
                signed_conf = confidence if direction == "bullish" else -confidence
                if signed_conf < conf_min_val:
                    log.debug("LIVE-EVAL: %s SKIP conf %.2f < %.2f", symbol, signed_conf, conf_min_val)
                    return False
            except (ValueError, TypeError):
                pass
        log.debug("LIVE-EVAL: %s passed confidence filter", symbol)

        # Symbol filter
        sym_filter = filters.get("symbol")
        if sym_filter and sym_filter.strip():
            if symbol.upper() != sym_filter.strip().upper():
                log.debug("LIVE-EVAL: %s SKIP symbol filter", symbol)
                return False

        # Market metric filters (price, volume, market cap, PE)
        if not self._apply_market_filters(symbol, filters):
            log.debug("LIVE-EVAL: %s SKIP market metric filter", symbol)
            return False
        log.debug("LIVE-EVAL: %s passed market filters", symbol)

        # --- Check allocation (per-portfolio) ---
        holding_count = count_holding_watches(self.db, live_config_id=cfg.config_id)

        alloc = cfg.allocation
        alloc_params = cfg.allocation_params or {}

        if alloc == "none":
            max_concurrent = 999
        elif alloc == "fixed_dollar":
            alloc_pct = float(alloc_params.get("alloc_pct", 5))
            max_concurrent = int(100 / alloc_pct) if alloc_pct > 0 else 20
        elif alloc == "max_positions":
            max_concurrent = int(alloc_params.get("max_pos", 10))
        elif alloc == "ranking_realloc":
            alloc_pct = float(alloc_params.get("alloc_pct", 5))
            max_concurrent = int(100 / alloc_pct) if alloc_pct > 0 else 20
        else:
            max_concurrent = 20

        log.debug("LIVE-EVAL: %s allocation %d/%d", symbol, holding_count, max_concurrent)

        # Determine replacement policy
        do_replace = False
        if alloc == "max_positions":
            do_replace = str(alloc_params.get("when_full", "skip")) == "replace"
        elif alloc == "ranking_realloc":
            do_replace = True  # ranking_realloc always replaces

        victim_watch: dict[str, Any] | None = None
        if holding_count >= max_concurrent:
            if not do_replace:
                log.debug("LIVE-EVAL: %s SKIP at capacity (when_full=skip)", symbol)
                return False
            # Try to replace the weakest position
            victim_watch = self._find_replacement_victim(
                symbol, confidence, direction, cfg,
            )
            if victim_watch is None:
                log.debug("LIVE-EVAL: %s SKIP at capacity — new signal not stronger than weakest", symbol)
                return False
            log.debug("LIVE-EVAL: %s will REPLACE %s (watch=%s)",
                       symbol, victim_watch['symbol'], victim_watch['watch_id'])

        # --- Determine entry price ---
        entry_price = self._extract_entry_price(snapshot, symbol)
        log.debug("LIVE-EVAL: %s entry_price=%s", symbol, entry_price)
        if entry_price is None or entry_price <= 0:
            log.debug("LIVE-EVAL: %s SKIP no entry price", symbol)
            return False

        # --- Duplicate symbol guard (per-portfolio) ---
        existing = get_active_watches(self.db)
        for w in existing:
            if (w.get("status") == "holding"
                    and w.get("symbol") == symbol
                    and w.get("live_config_id") == cfg.config_id):
                log.debug("LIVE-EVAL: %s SKIP already holding in config=%s", symbol, _cfg_tag(cfg))
                return False

        # --- Exit victim position (replacement) ---
        if victim_watch is not None:
            if not self._exit_victim(victim_watch, cfg):
                log.debug("LIVE-EVAL: %s SKIP — failed to exit victim %s", symbol, victim_watch['symbol'])
                return False

        # --- Create watch ---
        snapshot_id = snapshot.get("snapshot_id", "")
        wb = WatchBuilder.create_from_live_config(
            snapshot_id=snapshot_id,
            symbol=symbol,
            entry_price=entry_price,
            confidence=confidence,
            direction=direction,
            live_config_id=cfg.config_id,
            exit_strategy=cfg.exit_strategy,
            exit_params=cfg.exit_params,
        )

        # --- Execute Alpaca orders (if broker connected to this config) ---
        broker = self.broker_pool.get(cfg.alpaca_account_id) if self.broker_pool and cfg.alpaca_account_id else None
        if broker:
            # SAFETY: Check Alpaca for existing position BEFORE buying.
            # Prevents double-buys if watch DB is out of sync.
            existing_pos = broker.get_position(symbol)
            if existing_pos:
                log.warning("LIVE-EVAL: %s SKIP — Alpaca already holds position "
                            "(qty=%.4f, entry=$%.2f)",
                            symbol, existing_pos.qty, existing_pos.avg_entry_price)
                return False

            position_size = position_size_for_config(cfg) or 0.0

            # Guard: don't buy if we can't afford at least 1 whole share
            if entry_price > 0 and position_size / entry_price < 1.0:
                log.debug("LIVE-EVAL: %s SKIP — position size $%.0f < 1 share at $%.2f",
                          symbol, position_size, entry_price)
                return False

            # Submit market buy and WAIT for fill confirmation
            try:
                buy_confirmed = broker.buy_and_confirm(symbol, notional=position_size)
            except Exception as exc:
                log.exception("ALPACA BUY FAILED for %s — skipping (no watch created)", symbol)
                from trader.notifications import notify
                if victim_watch is not None:
                    # Victim was already sold but replacement buy failed — alert operator
                    victim_sym = victim_watch.get("symbol", "?")
                    notify(
                        subject=f"Buy failed after replacement sell: {symbol}",
                        body=(
                            f"Replacement buy FAILED for {symbol} after selling victim {victim_sym}.\n"
                            f"Account: {cfg.alpaca_account_id or 'N/A'}\n"
                            f"Config: {_cfg_tag(cfg)}\n"
                            f"Position size: ${position_size:.2f}\n"
                            f"Error: {exc}\n\n"
                            f"The victim position ({victim_sym}) has been exited but the "
                            f"replacement was not opened. Portfolio is temporarily under-allocated "
                            f"by one slot."
                        ),
                    )
                else:
                    notify(
                        subject=f"Buy failed: {symbol}",
                        body=(
                            f"Buy FAILED for {symbol}.\n"
                            f"Account: {cfg.alpaca_account_id or 'N/A'}\n"
                            f"Config: {_cfg_tag(cfg)}\n"
                            f"Position size: ${position_size:.2f}\n"
                            f"Error: {exc}"
                        ),
                    )
                return False

            wb.alpaca_buy_order_id = buy_confirmed.order_id

            # Use ACTUAL fill price and qty (not estimates)
            if buy_confirmed.filled_avg_price and buy_confirmed.filled_avg_price > 0:
                entry_price = buy_confirmed.filled_avg_price
                from trader.models.watch import WatchEntry
                wb.entry = WatchEntry(
                    snapshot_id=wb.entry.snapshot_id,
                    price=entry_price,
                    time=wb.entry.time,
                    confidence=wb.entry.confidence,
                    direction=wb.entry.direction,
                    horizon=wb.entry.horizon,
                    thesis=wb.entry.thesis,
                )
            actual_qty = buy_confirmed.filled_qty or 0
            wb.qty = actual_qty

            log.info("ALPACA BUY CONFIRMED: %s order=%s qty=%.4f fill_price=%.2f notional=%.2f",
                     symbol, buy_confirmed.order_id, actual_qty, entry_price, position_size)

            # Submit server-side stop-loss with ACTUAL qty from confirmed fill
            if cfg.guard_stop_pct > 0 and actual_qty > 0:
                stop_price = round(entry_price * (1 - cfg.guard_stop_pct / 100), 2)
                wb.alpaca_stop_price = stop_price  # persist for re-submission
                try:
                    stop_confirmed = broker.set_stop(symbol, qty=actual_qty, stop_price=stop_price)
                    wb.alpaca_stop_order_id = stop_confirmed.order_id
                    log.info("ALPACA STOP SET: %s order=%s qty=%.4f stop=%.2f",
                             symbol, stop_confirmed.order_id, actual_qty, stop_price)
                except Exception:
                    log.exception("ALPACA STOP FAILED for %s — position open without stop protection!", symbol)
        else:
            # Local/shadow portfolio — fetch fresh price at decision time
            # so simulated entry_price reflects the actual market price now,
            # not the stale snapshot price from investigation time.
            if self.market:
                try:
                    quotes = self.market.get_quotes([symbol])
                    q = (quotes or {}).get(symbol.upper()) or (quotes or {}).get(symbol) or {}
                    fresh = extract_quote_price(q)
                    if fresh is not None:
                        entry_price = fresh
                        from trader.models.watch import WatchEntry
                        wb.entry = WatchEntry(
                            snapshot_id=wb.entry.snapshot_id,
                            price=entry_price,
                            time=wb.entry.time,
                            confidence=wb.entry.confidence,
                            direction=wb.entry.direction,
                            horizon=wb.entry.horizon,
                            thesis=wb.entry.thesis,
                        )
                        log.debug("LIVE-EVAL: %s fresh entry_price=%.2f", symbol, entry_price)
                except Exception:
                    log.warning("Could not fetch fresh price for %s — using snapshot price", symbol)

            position_size = position_size_for_config(cfg) or 0.0
            if entry_price and entry_price > 0:
                wb.qty = position_size / entry_price

        watch = wb.to_watch()

        # Persist
        watch_path = Path(self.data_dir) / "watches" / f"{watch.watch_id}.json"
        insert_watch(self.db, watch=watch.to_dict())
        watch.persist(watch_path)

        log.info(
            "LIVE BUY: %s %s — conf=%.2f, price=%.2f, strategy=%s, alpaca=%s",
            symbol, watch.watch_id, confidence, entry_price, cfg.exit_strategy,
            "yes" if wb.alpaca_buy_order_id else "no",
        )

        # Shadow collector + Schwab stream disabled — tick_collector handles all streaming

        if self.bus:
            from trader.online.event_bus import PipelineEvent
            self.bus.publish(PipelineEvent(
                type="watch_created",
                payload={
                    "watch_id": watch.watch_id,
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": confidence,
                    "entry_price": entry_price,
                    "snapshot_id": snapshot_id,
                    "live_config_id": cfg.config_id,
                },
            ))

        return True

    # ------------------------------------------------------------------
    # Replace-weakest helpers
    # ------------------------------------------------------------------

    def _fetch_ranking_features_for_symbol(self, symbol: str) -> dict[str, float] | None:
        """Fetch ranking features for a single symbol (live).

        Priority: tick_collector (higher quality) → Schwab 1-min bars.
        """
        # Try tick_collector first
        try:
            from tick_collector.vdd import get_vdd_bars, get_pool

            loop = getattr(self, "_loop", None)
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
                self._loop = loop

            pool = loop.run_until_complete(get_pool())
            if pool is not None:
                # 5-min buckets, 2.5h lookback for 30 bars
                tick_bars = loop.run_until_complete(
                    get_vdd_bars(pool, symbol, lookback_m=150, bucket_s=300)
                )
                if tick_bars is not None and len(tick_bars) >= 2:
                    return compute_ranking_features_from_tick(tick_bars)
        except Exception:
            pass  # fall through to bar-based

        # Fall back to Schwab 1-min bars
        if self.market:
            try:
                from trader.market.backtest import _get_ohlcv_1m
                from datetime import date, timedelta
                start = (date.today() - timedelta(days=2)).isoformat()
                df = _get_ohlcv_1m(symbol, start)
                if df is not None and len(df) >= 10:
                    # Resample to 5-min
                    ohlcv_5m = df.resample("5min").agg({
                        "Open": "first", "High": "max", "Low": "min",
                        "Close": "last", "Volume": "sum",
                    }).dropna(subset=["Close"])
                    if len(ohlcv_5m) >= 2:
                        return compute_ranking_features(ohlcv_5m)
            except Exception:
                log.warning("Could not fetch bars for %s ranking", symbol)

        return None

    def _score_holding_watches(
        self,
        holdings: list[dict[str, Any]],
        rank_method: str,
        broker: Any | None,
    ) -> dict[str, float]:
        """Score each holding watch. Returns {watch_id: score}.

        Supports all ranking methods including forward-looking ones that
        fetch bar data for feature computation.
        """
        scores: dict[str, float] = {}
        confidences: dict[str, float] = {}
        pnl_scores: dict[str, float] = {}

        # If broker connected, batch-fetch all positions for current prices
        alpaca_prices: dict[str, float] = {}
        if broker:
            try:
                positions = broker.get_positions()
                alpaca_prices = {p.symbol.upper(): p.current_price for p in positions}
            except Exception:
                log.warning("Could not fetch Alpaca positions for ranking")

        # If no broker, fetch fresh quotes for unreal_pl scoring
        if not alpaca_prices and self.market and rank_method == RANK_METHOD_UNREAL_PL:
            syms = [w.get("symbol", "").upper() for w in holdings if w.get("symbol")]
            if syms:
                try:
                    quotes = self.market.get_quotes(syms)
                    for sym in syms:
                        q = (quotes or {}).get(sym) or {}
                        price = extract_quote_price(q)
                        if price is not None:
                            alpaca_prices[sym] = price
                except Exception:
                    log.warning("Could not fetch quotes for non-Alpaca ranking")

        for w in holdings:
            wid = w["watch_id"]
            entry = w.get("entry") or {}
            entry_price = float(entry.get("price", 0))
            conf = float(entry.get("confidence", 0.5))
            sym = w.get("symbol", "").upper()
            confidences[wid] = conf

            # Compute unrealized P&L
            current_price = alpaca_prices.get(sym)
            if current_price and entry_price > 0:
                pnl_scores[wid] = (current_price - entry_price) / entry_price
            else:
                pnl_scores[wid] = 0.0

        # --- Forward-looking methods: fetch bar data and compute features ---
        if rank_method in _FEATURE_BASED_METHODS:
            feature_scores: dict[str, dict[str, float]] = {}
            for w in holdings:
                wid = w["watch_id"]
                sym = w.get("symbol", "").upper()
                feat = self._fetch_ranking_features_for_symbol(sym)
                feature_scores[wid] = feat or {"slope": 0.0, "rsi": 50.0, "ad_slope": 0.0}

            if rank_method == RANK_METHOD_TRAILING_SLOPE:
                scores = {wid: f["slope"] for wid, f in feature_scores.items()}
            elif rank_method == RANK_METHOD_VOLUME_TREND:
                scores = {wid: f["ad_slope"] for wid, f in feature_scores.items()}
            elif rank_method == RANK_METHOD_RSI_CURRENT:
                scores = {wid: 100.0 - f["rsi"] for wid, f in feature_scores.items()}
            elif rank_method == RANK_METHOD_TECH_SCORE:
                wids = list(feature_scores.keys())
                if len(wids) < 2:
                    scores = {wid: f["slope"] for wid, f in feature_scores.items()}
                else:
                    import math

                    def _z(vals: list[float]) -> list[float]:
                        m = sum(vals) / len(vals)
                        v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
                        s = math.sqrt(v) if v > 0 else 1.0
                        return [(x - m) / s for x in vals]

                    slopes = [feature_scores[w]["slope"] for w in wids]
                    ads = [feature_scores[w]["ad_slope"] for w in wids]
                    rsis = [100.0 - feature_scores[w]["rsi"] for w in wids]
                    z_s, z_a, z_r = _z(slopes), _z(ads), _z(rsis)
                    scores = {
                        wid: 0.4 * zs + 0.3 * za + 0.3 * zr
                        for wid, zs, za, zr in zip(wids, z_s, z_a, z_r)
                    }
            return scores

        # --- Legacy methods ---
        if rank_method == RANK_METHOD_CONFIDENCE:
            scores = confidences
        elif rank_method == RANK_METHOD_UNREAL_PL:
            scores = pnl_scores
        else:
            scores = pnl_scores

        return scores

    def _score_new_in_tech_score(
        self,
        holdings: list[dict[str, Any]],
        new_features: dict[str, float],
        broker: Any | None,
        weights: tuple[float, float, float] = (0.4, 0.3, 0.3),
    ) -> float:
        """Score new signal in tech_score by including it in the z-score set.

        Fetches features for all holdings, adds the new signal's features,
        and z-scores everything together — giving the new signal a proper
        composite score instead of a raw slope proxy.
        """
        import math

        # Collect features for all holdings + new signal
        all_features: dict[str, dict[str, float]] = {}
        for w in holdings:
            wid = w["watch_id"]
            sym = w.get("symbol", "").upper()
            feat = self._fetch_ranking_features_for_symbol(sym)
            all_features[wid] = feat or {"slope": 0.0, "rsi": 50.0, "ad_slope": 0.0}
        new_key = "__new_signal__"
        all_features[new_key] = new_features

        sids = list(all_features.keys())
        if len(sids) < 3:
            return new_features.get("slope", 0.0)

        def _z(vals: list[float]) -> list[float]:
            m = sum(vals) / len(vals)
            v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
            s = math.sqrt(v) if v > 0 else 1.0
            return [(x - m) / s for x in vals]

        w1, w2, w3 = weights
        z_slope = _z([all_features[s]["slope"] for s in sids])
        z_ad = _z([all_features[s]["ad_slope"] for s in sids])
        z_rsi = _z([100.0 - all_features[s]["rsi"] for s in sids])

        all_scores = {
            sid: w1 * zs + w2 * za + w3 * zr
            for sid, zs, za, zr in zip(sids, z_slope, z_ad, z_rsi)
        }
        return all_scores[new_key]

    def _find_replacement_victim(
        self,
        new_symbol: str,
        new_confidence: float,
        new_direction: str,
        cfg: LiveConfig,
    ) -> dict[str, Any] | None:
        """Find the weakest holding in this config's portfolio.

        Returns the watch dict of the victim if the new signal is stronger,
        or None if no replacement should happen.
        """
        alloc_params = cfg.allocation_params or {}
        rank_method = normalize_rank_method(
            str(alloc_params.get("rank_method", RANK_METHOD_UNREAL_PL))
        )
        replace_min_margin = float(alloc_params.get("replace_min_margin", 0.0))

        # Get all holding watches for this config
        all_watches = get_active_watches(self.db)
        holdings = [
            w for w in all_watches
            if w.get("status") == "holding"
            and w.get("live_config_id") == cfg.config_id
        ]
        if not holdings:
            return None

        # Get broker for current prices
        broker = (self.broker_pool.get(cfg.alpaca_account_id)
                  if self.broker_pool and cfg.alpaca_account_id else None)

        raw_scores = self._score_holding_watches(holdings, rank_method, broker)
        if not raw_scores:
            return None

        # Score the incoming signal (symmetric for forward-looking methods)
        new_features: dict[str, float] | None = None
        if rank_method in _FEATURE_BASED_METHODS:
            new_features = self._fetch_ranking_features_for_symbol(new_symbol)

        # For tech_score: include new signal in z-score computation
        if (rank_method == RANK_METHOD_TECH_SCORE
                and new_features and len(holdings) >= 2):
            raw_new = self._score_new_in_tech_score(holdings, new_features, broker)
        else:
            raw_new = _score_new_signal(rank_method, new_confidence, new_features)

        # Normalize all scores to 0-1 so replace_min_margin is uniform
        scores, new_score = _normalize_scores_0_1(raw_scores, raw_new)

        # Find weakest
        worst_wid = min(scores, key=scores.get)  # type: ignore[arg-type]
        worst_score = scores[worst_wid]

        if new_score <= worst_score + replace_min_margin:
            log.debug("LIVE-EVAL: %s new_score=%.4f <= worst_score=%.4f+margin=%s "
                      "(%s) — no replacement [normalized 0-1]",
                      new_symbol, new_score, worst_score, replace_min_margin, worst_wid)
            return None

        # Find the watch dict for the victim
        for w in holdings:
            if w["watch_id"] == worst_wid:
                log.debug("LIVE-EVAL: %s new_score=%.4f > worst=%s score=%.4f "
                          "(margin=%s) — replacing [normalized 0-1]",
                          new_symbol, new_score, w['symbol'], worst_score, replace_min_margin)
                return w
        return None

    def _exit_victim(
        self,
        victim: dict[str, Any],
        cfg: LiveConfig,
    ) -> bool:
        """Exit (sell) the victim watch to make room for a replacement.

        Returns True on success, False if the exit failed.
        """
        symbol = victim["symbol"]
        watch_id = victim["watch_id"]
        entry = victim.get("entry") or {}
        entry_price = float(entry.get("price", 0))

        builder = WatchBuilder.from_dict(victim)

        # Get current price for exit (fallback to entry price)
        exit_price = entry_price

        broker = (self.broker_pool.get(cfg.alpaca_account_id)
                  if self.broker_pool and cfg.alpaca_account_id else None)

        if broker and victim.get("alpaca_buy_order_id"):
            # Cancel stop order first and wait for it to settle
            # so shares aren't held when the sell submits.
            stop_id = victim.get("alpaca_stop_order_id")
            if stop_id:
                broker.cancel_order(stop_id)
                builder.alpaca_stop_order_id = None
                from trader.market.alpaca_broker import ALPACA_STOP_CANCEL_SETTLE_TIMEOUT
                broker._wait_for_sell_orders_clear(symbol, timeout_s=ALPACA_STOP_CANCEL_SETTLE_TIMEOUT)

            self._mark_exit_pending(
                watch_id=watch_id,
                builder=builder,
                reason="replaced",
            )

            # Submit sell and store its ID so the stream handler can
            # retroactively update the exit price on late fills.
            try:
                sell_result = broker.close_position(symbol)
                if sell_result:
                    builder.alpaca_sell_order_id = sell_result.order_id
                    self._persist_watch_builder(watch_id, builder)

                    confirmed = broker.wait_for_fill(
                        sell_result.order_id,
                        timeout_s=broker._resolve_fill_timeout(None),
                    )
                    if confirmed and confirmed.filled_avg_price:
                        exit_price = confirmed.filled_avg_price
                        builder.clear_exit_pending()
                        log.info("REPLACE SELL CONFIRMED: %s price=%.2f qty=%s",
                                 symbol, exit_price, confirmed.filled_qty)
            except TimeoutError:
                log.warning("REPLACE SELL timed out for %s — aborting replacement (order left open, stream will update)",
                            symbol)
                return False
            except Exception:
                if not builder.alpaca_sell_order_id:
                    builder.clear_exit_pending()
                    self._persist_watch_builder(watch_id, builder)
                log.exception("REPLACE SELL FAILED for %s — aborting replacement", symbol)
                return False
        else:
            # Shadow portfolio: use Alpaca position price if available, else fresh quote
            if broker:
                pos = broker.get_position(symbol)
                if pos:
                    exit_price = pos.current_price
            elif self.market:
                try:
                    quotes = self.market.get_quotes([symbol])
                    q = (quotes or {}).get(symbol.upper()) or (quotes or {}).get(symbol) or {}
                    fresh = extract_quote_price(q)
                    if fresh is not None:
                        exit_price = fresh
                except Exception:
                    log.warning("Could not fetch exit price for %s — using entry price", symbol)

        builder.record_exit(price=exit_price, reason="replaced")
        log.info("REPLACE EXIT: %s %s — exit_price=%.2f", symbol, watch_id, exit_price)

        updated = builder.to_watch()
        ok = update_watch_if_current_status(
            self.db,
            watch_id,
            updated.to_dict(),
            expected_status="holding",
        )
        if not ok:
            if self._watch_already_exited_for_sell(
                watch_id=watch_id,
                sell_order_id=builder.alpaca_sell_order_id,
                expected_reason="replaced",
            ):
                log.info("Replacement exit for %s (%s) already recorded by concurrent stream/update path", symbol, watch_id)
                return True
            log.info("Skipping stale replacement exit for %s (%s): status changed concurrently", symbol, watch_id)
            return False

        if self.bus:
            from trader.online.event_bus import PipelineEvent
            pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
            self.bus.publish(PipelineEvent(
                type="watch_exited",
                payload={
                    "watch_id": watch_id,
                    "symbol": symbol,
                    "exit_price": exit_price,
                    "reason": "replaced",
                    "pnl_pct": round(pnl_pct, 2),
                    "bars_held": 0,
                },
            ))

        return True

    def _extract_entry_price(
        self,
        snapshot: dict[str, Any],
        symbol: str,
    ) -> float | None:
        """Extract entry price from snapshot data.

        In live mode, entry semantics are decision-time based. Use the latest
        price captured at seal time, then fall back to model metadata if needed.
        """
        # Try price_context first (current price — always available at seal time)
        # Structure: {per_symbol: {SYMBOL: {last_price: ...}}} or flat {lastPrice: ...}
        price_ctx = snapshot.get("price_context") or {}

        # Check nested per_symbol structure first (Schwab format)
        per_sym = price_ctx.get("per_symbol") or {}
        sym_data = per_sym.get(symbol.upper()) or per_sym.get(symbol) or {}
        for key in ("last_price", "lastPrice", "mark", "regularMarketPrice"):
            if key in sym_data:
                val = sym_data[key]
                if val and float(val) > 0:
                    return float(val)

        # Check flat structure (legacy/fallback)
        for key in ("lastPrice", "last_price", "regularMarketPrice"):
            if key in price_ctx:
                val = price_ctx[key]
                if val and float(val) > 0:
                    return float(val)

        # Try prediction entry_price
        pred = snapshot.get("prediction") or {}
        ep = pred.get("entry_price")
        if ep and float(ep) > 0:
            return float(ep)

        return None

    def _apply_market_filters(
        self, symbol: str, filters: dict[str, Any],
    ) -> bool:
        """Check market metric filters (price, volume, market cap, PE).

        Returns False if the symbol should be skipped due to a filter.
        Permissive on data fetch failure (logs warning, allows through).
        """
        # Gather which filters are actually set
        checks: list[tuple[str, str, str, float]] = []  # (key_min, key_max, metric_name, scale)
        for key_min, key_max, name, scale in [
            ("price_min", "price_max", "price", 1.0),
            ("avg_vol_min", "avg_vol_max", "avg_vol", 1e6),   # UI values in millions
            ("mkt_cap_min", "mkt_cap_max", "mkt_cap", 1e9),   # UI values in billions
            ("pe_min", "pe_max", "pe", 1.0),
        ]:
            lo = filters.get(key_min)
            hi = filters.get(key_max)
            if lo or hi:
                checks.append((key_min, key_max, name, scale))

        if not checks:
            return True  # no market filters set

        # Fetch market data
        try:
            data = self.market.get_fundamentals(symbol)
            quote = self.market.get_quotes([symbol]) if hasattr(self.market, "get_quotes") else {}
        except Exception:
            log.warning("Could not fetch market data for %s filter check — allowing through", symbol)
            return True

        price = None
        if isinstance(quote, dict):
            q = quote.get(symbol.upper()) or quote.get(symbol) or {}
            price = extract_quote_price(q)
        if price is None:
            price = (
                data.get("lastPrice")
                or data.get("last_price")
                or data.get("regularMarketPrice")
                or data.get("mark")
                or data.get("close")
            )

        metrics = {
            "price": float(price) if price else None,
            "avg_vol": data.get("avg10DaysVolume") or data.get("averageVolume"),
            "mkt_cap": data.get("marketCap"),
            "pe": data.get("peRatio") or data.get("trailingPE") or data.get("forwardPE"),
        }

        for key_min, key_max, name, scale in checks:
            val = metrics.get(name)
            if val is None:
                continue  # permissive: skip filter if data unavailable
            val = float(val)
            lo = filters.get(key_min)
            hi = filters.get(key_max)
            if lo:
                if val < float(lo) * scale:
                    log.debug("SKIP %s: %s=%.2f < min %.2f", symbol, name, val, float(lo) * scale)
                    return False
            if hi:
                if val > float(hi) * scale:
                    log.debug("SKIP %s: %s=%.2f > max %.2f", symbol, name, val, float(hi) * scale)
                    return False

        return True


# ---------------------------------------------------------------------------
# Monitoring loop (daemon thread entry point)
# ---------------------------------------------------------------------------


_SHADOW_SUMMARY_INTERVAL_S = 3600  # write clean summary JSON hourly
_EQUITY_SNAPSHOT_INTERVAL_S = float(os.getenv("EQUITY_SNAPSHOT_INTERVAL", "60"))  # 1 min default


def _ensure_stops_if_needed(monitor: LiveExitMonitor, last_date: str | None) -> str | None:
    """Ensure all holding positions have active stop orders, once per trading day.

    Returns the date string of the last check (to avoid repeating).
    """
    if not monitor.broker_pool:
        return last_date

    now = datetime.now(tz=ET)
    today = now.strftime("%Y-%m-%d")
    if today == last_date:
        return last_date  # already done today

    if not is_trading_session_open():
        return last_date

    try:
        from trader.market.alpaca_reconcile import ensure_stops
        from trader.db.database import get_active_live_configs
        from trader.models.live_config import LiveConfig

        active_cfgs = get_active_live_configs(monitor.db)
        for cfg_dict in active_cfgs:
            acct_id = cfg_dict.get("alpaca_account_id")
            if not acct_id:
                continue
            broker = monitor.broker_pool.get(acct_id)
            if not broker:
                continue
            cfg = LiveConfig.from_dict(cfg_dict)
            ensure_stops(broker=broker, db=monitor.db,
                         live_config_id=cfg.config_id,
                         guard_stop_pct=cfg.guard_stop_pct)
        return today
    except Exception:
        log.exception("Daily ensure-stops check failed")
        return last_date


_RECONCILE_INTERVAL_S = float(os.getenv("RECONCILE_INTERVAL", "900"))  # 15 min default


def _periodic_reconcile(monitor: LiveExitMonitor, last_time: float) -> float:
    """Run reconciliation periodically (every RECONCILE_INTERVAL seconds).

    Returns the monotonic timestamp of the last reconcile.
    """
    now = time.monotonic()
    if now - last_time < _RECONCILE_INTERVAL_S:
        return last_time

    if not monitor.broker_pool or not is_trading_session_open():
        return last_time

    try:
        from trader.market.alpaca_reconcile import reconcile, ensure_stops
        from trader.db.database import get_active_live_configs
        from trader.models.live_config import LiveConfig

        active_cfgs = get_active_live_configs(monitor.db)
        for cfg_dict in active_cfgs:
            acct_id = cfg_dict.get("alpaca_account_id")
            if not acct_id:
                continue
            broker = monitor.broker_pool.get(acct_id)
            if not broker:
                continue
            cfg = LiveConfig.from_dict(cfg_dict)
            reconcile(broker=broker, db=monitor.db,
                      live_config_id=cfg.config_id,
                      live_config=cfg)
            # Always re-check stops (DAY stops expire, stops can go missing)
            ensure_stops(broker=broker, db=monitor.db,
                         live_config_id=cfg.config_id,
                         guard_stop_pct=cfg.guard_stop_pct)
        return now
    except Exception:
        log.exception("Periodic reconciliation failed")
        return last_time


def _snapshot_equity(monitor: LiveExitMonitor, last_time: float) -> float:
    """Capture equity snapshots for all active portfolios periodically."""
    now = time.monotonic()
    if now - last_time < _EQUITY_SNAPSHOT_INTERVAL_S:
        return last_time

    try:
        from trader.db.database import (
            get_active_live_configs,
            get_all_watches,
            insert_equity_snapshot,
        )

        active_cfgs = get_active_live_configs(monitor.db)
        if not active_cfgs:
            return now

        # Get all watches once
        all_watches = get_all_watches(monitor.db, limit=500)

        # Group watches by config
        by_config: dict[str, list] = {}
        for w in all_watches:
            cid = w.get("live_config_id")
            if cid:
                by_config.setdefault(cid, []).append(w)

        ts_now = datetime.now(tz=timezone.utc).isoformat()

        for cfg_dict in active_cfgs:
            config_id = cfg_dict.get("config_id", "")
            if not config_id:
                continue

            starting = cfg_dict.get("starting_capital", 0)
            if not starting or starting <= 0:
                continue

            # Check if Alpaca-linked → use real equity
            acct_id = cfg_dict.get("alpaca_account_id")
            if acct_id and monitor.broker_pool:
                broker = monitor.broker_pool.get(acct_id)
                if broker:
                    try:
                        acct_info = broker.get_account()
                        positions = broker.get_positions()
                        total_unrealized = sum(p.unrealized_pl for p in positions)
                        insert_equity_snapshot(
                            monitor.db,
                            config_id=config_id,
                            timestamp=ts_now,
                            equity=acct_info.equity,
                            cash=acct_info.cash,
                            unrealized_pnl=total_unrealized,
                            realized_pnl=acct_info.equity - starting - total_unrealized,
                            position_count=len(positions),
                            source="alpaca",
                        )
                        continue
                    except Exception:
                        log.debug("Alpaca equity fetch failed for %s, using sim", config_id)

            # Sim calculation uses shared valuation helpers so app render and live snapshots
            # answer the same question with the same math.
            watches = by_config.get(config_id, [])
            holding_symbols = sorted({
                str(w.get("symbol") or "").upper()
                for w in watches
                if str(w.get("status") or "") == "holding" and w.get("symbol")
            })

            # Fetch canonical minute marks for all holdings so live rows match
            # the same style of valuation used by backfill.
            quote_fetch_failed = False
            prices: dict[str, float] = {}
            if holding_symbols and monitor.market:
                try:
                    if hasattr(monitor.market, "get_latest_minute_closes"):
                        prices = monitor.market.get_latest_minute_closes(holding_symbols) or {}
                    else:
                        quotes = monitor.market.get_quotes(holding_symbols)
                        for sym in holding_symbols:
                            q = (quotes or {}).get(sym.upper()) or (quotes or {}).get(sym) or {}
                            cp = extract_quote_price(q)
                            if cp is not None:
                                prices[sym] = cp
                except Exception:
                    quote_fetch_failed = True
                    log.debug("Failed to fetch minute marks for sim unrealized PnL")

            # Avoid writing bad "drop to cash" points when quote data is unavailable.
            valuation = summarize_sim_portfolio(watches, cfg_dict, prices)
            if holding_symbols and (quote_fetch_failed or valuation["priced_holding_count"] < len(holding_symbols)):
                log.warning(
                    "Skipping equity snapshot for %s: usable marks for %d/%d holdings",
                    config_id,
                    valuation["priced_holding_count"],
                    len(holding_symbols),
                )
                continue

            insert_equity_snapshot(
                monitor.db,
                config_id=config_id,
                timestamp=ts_now,
                equity=valuation["equity"],
                cash=valuation["cash"],
                unrealized_pnl=valuation["unrealized_dollar"],
                realized_pnl=valuation["realized_dollar"],
                position_count=valuation["holding_count"],
                source="live",
            )

        return now
    except Exception:
        log.exception("Equity snapshot failed")
        return last_time


def _get_poll_interval(monitor: LiveExitMonitor, default: int = 60) -> int:
    """Read poll_interval_s from active configs and use the smallest valid value."""
    try:
        active_cfgs = get_active_live_configs(monitor.db)
        intervals: list[int] = []
        for cfg_dict in active_cfgs:
            cfg = LiveConfig.from_dict(cfg_dict)
            raw = cfg.live_overrides.get("poll_interval_s")
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                intervals.append(value)
        if intervals:
            return min(intervals)
    except Exception:
        pass
    return default


def live_monitoring_loop(
    monitor: LiveExitMonitor,
    interval_s: int = 60,
) -> None:
    """Run the live exit monitor in a loop. Intended as a daemon thread target.

    Bar-level data is persisted immediately via JSONL append (crash-safe).
    The periodic save_all here just writes clean summary JSON files for
    convenience — not needed for safety.

    Poll interval can be overridden via live_overrides["poll_interval_s"].
    If multiple active configs set it, the smallest interval is used.
    """
    log.info("Live exit monitor started (default interval=%ds)", interval_s)
    last_summary_save = time.monotonic()
    last_ensure_stops_date: str | None = None
    last_reconcile_time = 0.0  # force immediate reconcile on first cycle
    last_equity_snapshot_time = 0.0  # force immediate snapshot on first cycle
    while True:
        try:
            # Only run during trading session (regular or extended hours)
            # or within 30 min after close to catch final exit signals
            if is_trading_session_open() or _near_close():
                monitor.run_cycle()

                # Ensure all positions have active stops (once per day at open)
                last_ensure_stops_date = _ensure_stops_if_needed(
                    monitor, last_ensure_stops_date)

                # Periodic reconciliation (every RECONCILE_INTERVAL seconds)
                last_reconcile_time = _periodic_reconcile(
                    monitor, last_reconcile_time)

                # Periodic equity snapshots
                last_equity_snapshot_time = _snapshot_equity(
                    monitor, last_equity_snapshot_time)

            # Periodic clean summary export (convenience, not safety).
            # Bar data is already persisted via JSONL append on each flush.
            if monitor.collector is not None:
                elapsed = time.monotonic() - last_summary_save
                if elapsed >= _SHADOW_SUMMARY_INTERVAL_S:
                    try:
                        paths = monitor.collector.save_all()
                        if paths:
                            log.info("Shadow summary export: %d symbols", len(paths))
                        last_summary_save = time.monotonic()
                    except Exception:
                        log.exception("Shadow summary export failed")
        except Exception:
            log.exception("Live monitor cycle error")
        time.sleep(_get_poll_interval(monitor, interval_s))


def _near_close() -> bool:
    """True if within 30 minutes after session close (catch stragglers).

    Uses extended close (8:00 PM) when ALPACA_EXTENDED_HOURS is enabled,
    otherwise regular close (4:00 PM).
    """
    from trader.market.market_hours import ALPACA_EXTENDED_HOURS, EXTENDED_CLOSE_HOUR
    now = datetime.now(tz=ET)
    if now.weekday() > 4:
        return False
    minutes = now.hour * 60 + now.minute
    close_min = (EXTENDED_CLOSE_HOUR * 60) if ALPACA_EXTENDED_HOURS else (16 * 60)
    return close_min <= minutes < close_min + 30
