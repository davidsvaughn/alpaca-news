"""Knowledge store manager.

This layer manages human-editable JSON files under `data/knowledge/`.

Phase 1:
- load (create defaults if missing)
- update specific sections (append-only) with explicit writes
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Protects read-modify-write operations on knowledge JSON files.
_file_lock = threading.Lock()

_log = logging.getLogger(__name__)

# ── Skip-pattern quality gate ──────────────────────────────────────────

# Patterns containing any of these (case-insensitive) are rejected because
# they could suppress genuinely price-moving news.
PROTECTED_KEYWORDS: frozenset[str] = frozenset({
    # Earnings / financials
    "earnings", "revenue", "eps", "profit", "loss",
    "beat", "miss", "surprise", "exceeded", "fell short",
    "guidance", "outlook",
    # M&A
    "acquisition", "acquire", "merger", "takeover", "buyout",
    # Regulatory
    "fda", "ema", "mhra", "approval", "approved", "approves",
    # Leadership
    "ceo", "cfo", "cto", "coo", "c-suite",
    "resign", "fired", "ousted", "stepping down",
    # Capital actions
    "buyback", "repurchase",
    "dividend cut", "dividend increase", "special dividend",
    "stock split", "reverse split",
    # Legal / risk
    "sec investigation", "doj", "indictment", "fraud",
    "bankruptcy", "chapter 11", "default",
    "trading halt", "halted",
    # Activist
    "activist", "proxy fight",
    # Contracts
    "contract win", "contract award",
})

_MIN_PATTERN_LEN = 6
_MAX_PATTERN_LEN = 80


def validate_skip_pattern(pattern: str) -> tuple[bool, str]:
    """Check whether a candidate skip pattern meets quality criteria.

    Returns ``(is_valid, reason)``.  Rejected patterns should not be persisted.
    """
    p = pattern.strip()
    if not p:
        return False, "empty"
    if len(p) < _MIN_PATTERN_LEN:
        return False, f"too short ({len(p)} chars)"
    if len(p) > _MAX_PATTERN_LEN:
        return False, f"too long ({len(p)} chars)"

    # Must be valid regex
    try:
        re.compile(p)
    except re.error as exc:
        return False, f"invalid regex: {exc}"

    p_lower = p.lower()

    for kw in PROTECTED_KEYWORDS:
        if kw in p_lower:
            return False, f"protected keyword '{kw}'"

    return True, "ok"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonc(path: Path) -> dict[str, Any]:
    """Read a JSONC file (JSON with ``//`` line comments)."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    stripped = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    # Remove trailing commas left when the last array/object entry is commented out.
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return json.loads(stripped)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonc(path: Path, data: dict[str, Any]) -> None:
    """Write JSON data to a JSONC file, preserving ``//`` comment lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_text = json.dumps(data, ensure_ascii=False, indent=2)

    if not path.exists():
        path.write_text(new_text + "\n", encoding="utf-8")
        return

    # Collect comment lines, anchored to the non-comment line that follows them.
    old_lines = path.read_text(encoding="utf-8").splitlines()
    anchor_groups: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for line in old_lines:
        if line.strip().startswith("//"):
            pending.append(line)
        elif pending:
            anchor_groups.append((line.strip().rstrip(","), list(pending)))
            pending.clear()
    trailing_comments = list(pending)

    if not anchor_groups and not trailing_comments:
        path.write_text(new_text + "\n", encoding="utf-8")
        return

    # Index by anchor; supports multiple groups sharing the same anchor.
    by_anchor: dict[str, list[list[str]]] = {}
    for anchor, group in anchor_groups:
        by_anchor.setdefault(anchor, []).append(group)

    # Re-insert comments before their anchor lines.
    result: list[str] = []
    for line in new_text.splitlines():
        key = line.strip().rstrip(",")
        if key in by_anchor and by_anchor[key]:
            result.extend(by_anchor[key].pop(0))
            if not by_anchor[key]:
                del by_anchor[key]
        result.append(line)

    # Orphaned comments (anchor was removed) + trailing → before closing brace.
    orphaned = [c for groups in by_anchor.values() for g in groups for c in g]
    orphaned.extend(trailing_comments)
    if orphaned:
        pos = len(result) - 1 if result and result[-1].strip() == "}" else len(result)
        for i, c in enumerate(orphaned):
            result.insert(pos + i, c)

    path.write_text("\n".join(result) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class KnowledgeStore:
    root_dir: Path

    @property
    def knowledge_dir(self) -> Path:
        return self.root_dir / "knowledge"

    def ensure_defaults(self) -> None:
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

        defaults: dict[str, dict[str, Any]] = {
            "skip_patterns.jsonc": {
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
            "investigate_patterns.json": {
                "headline_keywords": [],
                "last_updated": _utc_now_iso(),
                "human_edited": 0,
            },
        }

        for fname, content in defaults.items():
            path = self.knowledge_dir / fname
            if not path.exists():
                _write_json(path, content)

    def load_skip_patterns(self) -> dict[str, Any]:
        self.ensure_defaults()
        return _read_jsonc(self.knowledge_dir / "skip_patterns.jsonc")

    def load_investigate_patterns(self) -> dict[str, Any]:
        self.ensure_defaults()
        return _read_json(self.knowledge_dir / "investigate_patterns.json")

    def append_to_list(
        self, filename: str, key: str, item: Any
    ) -> None:
        """Append an item to a list field in a knowledge JSON file (dedup by equality)."""
        self.ensure_defaults()
        with _file_lock:
            path = self.knowledge_dir / filename
            data = _read_json(path)
            lst = data.get(key, [])
            if item not in lst:
                lst.append(item)
                data[key] = lst
                data["last_updated"] = _utc_now_iso()
                _write_json(path, data)

    def append_skip_keywords(self, keywords: list[str]) -> int:
        """Append validated skip keywords.  Returns count actually added."""
        if not keywords:
            return 0
        with _file_lock:
            data = self.load_skip_patterns()
            existing = set(str(x).lower() for x in data.get("headline_keywords", []))
            added = 0
            for kw in keywords:
                k = kw.strip()
                if not k or k.lower() in existing:
                    continue

                valid, reason = validate_skip_pattern(k)
                if not valid:
                    _log.debug("Skip pattern rejected: %r (%s)", k, reason)
                    continue

                data.setdefault("headline_keywords", []).append(k)
                existing.add(k.lower())
                added += 1

            if added:
                data["auto_learned"] = int(data.get("auto_learned", 0)) + added
                data["last_updated"] = _utc_now_iso()
                _write_jsonc(self.knowledge_dir / "skip_patterns.jsonc", data)
        return added
