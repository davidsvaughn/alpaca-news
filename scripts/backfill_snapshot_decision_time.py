"""Backfill snapshot decision timestamps and historical decision-time price.

For older snapshots that predate the explicit ``decision_at`` field, infer the
decision/seal time from the persisted snapshot artifact:

- live JSON file ``mtime`` for unarchived snapshots
- ZIP member timestamp for archived snapshots

The script updates SQLite ``snapshot_json`` records and can also rewrite the
JSON artifacts so the stored files stay self-consistent.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from trader.config import load_settings
from trader.db.database import open_sqlite, update_snapshot_json
from trader.market.backtest import _get_ohlcv_1m, _parse_entry_time
from trader.market.data_service import MarketDataService
from trader.models import atomic_write_text
from trader.online.symbol_filter import load_symbol_lists
from trader.snapshot_decision import (
    build_decision_metrics_payload,
    compute_avg_daily_volume_from_bars,
    merge_decision_symbol_metrics,
    primary_symbol,
    snapshot_entry_time,
)


LOCAL_TZ_NAME = os.environ.get("TZ") or "America/Kentucky/Monticello"


@dataclass(frozen=True)
class LiveArtifact:
    path: Path
    mtime_iso: str
    atime_ns: int
    mtime_ns: int


@dataclass(frozen=True)
class ArchiveArtifact:
    zip_path: Path
    member_name: str
    timestamp_iso: str
    info: ZipInfo


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect changes without writing them.")
    parser.add_argument("--skip-artifacts", action="store_true", help="Update SQLite only; do not rewrite JSON/ZIP artifacts.")
    parser.add_argument("--skip-price", action="store_true", help="Backfill decision_at only; skip historical price lookup.")
    parser.add_argument(
        "--fill-current-fundamentals",
        action="store_true",
        help="For rows still missing avg_vol/mkt_cap/pe, freeze current quote fundamentals into decision_metrics.",
    )
    parser.add_argument(
        "--fill-historical-avg-vol",
        action="store_true",
        help="For rows still missing avg_vol, compute it from historical 1-minute bars before decision_at.",
    )
    parser.add_argument(
        "--verified-only",
        action="store_true",
        help="Only process snapshots whose primary symbol is in the cached verified buyable list.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Process at most N snapshots (0 = no limit).")
    return parser.parse_args()


def _iso_from_epoch(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


def _iso_from_zipinfo(info: ZipInfo) -> str:
    from zoneinfo import ZoneInfo

    local_dt = datetime(*info.date_time, tzinfo=ZoneInfo(LOCAL_TZ_NAME))
    return local_dt.astimezone(timezone.utc).isoformat()


def _load_all_snapshots(db_path: str) -> list[dict]:
    db = open_sqlite(db_path)
    with db.engine.connect() as conn:
        rows = conn.execute(text("SELECT snapshot_json FROM snapshots ORDER BY created_at DESC")).fetchall()
    result: list[dict] = []
    for (raw,) in rows:
        result.append(json.loads(raw) if isinstance(raw, str) else raw)
    return result


def _index_live_artifacts(snapshots_dir: Path) -> dict[str, LiveArtifact]:
    artifacts: dict[str, LiveArtifact] = {}
    for path in snapshots_dir.glob("*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        artifacts[path.stem] = LiveArtifact(
            path=path,
            mtime_iso=_iso_from_epoch(st.st_mtime),
            atime_ns=st.st_atime_ns,
            mtime_ns=st.st_mtime_ns,
        )
    return artifacts


def _index_archive_artifacts(archive_dir: Path) -> dict[str, ArchiveArtifact]:
    artifacts: dict[str, ArchiveArtifact] = {}
    for zip_path in sorted(archive_dir.glob("*.zip")):
        try:
            with ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.endswith(".json"):
                        continue
                    artifacts[Path(info.filename).stem] = ArchiveArtifact(
                        zip_path=zip_path,
                        member_name=info.filename,
                        timestamp_iso=_iso_from_zipinfo(info),
                        info=info,
                    )
        except Exception:
            continue
    return artifacts


def _lookup_decision_price(
    *,
    symbol: str,
    decision_at: str,
    df_cache: dict[tuple[str, str, str], object],
) -> float | None:
    import pandas as pd

    entry_dt = _parse_entry_time(str(decision_at))
    start_date = entry_dt.date().isoformat()
    cache_key = (symbol.upper(), start_date, start_date)
    df = df_cache.get(cache_key)
    if df is None:
        df = _get_ohlcv_1m(symbol.upper(), start_date, start_date)
        df_cache[cache_key] = df
    if df is None or getattr(df, "empty", True):
        return None

    entry_ts = pd.Timestamp(entry_dt, tz=df.index.tz).as_unit(df.index.unit)
    entry_idx = df.index.searchsorted(entry_ts)
    if entry_idx >= len(df):
        return None
    return float(df.iloc[entry_idx]["Close"])


def _lookup_historical_avg_vol(
    *,
    symbol: str,
    decision_at: str,
    df_cache: dict[tuple[str, str, str], object],
    lookback_days: int = 10,
) -> float | None:
    entry_dt = _parse_entry_time(str(decision_at))
    end_date = entry_dt.date().isoformat()
    start_date = (entry_dt.date() - timedelta(days=max(30, lookback_days * 3))).isoformat()
    cache_key = (symbol.upper(), start_date, end_date)
    df = df_cache.get(cache_key)
    if df is None:
        df = _get_ohlcv_1m(symbol.upper(), start_date, end_date)
        df_cache[cache_key] = df
    return compute_avg_daily_volume_from_bars(
        df,
        decision_at=decision_at,
        lookback_days=lookback_days,
    )


def _rewrite_live_json(path: Path, snapshot: dict, *, atime_ns: int, mtime_ns: int) -> None:
    atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    os.utime(path, ns=(atime_ns, mtime_ns))


def _clone_zipinfo(info: ZipInfo) -> ZipInfo:
    out = ZipInfo(filename=info.filename, date_time=info.date_time)
    out.compress_type = info.compress_type or ZIP_DEFLATED
    out.comment = info.comment
    out.extra = info.extra
    out.create_system = info.create_system
    out.create_version = info.create_version
    out.extract_version = info.extract_version
    out.flag_bits = info.flag_bits
    out.volume = info.volume
    out.internal_attr = info.internal_attr
    out.external_attr = info.external_attr
    return out


def _rewrite_zip(zip_path: Path, updates: dict[str, dict]) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=zip_path.parent, suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with ZipFile(zip_path, "r") as src, ZipFile(tmp_path, "w") as dst:
            dst.comment = src.comment
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename in updates:
                    data = (json.dumps(updates[info.filename], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                dst.writestr(_clone_zipinfo(info), data)
        tmp_path.replace(zip_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _fetch_current_fundamentals(symbols: list[str]) -> dict[str, dict[str, float | str | None]]:
    market = MarketDataService()
    result: dict[str, dict[str, float | str | None]] = {}
    chunk_size = 50
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        batch = market.get_quotes_with_fundamentals(
            chunk,
            fill_avg_volume_from_history=True,
            as_of=datetime.now(tz=timezone.utc),
        )
        for symbol in chunk:
            data = batch.get(symbol) or {}
            result[symbol] = {
                "avg_vol": data.get("avg_10d_volume")
                if data.get("avg_10d_volume") not in (None, "")
                else data.get("avg_volume"),
                "mkt_cap": data.get("market_cap"),
                "pe": data.get("pe_ratio"),
                "source": data.get("source") or "quote_fundamentals",
            }
    return result


def main() -> None:
    args = _parse_args()
    settings = load_settings()
    db = open_sqlite(settings.sqlite_path)
    snapshots = _load_all_snapshots(settings.sqlite_path)
    live_artifacts = _index_live_artifacts(Path(settings.snapshots_dir))
    archive_artifacts = _index_archive_artifacts(Path(settings.snapshot_archive_dir))
    df_cache: dict[tuple[str, str, str], object] = {}

    updated_db = 0
    updated_live = 0
    updated_zip_members = 0
    zip_updates: dict[Path, dict[str, dict]] = {}
    processed = 0
    current_fundamentals: dict[str, dict[str, float | str | None]] = {}
    verified_symbols = {
        str(sym or "").strip().upper()
        for sym in (load_symbol_lists(settings.data_dir).get("verified") or [])
    }

    if args.fill_current_fundamentals:
        symbols_needed: list[str] = []
        seen_symbols: set[str] = set()
        for snapshot in snapshots:
            symbol = primary_symbol(snapshot)
            if not symbol or symbol in seen_symbols or symbol not in verified_symbols:
                continue
            existing_metrics = (snapshot.get("decision_metrics") or {}).get("per_symbol") or {}
            existing_symbol_metrics = existing_metrics.get(symbol) or {}
            needs = any(existing_symbol_metrics.get(k) in (None, "") for k in ("avg_vol", "mkt_cap", "pe"))
            if not needs:
                continue
            seen_symbols.add(symbol)
            symbols_needed.append(symbol)
        if symbols_needed:
            current_fundamentals = _fetch_current_fundamentals(symbols_needed)

    for snapshot in snapshots:
        if args.limit and processed >= args.limit:
            break
        processed += 1

        snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
        symbol = primary_symbol(snapshot)
        if args.verified_only and (not symbol or symbol not in verified_symbols):
            continue
        if not snapshot_id:
            continue

        live_art = live_artifacts.get(snapshot_id)
        archive_art = archive_artifacts.get(snapshot_id)
        artifact_iso = live_art.mtime_iso if live_art is not None else (archive_art.timestamp_iso if archive_art else "")

        existing_decision_at = str(snapshot.get("decision_at") or "").strip()
        decision_at = existing_decision_at or artifact_iso
        if not decision_at:
            continue

        price_missing = bool(symbol) and args.skip_price is False
        decision_price = None
        existing_metrics = (snapshot.get("decision_metrics") or {}).get("per_symbol") or {}
        existing_symbol_metrics = existing_metrics.get(symbol) or {}
        if existing_symbol_metrics.get("price") not in (None, ""):
            decision_price = float(existing_symbol_metrics["price"])
            price_missing = False

        changed = False
        updated = dict(snapshot)
        if not existing_decision_at and artifact_iso:
            updated["decision_at"] = decision_at
            changed = True

        if price_missing:
            price = _lookup_decision_price(symbol=symbol, decision_at=decision_at, df_cache=df_cache)
            if price is not None:
                if not updated.get("decision_metrics"):
                    updated["decision_metrics"] = build_decision_metrics_payload(
                        per_symbol={},
                        captured_at=decision_at,
                        source="historical_1m",
                    )
                updated = merge_decision_symbol_metrics(
                    updated,
                    symbol=symbol,
                    metrics={"price": price, "source": "historical_1m"},
                    captured_at=decision_at,
                    source="historical_1m",
                )
                changed = True

        if args.fill_historical_avg_vol and symbol and symbol in verified_symbols:
            existing_metrics = (updated.get("decision_metrics") or {}).get("per_symbol") or {}
            existing_symbol_metrics = existing_metrics.get(symbol) or {}
            if existing_symbol_metrics.get("avg_vol") in (None, ""):
                avg_vol = _lookup_historical_avg_vol(symbol=symbol, decision_at=decision_at, df_cache=df_cache)
                if avg_vol is not None:
                    if not updated.get("decision_metrics"):
                        updated["decision_metrics"] = build_decision_metrics_payload(
                            per_symbol={},
                            captured_at=decision_at,
                            source="historical_avg_vol_backfill",
                        )
                    updated = merge_decision_symbol_metrics(
                        updated,
                        symbol=symbol,
                        metrics={"avg_vol": avg_vol, "source": "historical_avg_vol_backfill"},
                        captured_at=(updated.get("decision_metrics") or {}).get("captured_at") or decision_at,
                        source="historical_avg_vol_backfill",
                    )
                    changed = True

        if args.fill_current_fundamentals and symbol:
            current_metrics = current_fundamentals.get(symbol) or {}
            if current_metrics:
                existing_metrics = (updated.get("decision_metrics") or {}).get("per_symbol") or {}
                existing_symbol_metrics = existing_metrics.get(symbol) or {}
                missing_fields = {
                    key: current_metrics.get(key)
                    for key in ("avg_vol", "mkt_cap", "pe")
                    if existing_symbol_metrics.get(key) in (None, "") and current_metrics.get(key) not in (None, "")
                }
                if missing_fields:
                    if not updated.get("decision_metrics"):
                        updated["decision_metrics"] = build_decision_metrics_payload(
                            per_symbol={},
                            captured_at=datetime.now(tz=timezone.utc).isoformat(),
                            source="current_quote_fundamentals_backfill",
                        )
                    updated = merge_decision_symbol_metrics(
                        updated,
                        symbol=symbol,
                        metrics={**missing_fields, "source": "current_quote_fundamentals_backfill"},
                        captured_at=(updated.get("decision_metrics") or {}).get("captured_at") or decision_at,
                        source="current_quote_fundamentals_backfill",
                    )
                    changed = True

        if not changed:
            continue

        if not args.dry_run:
            update_snapshot_json(db, snapshot_id, updated)
        updated_db += 1

        if args.skip_artifacts:
            continue

        if live_art is not None:
            if not args.dry_run:
                _rewrite_live_json(
                    live_art.path,
                    updated,
                    atime_ns=live_art.atime_ns,
                    mtime_ns=live_art.mtime_ns,
                )
            updated_live += 1
            continue

        if archive_art is not None:
            zip_updates.setdefault(archive_art.zip_path, {})[archive_art.member_name] = updated
            updated_zip_members += 1

    if not args.dry_run:
        for zip_path, updates in zip_updates.items():
            _rewrite_zip(zip_path, updates)

    print(
        json.dumps(
            {
                "processed": processed,
                "updated_db": updated_db,
                "updated_live": updated_live,
                "updated_zip_members": updated_zip_members,
                "dry_run": args.dry_run,
                "skip_artifacts": args.skip_artifacts,
                "skip_price": args.skip_price,
                "fill_historical_avg_vol": args.fill_historical_avg_vol,
                "fill_current_fundamentals": args.fill_current_fundamentals,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
