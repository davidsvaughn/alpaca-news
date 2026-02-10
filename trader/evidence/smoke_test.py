"""Smoke test for evidence acquisition.

This avoids relying on LLM search output containing URLs.

Run:
  uv run python -m trader.evidence.smoke_test --url https://example.com
"""

from __future__ import annotations

import argparse
from pathlib import Path

from trader.evidence.acquirer import acquire_from_traces


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--extractor", default="trafilatura")
    args = ap.parse_args()

    traces = [
        {
            "trace_id": "trace_1",
            "hop_index": 1,
            "action": {"tool": "web_search", "provider": "openai"},
            "execution": {"model": "test", "start_time": "", "end_time": "", "cost_usd": 0.0},
            "results": [
                {"source_type": "web", "title": "example", "url": args.url, "snippet": "test"},
            ],
        }
    ]

    res = acquire_from_traces(
        traces=traces,
        evidence_root=Path("data") / "evidence",
        max_docs=1,
        extractor=args.extractor,
    )
    print("OK")
    for r in res.refs:
        print(r.doc_id, r.path)


if __name__ == "__main__":
    main()
