"""Helpers for extracting structured data from LLM responses.

LLMs frequently wrap JSON in markdown code fences or add prose before/after.
These helpers robustly extract the JSON payload.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1")


_CODE_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n?(.*?)```",
    re.DOTALL,
)


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from LLM output text.

    Handles:
    - Raw JSON (no fences)
    - Markdown code fences (```json ... ``` or ``` ... ```)
    - Leading/trailing prose around the JSON

    Raises RuntimeError if no valid JSON object can be found.
    """
    text = text.strip()

    # Attempt 1: try parsing the whole thing directly
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Attempt 2: extract from code fences
    for match in _CODE_FENCE_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    # Attempt 3: find the first { ... } block by scanning for balanced braces
    first_brace = text.find("{")
    if first_brace != -1:
        # Find the matching closing brace
        depth = 0
        for i in range(first_brace, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[first_brace : i + 1]
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        break

    raise RuntimeError(
        f"Could not extract JSON object from LLM response. "
        f"Text (first 500 chars): {text[:500]!r}"
    )
