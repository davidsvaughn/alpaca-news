"""Tests for news JSON archive/retention logic."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from trader.online.news_archive import run_news_archive_once


def _touch_json(path: Path, *, content: str, age_hours: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ts = time.time() - int(age_hours * 3600)
    os.utime(path, (ts, ts))


def test_archives_old_files_and_keeps_hot_files(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming" / "insight_sentry"
    old_file = incoming / "old.json"
    hot_file = incoming / "hot.json"
    _touch_json(old_file, content='{"id":1}', age_hours=30)
    _touch_json(hot_file, content='{"id":2}', age_hours=1)

    settings = SimpleNamespace(
        news_watch_dirs=[str(incoming)],
        news_archive_dir=str(tmp_path / "archive"),
        news_archive_hot_hours=24,
        news_archive_retention_days=90,
    )

    archived, _bytes, pruned = run_news_archive_once(settings=settings)
    assert archived == 1
    assert pruned == 0
    assert not old_file.exists()
    assert hot_file.exists()

    zips = list((tmp_path / "archive" / "insight_sentry").glob("*.zip"))
    assert len(zips) == 1
    with ZipFile(zips[0], mode="r") as zf:
        assert "old.json" in zf.namelist()
        assert "hot.json" not in zf.namelist()


def test_prunes_archives_older_than_retention(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    old_zip = archive_root / "alpaca" / "2020-01-01.zip"
    old_zip.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(old_zip, mode="w") as zf:
        zf.writestr("x.json", '{"x":1}')

    recent_zip = archive_root / "alpaca" / "2099-01-01.zip"
    with ZipFile(recent_zip, mode="w") as zf:
        zf.writestr("y.json", '{"y":1}')

    settings = SimpleNamespace(
        news_watch_dirs=[],
        news_archive_dir=str(archive_root),
        news_archive_hot_hours=24,
        news_archive_retention_days=90,
    )

    archived, _bytes, pruned = run_news_archive_once(settings=settings)
    assert archived == 0
    assert pruned == 1
    assert not old_zip.exists()
    assert recent_zip.exists()
