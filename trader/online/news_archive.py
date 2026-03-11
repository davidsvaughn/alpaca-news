"""Retention + archival for incoming news JSON files.

Policy:
- Keep recent files in the hot incoming directories for NEWS_ARCHIVE_HOT_HOURS.
- Move older files into per-day ZIP archives under NEWS_ARCHIVE_DIR/<feed>/YYYY-MM-DD.zip.
- Prune ZIP archives older than NEWS_ARCHIVE_RETENTION_DAYS.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from trader.config import Settings


def _utc_day_for_epoch(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%d")


def _archive_feed_dir(
    *,
    incoming_dir: Path,
    archive_root: Path,
    hot_hours: int,
    bucket_name: str | None = None,
) -> tuple[int, int]:
    """Archive stale JSON files for one feed directory.

    Returns (files_archived, bytes_archived).
    """
    if not incoming_dir.exists():
        return (0, 0)

    cutoff_s = time.time() - (max(1, hot_hours) * 3600)
    grouped: dict[str, list[tuple[Path, int]]] = {}

    for p in incoming_dir.iterdir():
        if not p.is_file() or p.suffix.lower() != ".json":
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff_s:
            continue
        day = _utc_day_for_epoch(st.st_mtime)
        grouped.setdefault(day, []).append((p, int(st.st_size)))

    archived_files = 0
    archived_bytes = 0
    feed_name = bucket_name if bucket_name is not None else (incoming_dir.name or "feed")

    for day, entries in grouped.items():
        if feed_name:
            zip_path = archive_root / feed_name / f"{day}.zip"
        else:
            zip_path = archive_root / f"{day}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        entries.sort(key=lambda x: x[0].name)

        existing: set[str] = set()
        if zip_path.exists():
            try:
                with ZipFile(zip_path, mode="r") as zf:
                    existing = set(zf.namelist())
            except Exception:
                # Corrupt archive should not block processing; start a fresh one.
                existing = set()

        with ZipFile(zip_path, mode="a", compression=ZIP_DEFLATED, compresslevel=6) as zf:
            for p, size in entries:
                arcname = p.name
                archived_ok = False
                if arcname in existing:
                    archived_ok = True
                else:
                    try:
                        zf.write(p, arcname=arcname)
                        existing.add(arcname)
                        archived_ok = True
                    except FileNotFoundError:
                        continue
                    except Exception as exc:
                        print(f"WARN: failed to archive {p}: {exc}")
                        continue

                if not archived_ok:
                    continue

                try:
                    p.unlink()
                    archived_files += 1
                    archived_bytes += size
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    print(f"WARN: archived but failed to delete source {p}: {exc}")

    return (archived_files, archived_bytes)


def _prune_old_archives(*, archive_root: Path, retention_days: int) -> int:
    """Delete archive ZIPs older than retention_days. Returns count deleted."""
    if not archive_root.exists():
        return 0

    keep_days = max(1, retention_days)
    cutoff_date = (datetime.now(tz=timezone.utc) - timedelta(days=keep_days)).date()
    deleted = 0

    for z in archive_root.rglob("*.zip"):
        if not z.is_file():
            continue

        prune = False
        try:
            d = datetime.strptime(z.stem, "%Y-%m-%d").date()
            prune = d < cutoff_date
        except ValueError:
            # Fallback: mtime when filename is not a date.
            try:
                mdate = datetime.fromtimestamp(z.stat().st_mtime, tz=timezone.utc).date()
                prune = mdate < cutoff_date
            except OSError:
                prune = False

        if not prune:
            continue

        try:
            z.unlink()
            deleted += 1
        except Exception as exc:
            print(f"WARN: failed to prune archive {z}: {exc}")

    return deleted


def run_news_archive_once(*, settings: Settings) -> tuple[int, int, int]:
    """Run one archive/prune cycle.

    Returns (files_archived, bytes_archived, archives_pruned).
    """
    archive_root = Path(settings.news_archive_dir)
    archive_root.mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_bytes = 0
    for watch_dir in settings.news_watch_dirs:
        incoming = Path(watch_dir)
        incoming.mkdir(parents=True, exist_ok=True)
        fcount, bcount = _archive_feed_dir(
            incoming_dir=incoming,
            archive_root=archive_root,
            hot_hours=settings.news_archive_hot_hours,
            bucket_name=incoming.name or "feed",
        )
        total_files += fcount
        total_bytes += bcount

    pruned = _prune_old_archives(
        archive_root=archive_root,
        retention_days=settings.news_archive_retention_days,
    )
    return (total_files, total_bytes, pruned)


def run_snapshot_archive_once(*, settings: Settings) -> tuple[int, int, int]:
    """Run one archive/prune cycle for snapshot JSON files."""
    snapshots_dir = Path(settings.snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    archive_root = Path(settings.snapshot_archive_dir)
    archive_root.mkdir(parents=True, exist_ok=True)

    files_archived, bytes_archived = _archive_feed_dir(
        incoming_dir=snapshots_dir,
        archive_root=archive_root,
        hot_hours=settings.snapshot_archive_hot_hours,
        bucket_name="",  # put daily ZIPs directly in snapshot_archive_dir
    )
    archives_pruned = _prune_old_archives(
        archive_root=archive_root,
        retention_days=settings.snapshot_archive_retention_days,
    )
    return (files_archived, bytes_archived, archives_pruned)


def run_news_archive_loop(*, settings: Settings) -> None:
    """Background loop that periodically archives + prunes news files."""
    interval_s = max(60, int(settings.news_archive_interval_s))
    while True:
        try:
            news_files, news_bytes, news_pruned = run_news_archive_once(settings=settings)
            snap_files, snap_bytes, snap_pruned = run_snapshot_archive_once(settings=settings)
            if news_files or news_pruned or snap_files or snap_pruned:
                news_mb = news_bytes / (1024 * 1024)
                snap_mb = snap_bytes / (1024 * 1024)
                print(
                    "Archive cycle: "
                    f"news archived={news_files} ({news_mb:.1f} MB), pruned={news_pruned}; "
                    f"snapshots archived={snap_files} ({snap_mb:.1f} MB), pruned={snap_pruned}"
                )
        except Exception as exc:
            print(f"WARN: news archive loop failed: {exc}")
        time.sleep(interval_s)
