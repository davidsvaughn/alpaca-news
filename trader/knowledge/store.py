"""Knowledge store manager.

This layer manages human-editable JSON files under `data/knowledge/`.

Phase 1:
- load (create defaults if missing)
- update specific sections (append-only) with explicit writes
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class KnowledgeStore:
    root_dir: Path

    @property
    def knowledge_dir(self) -> Path:
        return self.root_dir / "knowledge"

    def ensure_defaults(self) -> None:
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

        defaults: dict[str, dict[str, Any]] = {
            "skip_patterns.json": {
                "headline_keywords": [],
                "sources_to_skip": [],
                "symbol_contexts": {},
                "last_updated": _utc_now_iso(),
                "auto_learned": 0,
                "human_edited": 0,
            },
            "reliable_sources.json": {"domains": {}, "last_updated": _utc_now_iso()},
            "search_strategies.json": {"templates": {}, "last_updated": _utc_now_iso()},
            "x_search_strategies.json": {"templates": {}, "last_updated": _utc_now_iso()},
            "signal_patterns.json": {"patterns": [], "last_updated": _utc_now_iso()},
            "anti_patterns.json": {"patterns": [], "last_updated": _utc_now_iso()},
            "model_notes.json": {"notes": [], "last_updated": _utc_now_iso()},
        }

        for fname, content in defaults.items():
            path = self.knowledge_dir / fname
            if not path.exists():
                _write_json(path, content)

    def load_skip_patterns(self) -> dict[str, Any]:
        self.ensure_defaults()
        return _read_json(self.knowledge_dir / "skip_patterns.json")

    def append_skip_keywords(self, keywords: list[str]) -> None:
        if not keywords:
            return
        data = self.load_skip_patterns()
        existing = set(str(x).lower() for x in data.get("headline_keywords", []))
        added = 0
        for kw in keywords:
            k = kw.strip()
            if not k:
                continue
            if k.lower() in existing:
                continue
            data.setdefault("headline_keywords", []).append(k)
            existing.add(k.lower())
            added += 1
        if added:
            data["auto_learned"] = int(data.get("auto_learned", 0)) + added
            data["last_updated"] = _utc_now_iso()
            _write_json(self.knowledge_dir / "skip_patterns.json", data)
