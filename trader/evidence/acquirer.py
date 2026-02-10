"""Evidence acquisition orchestrator.

Given ToolTraces from exploration, extract candidate URLs and acquire a bounded
set of documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trader.evidence.extract import extract_article
from trader.evidence.fetch import fetch_url
from trader.evidence.store import EvidenceDocRef, build_evidence_doc, persist_evidence_doc


@dataclass(frozen=True)
class AcquireResult:
    refs: list[EvidenceDocRef]
    traces: list[dict[str, Any]]


def _extract_urls_from_traces(traces: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for t in traces:
        results = t.get("results") or []
        if not isinstance(results, list):
            continue
        for r in results:
            if not isinstance(r, dict):
                continue
            url = r.get("url")
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)
    # Preserve order but dedupe
    out: list[str] = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def acquire_from_traces(
    *,
    traces: list[dict[str, Any]],
    evidence_root: Path,
    max_docs: int = 3,
    extractor: str = "trafilatura",
) -> AcquireResult:
    urls = _extract_urls_from_traces(traces)
    urls = urls[:max_docs]

    refs: list[EvidenceDocRef] = []
    acquire_traces: list[dict[str, Any]] = []

    from trader.models.tool_trace import TraceExecution, new_tool_trace, utc_now_iso

    for i, url in enumerate(urls, start=1):
        start = utc_now_iso()
        fr = fetch_url(url=url)
        article = extract_article(html=fr.content, url=fr.final_url, extractor=extractor)
        doc = build_evidence_doc(
            url=url,
            final_url=fr.final_url,
            status_code=fr.status_code,
            content_type=fr.content_type,
            extracted_text=article.text,
            metadata=article.metadata,
            extractor=extractor,
        )
        ref = persist_evidence_doc(root_dir=evidence_root, doc=doc)
        refs.append(ref)
        end = utc_now_iso()

        acquire_traces.append(
            new_tool_trace(
                trace_id=f"trace_url_fetch_{i}",
                hop_index=0,
                parent_trace_id=None,
                decision_context={
                    "state_summary": "",
                    "reason_for_action": "Acquire and extract URL content for auditability",
                    "symbols": [],
                },
                action={
                    "tool": "url_fetch",
                    "provider": f"httpx+{extractor}",
                    "query_template": "url_fetch",
                    "query": url,
                    "filters": {"max_bytes": 2_000_000},
                },
                execution=TraceExecution(model=extractor, start_time=start, end_time=end, cost_usd=0.0),
                results=[{"source_type": "web", "url": url, "title": "acquired_doc", "snippet": ref.doc_id}],
                extracted_signals={"doc_id": ref.doc_id, "path": ref.path},
                stop_signal={"should_stop": False, "reason": ""},
            )
        )

    return AcquireResult(refs=refs, traces=acquire_traces)
