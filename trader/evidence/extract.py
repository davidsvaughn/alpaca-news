"""Article extraction.

Default extractor: Trafilatura.

Context7 references:
- trafilatura.fetch_url + trafilatura.extract(... with_metadata=True, output_format='json')
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractedArticle:
    text: str
    metadata: dict[str, Any]


def extract_with_trafilatura(*, html: bytes, url: str | None = None) -> ExtractedArticle:
    try:
        import trafilatura  # type: ignore
    except Exception as e:
        raise RuntimeError("trafilatura is required for evidence extraction") from e

    doc = html.decode("utf-8", errors="ignore")
    out = trafilatura.extract(doc, output_format="json", with_metadata=True)
    if not out:
        # Try again with URL hint (helps with dates sometimes)
        out = trafilatura.extract(doc, output_format="json", with_metadata=True, url=url)
    if not out:
        raise RuntimeError("Trafilatura returned empty extraction")

    data = json.loads(out)
    # In json format, Trafilatura includes 'text' plus metadata keys
    text = str(data.get("text") or "")
    if not text.strip():
        raise RuntimeError("Trafilatura extracted empty text")
    meta = dict(data)
    return ExtractedArticle(text=text, metadata=meta)


def extract_with_newspaper_fulltext(*, html: bytes) -> ExtractedArticle:
    """Fallback extractor using newspaper3k's fulltext() helper."""
    try:
        from newspaper import fulltext  # type: ignore
    except Exception as e:
        raise RuntimeError("newspaper3k is required for this extractor") from e

    doc = html.decode("utf-8", errors="ignore")
    text = str(fulltext(doc) or "")
    if not text.strip():
        raise RuntimeError("newspaper.fulltext extracted empty text")
    return ExtractedArticle(text=text, metadata={"method": "newspaper_fulltext"})


def extract_article(*, html: bytes, url: str | None = None, extractor: str = "trafilatura") -> ExtractedArticle:
    if extractor == "trafilatura":
        return extract_with_trafilatura(html=html, url=url)
    if extractor == "newspaper_fulltext":
        return extract_with_newspaper_fulltext(html=html)
    if extractor == "readability_lxml":
        raise NotImplementedError("readability_lxml extractor not implemented yet")
    raise ValueError(f"Unknown extractor: {extractor}")
