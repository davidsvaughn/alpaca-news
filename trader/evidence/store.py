"""Evidence document persistence.

Stores a JSON record under data/evidence/{doc_id}.json.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_text(t: str) -> str:
    return "sha256:" + hashlib.sha256(t.encode("utf-8", errors="ignore")).hexdigest()


@dataclass(frozen=True)
class EvidenceDocRef:
    doc_id: str
    url: str
    path: str


def persist_evidence_doc(*, root_dir: Path, doc: dict[str, Any]) -> EvidenceDocRef:
    root_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    doc_id = sha256_bytes(raw)
    path = root_dir / f"{doc_id.replace(':', '_')}.json"
    if not path.exists():
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return EvidenceDocRef(doc_id=doc_id, url=str(doc.get("url") or ""), path=str(path))


def build_evidence_doc(
    *,
    url: str,
    final_url: str,
    status_code: int,
    content_type: str | None,
    extracted_text: str,
    metadata: dict[str, Any],
    extractor: str,
) -> dict[str, Any]:
    return {
        "version": "v1",
        "retrieved_at": _utc_now_iso(),
        "url": url,
        "final_url": final_url,
        "http": {
            "status": status_code,
            "content_type": content_type,
        },
        "extraction": {
            "method": extractor,
            "metadata": metadata,
        },
        "text": extracted_text,
        "text_sha256": sha256_text(extracted_text),
    }
