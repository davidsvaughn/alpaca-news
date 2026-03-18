from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import replace

from fastapi.routing import APIRoute

from trader.config import load_settings
from trader.db.database import (
    activate_live_config,
    get_live_config,
    get_watch,
    insert_live_config,
    insert_watch,
    open_sqlite,
)
from trader.knowledge.store import KnowledgeStore
from trader.models.live_config import LiveConfig
from trader.models.watch import WatchBuilder
from trader.online.event_bus import EventBus
from trader.web.app import create_app


class _DummyMarketDataService:
    def get_latest_minute_closes(self, symbols: list[str]) -> dict[str, float]:
        return {}


def test_delete_live_config_allows_stale_missing_alpaca_account(monkeypatch, tmp_path):
    db = open_sqlite(str(tmp_path / "test.db"))
    settings = replace(
        load_settings(),
        data_dir=str(tmp_path),
        sqlite_path=str(tmp_path / "test.db"),
    )
    knowledge = KnowledgeStore(tmp_path)
    knowledge.ensure_defaults()

    import trader.market.data_service as data_service
    import fastapi.dependencies.utils as fastapi_utils

    monkeypatch.setattr(data_service, "MarketDataService", _DummyMarketDataService)
    monkeypatch.setattr(fastapi_utils, "ensure_multipart_is_installed", lambda: None)
    orchestrator_stub = types.ModuleType("trader.online.orchestrator")
    orchestrator_stub.sync_alpaca_trade_streams = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "trader.online.orchestrator", orchestrator_stub)

    app = create_app(
        settings=settings,
        bus=EventBus(),
        db=db,
        knowledge=knowledge,
    )

    cfg = LiveConfig.create(
        name="Stale Alpaca Config",
        filters={},
        allocation="none",
        allocation_params={},
        starting_capital=10000.0,
        exit_strategy="volume_delta_divergence",
        exit_params={"lookback": 80},
        alpaca_account_id="PA_OLD_DELETED_ACCOUNT",
        alpaca_account_name="Old Paper Account",
    )
    assert insert_live_config(db, config=cfg.to_dict())
    assert activate_live_config(db, cfg.config_id)

    builder = WatchBuilder.create_from_live_config(
        snapshot_id="snap_test",
        symbol="AAPL",
        entry_price=150.0,
        confidence=0.9,
        direction="bullish",
        live_config_id=cfg.config_id,
        exit_strategy=cfg.exit_strategy,
        exit_params=cfg.exit_params,
    )
    builder.qty = 1.0
    builder.alpaca_buy_order_id = "buy_old_1"
    watch = builder.to_watch()
    assert insert_watch(db, watch=watch.to_dict())

    route = next(
        r for r in app.routes
        if isinstance(r, APIRoute)
        and r.path == "/api/live/config/{config_id}"
        and "DELETE" in (r.methods or set())
    )
    response = asyncio.run(route.endpoint(cfg.config_id))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "deleted"
    assert payload["alpaca_account_id"] == "PA_OLD_DELETED_ACCOUNT"
    assert payload["liquidation"]["skipped_reason"] == "alpaca_account_missing"
    assert payload["liquidation"]["force_exit_reason"] == "alpaca_account_missing_on_delete"
    assert payload["liquidation"]["force_exited"] == ["AAPL"]

    assert get_live_config(db, cfg.config_id) is None

    updated_watch = get_watch(db, watch.watch_id)
    assert updated_watch is not None
    assert updated_watch["status"] == "exited"
    assert updated_watch["exit"]["reason"] == "alpaca_account_missing_on_delete"
    assert updated_watch["exit"]["price"] == 150.0


