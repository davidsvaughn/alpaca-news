"""Quick extractor benchmark harness.

This is not meant to be exhaustive or perfect. It is a pragmatic tool to
compare extraction quality across libraries on a small set of URLs.

Run:
  uv run python -m trader.evidence.benchmark --urls-file data/evidence_urls.txt

Outputs:
  data/evidence/benchmarks/{timestamp}.json
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from trader.evidence.fetch import fetch_url
from trader.evidence.extract import extract_article


@dataclass(frozen=True)
class BenchRow:
    url: str
    ok: bool
    error: str | None
    text_chars: int
    seconds: float
    title: str | None


def run_benchmark(urls: list[str], *, extractor: str) -> list[BenchRow]:
    rows: list[BenchRow] = []
    for url in urls:
        url = url.strip()
        if not url or url.startswith("#"):
            continue
        t0 = time.time()
        try:
            fr = fetch_url(url=url)
            art = extract_article(html=fr.content, url=fr.final_url, extractor=extractor)
            title = None
            # Trafilatura JSON output often includes 'title'
            if isinstance(art.metadata, dict):
                title = art.metadata.get("title")  # type: ignore[assignment]
            rows.append(
                BenchRow(
                    url=url,
                    ok=True,
                    error=None,
                    text_chars=len(art.text),
                    seconds=round(time.time() - t0, 3),
                    title=str(title) if title else None,
                )
            )
        except Exception as e:
            rows.append(
                BenchRow(
                    url=url,
                    ok=False,
                    error=str(e),
                    text_chars=0,
                    seconds=round(time.time() - t0, 3),
                    title=None,
                )
            )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls-file", required=True, help="Path to a newline-delimited URL list")
    ap.add_argument(
        "--extractor",
        default="trafilatura",
        help="trafilatura | newspaper_fulltext",
    )
    args = ap.parse_args()

    urls_file = Path(args.urls_file)
    urls = urls_file.read_text(encoding="utf-8").splitlines()
    rows = run_benchmark(urls, extractor=args.extractor)

    out_dir = Path("data") / "evidence" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time())}.json"

    payload: dict[str, Any] = {
        "extractor": args.extractor,
        "rows": [asdict(r) for r in rows],
        "summary": {
            "total": len(rows),
            "ok": sum(1 for r in rows if r.ok),
            "fail": sum(1 for r in rows if not r.ok),
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote benchmark: {out_path}")


if __name__ == "__main__":
    main()
