# trader/ (Phase 2)

This directory contains the first working slice of the system described in
`cline/DESIGN_PLAN.md`.

## What works now

- Watches `output/alpaca/*.json` for new news items (via watchdog + worker queue)
- Runs **keyword pre-filter** → **LLM triage** → **two-phase exploration** (Stage 2)
  - **Phase 1**: broad, cheap, shallow evidence gathering → generates competing hypotheses
  - **Phase 2**: selective deepening → confirms/refutes top-K hypotheses with gated follow-ups
- Seals an immutable **Snapshot** JSON artifact under `data/snapshots/`
- Persists Snapshot metadata+JSON into `data/trader.db` (SQLite)
- Captures **Schwab market context** + **price context** (quotes + recent candles; optional stream)
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
- **Finite action menu** for exploration (learnable, policy-friendly)
- **First-class stop reasons** recorded in tool traces (STOP_CONFIRMED / STOP_LOW_SIGNAL / STOP_BUDGET / STOP_REDUNDANT)
- **SchwabMarketClient** wrapper provides candle/quote/stream context for snapshots

## Quick start (no API keys)

Run end-to-end with the mock LLM:

```bash
cp .env.example .env
# ensure MOCK_LLM=true in .env
# optional but recommended if you don't want to use Schwab market data yet:
#   SCHWAB_DISABLED=true

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

## Schwab market data

By default the orchestrator will attempt to initialize Schwab for context capture.

Options:

- Disable entirely (recommended for local mock runs):

```bash
SCHWAB_DISABLED=true
```

- Enable by setting credentials:

```bash
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
```

## Key modules

| Module | Purpose |
|---|---|
| `models/snapshot.py` | Snapshot + SnapshotBuilder + deterministic IDs |
| `models/tool_trace.py` | ToolTrace schema helpers |
| `models/actions.py` | Finite action menu + stop reasons + hypothesis schema |
| `online/orchestrator.py` | Watchdog → queue → worker → seal pipeline |
| `online/triage.py` | Pre-filter + LLM triage (Stage 1) |
| `online/explorer.py` | Explorer v1: two-phase exploration + hypothesis ranking (Stage 2) |
| `llm/client.py` | Unified OpenAI / Gemini / Grok client |
| `llm/cost_tracker.py` | Per-call + per-tool cost tracking, budget enforcement |
| `llm/pricing.py` | Pricing tables (single source of truth) |
| `llm/extract.py` | Robust JSON extraction from LLM output |
| `llm/mock.py` | Mock LLM for no-key testing |
| `knowledge/store.py` | Knowledge JSON file manager (skip patterns, etc.) |
| `db/database.py` | SQLite persistence (idempotent inserts) |
| `market/schwab_client.py` | Schwab wrapper for quotes, candles, and streaming |
| `web/app.py` | FastAPI dashboard |
| `web/sse.py` | Server-Sent Events for live streaming |

## Notes

- Phase 1 uses SQLite for speed. Postgres JSONB is planned later.
- Explorer v1 focuses on *capturing evidence + traces* rather than perfect trading decisions.
- Pre-filter catches obvious fluff (e.g. "if you had invested 5 years ago…")
  without an LLM call; learned skip keywords are also checked locally.

## Explorer knobs (Phase 2)

Key env vars (see `.env.example`):

- `MAX_PHASE1_ACTIONS` (default 4)
- `MAX_PHASE2_BRANCHES` (default 2)
- `MAX_TOTAL_HOPS` (default 3)
- `MAX_COST_PER_NEWS_ITEM`
