"""Startup health check for LLM API providers.

Performs lightweight validation that each configured API key is valid and the
provider is reachable.  Logs results (pass/warn/fail) and returns a summary.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    provider: str
    ok: bool
    message: str
    elapsed_s: float


def _check_grok() -> CheckResult:
    """Validate xAI/Grok API key by listing models."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        return CheckResult("grok", False, "XAI_API_KEY not set", 0)
    t0 = time.time()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url="https://api.x.ai/v1/", timeout=15)
        models = client.models.list()
        elapsed = time.time() - t0
        count = len(list(models))
        return CheckResult("grok", True, f"OK ({count} models, {elapsed:.1f}s)", elapsed)
    except Exception as e:
        return CheckResult("grok", False, f"{type(e).__name__}: {e}", time.time() - t0)


def _check_openai() -> CheckResult:
    """Validate OpenAI API key by listing models."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return CheckResult("openai", False, "OPENAI_API_KEY not set", 0)
    t0 = time.time()
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=15)
        models = client.models.list()
        elapsed = time.time() - t0
        # Just consume a few to verify
        count = sum(1 for _ in zip(models, range(5)))
        return CheckResult("openai", True, f"OK ({elapsed:.1f}s)", elapsed)
    except Exception as e:
        return CheckResult("openai", False, f"{type(e).__name__}: {e}", time.time() - t0)


def _check_gemini() -> CheckResult:
    """Validate Google/Gemini API key with a minimal call."""
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        return CheckResult("gemini", False, "GOOGLE_API_KEY not set", 0)
    t0 = time.time()
    try:
        from google import genai
        client = genai.Client(api_key=key)
        # List models is lightweight and confirms auth
        models = client.models.list()
        count = sum(1 for _ in zip(models, range(5)))
        elapsed = time.time() - t0
        return CheckResult("gemini", True, f"OK ({elapsed:.1f}s)", elapsed)
    except Exception as e:
        return CheckResult("gemini", False, f"{type(e).__name__}: {e}", time.time() - t0)


def run_health_checks() -> list[CheckResult]:
    """Run all provider health checks and print results."""
    checks = [_check_grok, _check_openai, _check_gemini]
    results: list[CheckResult] = []

    print("API health checks:")
    for check_fn in checks:
        result = check_fn()
        results.append(result)
        status = "OK" if result.ok else "FAIL"
        print(f"  [{status}] {result.provider}: {result.message}")

    failed = [r for r in results if not r.ok]
    if failed:
        names = ", ".join(r.provider for r in failed)
        log.warning("%d provider(s) unavailable: %s", len(failed), names)
        log.warning("Pipeline will degrade gracefully for unavailable providers.")
    else:
        print("  All providers OK.")

    return results
