# trader/ (Phase 1)

This directory contains the first working slice of the system described in
`cline/DESIGN_PLAN.md`.

## What works now

- Watches `output/alpaca/*.json` for new news items
- Runs **triage** (LLM) → optional **explore phase 1** (LLM w/ web search tools)
- Seals a **Snapshot** JSON artifact under `data/snapshots/`
- Persists Snapshot metadata+JSON into `data/trader.db` (SQLite)
- Serves a minimal **FastAPI dashboard** with **SSE** at `http://127.0.0.1:8000/`

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

## Real LLM keys

Set one or more of:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`

Then set providers/models in `.env` (see `.env.example`).

## Notes

- Phase 1 uses SQLite for speed. Postgres JSONB is planned next.
- The current explorer is a *minimal* 1-hop evidence gatherer to establish the
  Snapshot/tool-trace data model and persistence.
