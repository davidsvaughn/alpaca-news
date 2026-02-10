# trader/ (Phase 1)

This directory contains the first working slice of the system described in
`cline/DESIGN_PLAN.md`.

## What works now

- Watches `output/alpaca/*.json` for new news items (via watchdog + worker queue)
- Runs **keyword pre-filter** → **LLM triage** → optional **explore phase 1** (LLM w/ web search tools)
- Seals an immutable **Snapshot** JSON artifact under `data/snapshots/`
- Persists Snapshot metadata+JSON into `data/trader.db` (SQLite)
- Serves a minimal **FastAPI dashboard** with **SSE** at `http://127.0.0.1:8000/`
- **Deterministic snapshot IDs** — backfill is idempotent (safe to re-run)
- **Per-tool cost tracking** in both CostTracker and sealed Snapshots
- **Pre-filter** catches obvious fluff headlines without an LLM call (saves money)

## Architecture highlights

- **SnapshotBuilder** pattern: mutable accumulator during pipeline → frozen `Snapshot` on `.seal()`
- **Queue-decoupled processing**: watchdog enqueues file paths; worker thread processes them sequentially (watchdog never blocks on LLM calls)
- **Robust JSON extraction** from LLM responses: handles code fences, prose wrapping, etc.
- **Triage-refined symbols** passed to explorer so it knows what to focus on
- **Unified LLM client** supports OpenAI, Gemini, and Grok with the same interface

## Quick start (no API keys)

Run end-to-end with the mock LLM:

```bash
cp .env.example .env
# ensure MOCK_LLM=true and BACKFILL_ON_START=true in .env

uv run python -m trader.main
```

Then open:

- http://127.0.0.1:8000/

Snapshots will be written to:

- `data/snapshots/*.json`

## Backfill (process existing files)

```bash
MOCK_LLM=true uv run python -m trader.online.backfill --limit 10
```

Re-running backfill on the same files is safe — duplicates are skipped via
deterministic snapshot IDs derived from the Alpaca article ID.

## Real LLM keys

Set one or more of:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
- `XAI_API_KEY`

Then set providers/models in `.env` (see `.env.example`).

## Key modules

| Module | Purpose |
|---|---|
| `models/snapshot.py` | Snapshot + SnapshotBuilder + deterministic IDs |
| `models/tool_trace.py` | ToolTrace schema helpers |
| `online/orchestrator.py` | Watchdog → queue → worker → seal pipeline |
| `online/triage.py` | Pre-filter + LLM triage (Stage 1) |
| `online/explorer.py` | Phase 1 exploration with web/X search (Stage 2) |
| `llm/client.py` | Unified OpenAI / Gemini / Grok client |
| `llm/cost_tracker.py` | Per-call + per-tool cost tracking, budget enforcement |
| `llm/pricing.py` | Pricing tables (single source of truth) |
| `llm/extract.py` | Robust JSON extraction from LLM output |
| `llm/mock.py` | Mock LLM for no-key testing |
| `knowledge/store.py` | Knowledge JSON file manager (skip patterns, etc.) |
| `db/database.py` | SQLite persistence (idempotent inserts) |
| `web/app.py` | FastAPI dashboard |
| `web/sse.py` | Server-Sent Events for live streaming |

## Notes

- Phase 1 uses SQLite for speed. Postgres JSONB is planned for Phase 2+.
- The current explorer is a *minimal* 1-hop evidence gatherer to establish the
  Snapshot/tool-trace data model and persistence.
- Pre-filter catches obvious fluff (e.g. "if you had invested 5 years ago…")
  without an LLM call; learned skip keywords are also checked locally.