def test_archive_live_config_preserves_config_and_force_exits_missing_alpaca(monkeypatch, tmp_path):
    db = open_sqlite(str(tmp_path / "test.db"))
    settings = replace(
        load_settings(),
        data_dir=str(tmp_path),
        sqlite_path=str(tmp_path / "test.db"),
    )
    knowledge = KnowledgeStore(tmp_path)
    knowledge.ensure_defaults()

    import trader.market.data_service as data_service
    import fastapi.dependencies.utils as fastapi_utils

    monkeypatch.setattr(data_service, "MarketDataService", _DummyMarketDataService)
    monkeypatch.setattr(fastapi_utils, "ensure_multipart_is_installed", lambda: None)
    orchestrator_stub = types.ModuleType("trader.online.orchestrator")
    orchestrator_stub.sync_alpaca_trade_streams = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "trader.online.orchestrator", orchestrator_stub)

    class _ImmediateThread:
        def __init__(self, *, target=None, args=(), kwargs=None, **_other):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    monkeypatch.setattr("trader.web.app.threading.Thread", _ImmediateThread)

    app = create_app(
        settings=settings,
        bus=EventBus(),
        db=db,
        knowledge=knowledge,
    )

    cfg = LiveConfig.create(
        name="Archive Me",
        filters={},
        allocation="none",
        allocation_params={},
        starting_capital=10000.0,
        exit_strategy="volume_delta_divergence",
        exit_params={"lookback": 80},
        alpaca_account_id="PA_OLD_DELETED_ACCOUNT",
        alpaca_account_name="Old Paper Account",
    )
    assert insert_live_config(db, config=cfg.to_dict())
    assert activate_live_config(db, cfg.config_id)

    builder = WatchBuilder.create_from_live_config(
        snapshot_id="snap_archive",
        symbol="MSFT",
        entry_price=250.0,
        confidence=0.9,
        direction="bullish",
        live_config_id=cfg.config_id,
        exit_strategy=cfg.exit_strategy,
        exit_params=cfg.exit_params,
    )
    builder.qty = 2.0
    builder.alpaca_buy_order_id = "buy_archive_1"
    watch = builder.to_watch()
    assert insert_watch(db, watch=watch.to_dict())

    route = next(
        r for r in app.routes
        if isinstance(r, APIRoute)
        and r.path == "/api/live/config/{config_id}/archive"
        and "POST" in (r.methods or set())
    )
    response = asyncio.run(route.endpoint(cfg.config_id))

    assert response.status_code == 202
    payload = json.loads(response.body)
    assert payload["status"] == "archiving"
    assert payload["alpaca_account_id"] == "PA_OLD_DELETED_ACCOUNT"
    assert payload["archive_requested_at"]

    archived_cfg = get_live_config(db, cfg.config_id)
    assert archived_cfg is not None
    assert archived_cfg["active"] is False
    assert archived_cfg["archived"] is True
    assert archived_cfg["archived_at"]
    assert archived_cfg["archive_status"] == "archived"
    assert archived_cfg["archive_error"] is None

    updated_watch = get_watch(db, watch.watch_id)
    assert updated_watch is not None
    assert updated_watch["status"] == "exited"
    assert updated_watch["exit"]["reason"] == "alpaca_account_missing_on_archive"
    assert updated_watch["exit"]["price"] == 250.0


def test_delete_archived_config_skips_shared_alpaca_account_checks(monkeypatch, tmp_path):
    db = open_sqlite(str(tmp_path / "test.db"))
    settings = replace(
        load_settings(),
        data_dir=str(tmp_path),
        sqlite_path=str(tmp_path / "test.db"),
    )
    knowledge = KnowledgeStore(tmp_path)
    knowledge.ensure_defaults()

    import trader.market.data_service as data_service
    import fastapi.dependencies.utils as fastapi_utils

    monkeypatch.setattr(data_service, "MarketDataService", _DummyMarketDataService)
    monkeypatch.setattr(fastapi_utils, "ensure_multipart_is_installed", lambda: None)
    orchestrator_stub = types.ModuleType("trader.online.orchestrator")
    orchestrator_stub.sync_alpaca_trade_streams = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "trader.online.orchestrator", orchestrator_stub)

    app = create_app(
        settings=settings,
        bus=EventBus(),
        db=db,
        knowledge=knowledge,
    )

    archived_cfg = LiveConfig.create(
        name="Archived Config",
        filters={},
        allocation="none",
        allocation_params={},
        starting_capital=10000.0,
        exit_strategy="volume_delta_divergence",
        exit_params={"lookback": 80},
        alpaca_account_id="PA_SHARED",
        alpaca_account_name="Shared Paper",
    )
    archived_dict = archived_cfg.to_dict()
    archived_dict["active"] = False
    archived_dict["archived"] = True
    archived_dict["archived_at"] = "2026-03-17T17:30:50+00:00"
    archived_dict["archive_status"] = "archived"
    assert insert_live_config(db, config=archived_dict)

    active_cfg = LiveConfig.create(
        name="Active Config",
        filters={},
        allocation="none",
        allocation_params={},
        starting_capital=10000.0,
        exit_strategy="volume_delta_divergence",
        exit_params={"lookback": 80},
        alpaca_account_id="PA_SHARED",
        alpaca_account_name="Shared Paper",
    )
    assert insert_live_config(db, config=active_cfg.to_dict())
    assert activate_live_config(db, active_cfg.config_id)

    route = next(
        r for r in app.routes
        if isinstance(r, APIRoute)
        and r.path == "/api/live/config/{config_id}"
        and "DELETE" in (r.methods or set())
    )
    response = asyncio.run(route.endpoint(archived_cfg.config_id))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "deleted"
    assert payload["alpaca_account_id"] == "PA_SHARED"
    assert payload["liquidation"]["skipped_reason"] == "archived_record_only"

    assert get_live_config(db, archived_cfg.config_id) is None
    assert get_live_config(db, active_cfg.config_id) is not None
