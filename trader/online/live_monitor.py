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
    get_active_live_config,
    get_active_watches,
    insert_watch,
    update_watch,
)
from trader.market.market_hours import ET, add_market_hours, is_market_open, is_trading_session_open
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder

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
    ) -> None:
        self.db = db
        self.bus = bus
        self.data_dir = data_dir
        self.collector = collector
        self.broker_pool = broker_pool  # When set, exits close Alpaca positions
        # Per-symbol indicator cache (reused across cycles to avoid
        # recomputing indicators on unchanged bar history).
        self._indicator_caches: dict[str, dict[tuple[Any, ...], Any]] = {}

    def _get_broker_for_watch(self, watch_dict: dict[str, Any]) -> Any:
        """Look up the Alpaca broker for a watch's LiveConfig. Returns None if not linked."""
        if not self.broker_pool:
            return None
        config_id = watch_dict.get("live_config_id")
        if not config_id:
            return None
        config_dict = get_active_live_config(self.db)
        if not config_dict:
            return None
        acct_id = config_dict.get("alpaca_account_id")
        if not acct_id:
            return None
        return self.broker_pool.get(acct_id)

    def run_cycle(self) -> None:
        """Run one check cycle across all active watches."""
        watches = get_active_watches(self.db)
        if not watches:
            return

        for watch_dict in watches:
            try:
                status = watch_dict.get("status")
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

            result = loop.run_until_complete(
                check_vdd_exit(pool, symbol, lookback_m, bucket_s, min_trades)
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
        min_hold = 5
        market_close: str | None = "16:00"
        live_overrides: dict[str, Any] = {}

        # If watch has a live_config_id, load config for guards/timing
        config_dict = get_active_live_config(self.db)
        if config_dict:
            cfg = LiveConfig.from_dict(config_dict)
            if not strategy_key:
                strategy_key = cfg.exit_strategy
                exit_params = cfg.exit_params
            guard_stop_pct = cfg.guard_stop_pct
            guard_target_pct = cfg.guard_target_pct
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
            # Entry is after all available bars — nothing to evaluate yet
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

        if result.should_exit:
            exit_price = result.exit_price or float(bars.iloc[-1]["Close"])

            # If broker is connected, close Alpaca position and cancel stop order
            broker = self._get_broker_for_watch(watch_dict)
            if broker and watch_dict.get("alpaca_buy_order_id"):
                # Cancel the server-side stop order FIRST (prevent race with stop fill)
                stop_id = watch_dict.get("alpaca_stop_order_id")
                if stop_id:
                    broker.cancel_order(stop_id)
                    builder.alpaca_stop_order_id = None

                # Close position and WAIT for sell confirmation
                try:
                    sell_confirmed = broker.close_position_and_confirm(symbol)
                    if sell_confirmed and sell_confirmed.filled_avg_price:
                        exit_price = sell_confirmed.filled_avg_price
                        log.info("ALPACA SELL CONFIRMED: %s price=%.2f qty=%s",
                                 symbol, exit_price, sell_confirmed.filled_qty)
                except Exception:
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
        update_watch(self.db, watch_id, updated.to_dict())

    # ------------------------------------------------------------------
    # Exited → cooling_off transition
    # ------------------------------------------------------------------

    def _transition_to_cooling_off(self, watch_dict: dict[str, Any]) -> None:
        """Transition an exited watch to cooling_off."""
        builder = WatchBuilder.from_dict(watch_dict)
        watch_id = watch_dict["watch_id"]

        # Determine cooling_off duration from config
        cooling_hours = 24.0  # default
        config_dict = get_active_live_config(self.db)
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
        update_watch(self.db, watch_id, updated.to_dict())

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
        update_watch(self.db, watch_id, updated.to_dict())

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
            print(f"LIVE-PM: {symbol} — no active configs")
            return False
        print(f"LIVE-PM: {symbol} — checking {len(active_configs)} config(s)")

        any_created = False
        for config_dict in active_configs:
            try:
                cfg = LiveConfig.from_dict(config_dict)
                if cfg.paused:
                    print(f"LIVE-PM: {symbol} config={cfg.name} PAUSED — skipping new buys")
                    continue
                result = self._evaluate_for_config(snapshot, symbol, cfg)
                print(f"LIVE-PM: {symbol} config={cfg.name} -> {'BUY' if result else 'SKIP'}")
                if result:
                    any_created = True
            except Exception as e:
                print(f"LIVE-PM: {symbol} config={config_dict.get('name','?')} ERROR: {e}")
                import traceback
                traceback.print_exc()

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
        print(f"LIVE-EVAL: {symbol} dir={direction} conf={confidence} config={cfg.name}")

        # --- Apply filters ---

        if direction == "neutral":
            print(f"LIVE-EVAL: {symbol} SKIP neutral")
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
                    print(f"LIVE-EVAL: {symbol} SKIP conf {signed_conf:.2f} < {conf_min_val:.2f}")
                    return False
            except (ValueError, TypeError):
                pass
        print(f"LIVE-EVAL: {symbol} passed confidence filter")

        # Symbol filter
        sym_filter = filters.get("symbol")
        if sym_filter and sym_filter.strip():
            if symbol.upper() != sym_filter.strip().upper():
                print(f"LIVE-EVAL: {symbol} SKIP symbol filter")
                return False

        # Market metric filters (price, volume, market cap, PE)
        if not self._apply_market_filters(symbol, filters):
            print(f"LIVE-EVAL: {symbol} SKIP market metric filter")
            return False
        print(f"LIVE-EVAL: {symbol} passed market filters")

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

        print(f"LIVE-EVAL: {symbol} allocation {holding_count}/{max_concurrent}")
        if holding_count >= max_concurrent:
            print(f"LIVE-EVAL: {symbol} SKIP at capacity")
            return False

        # --- Determine entry price ---
        entry_price = self._extract_entry_price(snapshot, symbol, cfg.price_delay_minutes)
        print(f"LIVE-EVAL: {symbol} entry_price={entry_price}")
        if entry_price is None or entry_price <= 0:
            print(f"LIVE-EVAL: {symbol} SKIP no entry price")
            return False

        # Price minimum filter — uses entry price (current price in live mode).
        # This corresponds to the "Price@x" filter in backtest, which in live
        # mode just means "minimum stock price to consider".
        price_10_min = filters.get("price_10_min")
        if price_10_min:
            try:
                if entry_price < float(price_10_min):
                    log.info("SKIP %s: price %.2f < min $%s", symbol, entry_price, price_10_min)
                    return False
            except (ValueError, TypeError):
                pass

        # --- Duplicate symbol guard (per-portfolio) ---
        existing = get_active_watches(self.db)
        for w in existing:
            if (w.get("status") == "holding"
                    and w.get("symbol") == symbol
                    and w.get("live_config_id") == cfg.config_id):
                print(f"LIVE-EVAL: {symbol} SKIP already holding in config {cfg.name}")
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
                print(f"LIVE-EVAL: {symbol} SKIP — Alpaca already holds position "
                      f"(qty={existing_pos.qty:.4f}, entry=${existing_pos.avg_entry_price:.2f})")
                log.warning("LIVE-EVAL: %s blocked buy — Alpaca already holds %.4f shares",
                            symbol, existing_pos.qty)
                return False

            alloc_pct = float((cfg.allocation_params or {}).get("alloc_pct", 5))
            position_size = cfg.starting_capital * alloc_pct / 100.0

            # Guard: don't buy if we can't afford at least 1 whole share
            if entry_price > 0 and position_size / entry_price < 1.0:
                print(f"LIVE-EVAL: {symbol} SKIP — position size ${position_size:.0f} "
                      f"< 1 share at ${entry_price:.2f}")
                return False

            # Submit market buy and WAIT for fill confirmation
            try:
                buy_confirmed = broker.buy_and_confirm(symbol, notional=position_size)
            except Exception:
                log.exception("ALPACA BUY FAILED for %s — skipping (no watch created)", symbol)
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
            # Local/shadow portfolio — calculate qty from allocation
            alloc_pct = float((cfg.allocation_params or {}).get("alloc_pct", 5))
            position_size = cfg.starting_capital * alloc_pct / 100.0
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

        # Start streaming for this symbol (shadow collector + Schwab stream)
        if self.collector is not None:
            self.collector.add_symbol(symbol)
        if self.market is not None and hasattr(self.market, '_schwab') and self.market.schwab_available:
            try:
                self.market._schwab.start_stream([symbol])
                log.info("Started Schwab stream for %s", symbol)
            except Exception:
                log.exception("Failed to start Schwab stream for %s", symbol)

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

    def _extract_entry_price(
        self,
        snapshot: dict[str, Any],
        symbol: str,
        delay_minutes: int,
    ) -> float | None:
        """Extract entry price from snapshot data.

        In live mode, prioritizes current price (price_context) since the
        delayed price_at value may not be populated yet at seal time.
        Falls back through multiple sources.
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

        # Try price_at dict (from delayed price collection, may not exist yet)
        price_at = snapshot.get("price_at") or {}
        delay_key = str(delay_minutes)
        if delay_key in price_at:
            val = price_at[delay_key]
            if val and float(val) > 0:
                return float(val)

        # Try legacy price_10min
        p10 = snapshot.get("price_10min")
        if p10 and float(p10) > 0:
            return float(p10)

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
            price = q.get("lastPrice") or q.get("last_price")
        if price is None:
            price = data.get("lastPrice") or data.get("regularMarketPrice")

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
_EQUITY_SNAPSHOT_INTERVAL_S = float(os.getenv("EQUITY_SNAPSHOT_INTERVAL", "900"))  # 15 min default


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

            # Sim calculation (same logic as _compute_sim in app.py)
            watches = by_config.get(config_id, [])
            alloc = cfg_dict.get("allocation", "")
            alloc_params = cfg_dict.get("allocation_params") or {}

            if alloc == "max_positions":
                max_pos = int(alloc_params.get("max_pos", 10))
            elif alloc in ("fixed_dollar", "ranking_realloc"):
                alloc_pct = float(alloc_params.get("alloc_pct", 5))
                max_pos = max(1, int(100 / alloc_pct))
            else:
                max_pos = 20

            pos_size = starting / max_pos
            realized_dollar = 0.0
            unrealized_dollar = 0.0
            holding_count = 0

            for w in watches:
                status = w.get("status", "")
                if status in ("exited", "cooling_off", "sealed", "retrospective"):
                    ex = w.get("exit")
                    if ex:
                        rpnl = ex.get("realized_pnl_pct")
                        if rpnl is not None:
                            realized_dollar += pos_size * float(rpnl) / 100
                elif status == "holding":
                    holding_count += 1
                    upnl = w.get("unrealized_pnl")
                    if upnl is not None:
                        unrealized_dollar += pos_size * float(upnl) / 100

            equity = starting + realized_dollar + unrealized_dollar
            cash = starting - (holding_count * pos_size) + realized_dollar

            insert_equity_snapshot(
                monitor.db,
                config_id=config_id,
                timestamp=ts_now,
                equity=round(equity, 2),
                cash=round(cash, 2),
                unrealized_pnl=round(unrealized_dollar, 2),
                realized_pnl=round(realized_dollar, 2),
                position_count=holding_count,
                source="live",
            )

        return now
    except Exception:
        log.exception("Equity snapshot failed")
        return last_time


def _get_poll_interval(monitor: LiveExitMonitor, default: int = 60) -> int:
    """Read poll_interval_s from active config's live_overrides, or use default."""
    try:
        config_dict = get_active_live_config(monitor.db)
        if config_dict:
            cfg = LiveConfig.from_dict(config_dict)
            return int(cfg.live_overrides.get("poll_interval_s", default))
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

    Poll interval can be overridden via live_overrides["poll_interval_s"]
    in the active LiveConfig (re-read each cycle).
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
