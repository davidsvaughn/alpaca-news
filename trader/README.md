# trader/

This directory contains the first working slice of the system described in
`docs/ARCHITECTURE.md`.

It implements the **online (real-time) loop**:

> new Alpaca news JSON file → triage → two-phase exploration → seal an immutable Snapshot

The long-term goal is that these Snapshots become the *atomic learning artifact*
for offline labeling + policy learning.

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

## New capabilities (added)

### 1) X API v2 integration (optional)

New module: `trader/xapi/`.

Implements (Bearer token):
- Filtered stream rules: `GET/POST /2/tweets/search/stream/rules`
- Stream consumer: `GET /2/tweets/search/stream` (reconnect/backoff)
- Usage polling: `GET /2/usage/tweets`

There is also a conservative **BURST-first** background service:
- `trader/online/x_stream_service.py`

It is gated by env vars and defaults to disabled.

**Quick smoke test (safe):**
```bash
uv run python -c "from trader.xapi.client import XApiClient; from trader.xapi.usage import get_usage; print(get_usage(client=XApiClient(), days=7))"
```

### 2) Evidence acquisition layer (optional)

New module: `trader/evidence/`.

Implements the scout→acquire pattern:
- exploration traces provide candidate URLs
- the system fetches and extracts article text itself
- persists immutable evidence docs under `data/evidence/*.json`

Extractor defaults to **Trafilatura**, but is configurable.

**Smoke test:**
```bash
uv run python -m trader.evidence.smoke_test --url https://example.com
```

## Phase 1 vs Phase 2 (important clarification)

There are **two different “phase” concepts** used across the repo:

1. **Project/roadmap phases** in `docs/ARCHITECTURE.md` (Phase 1/2/3/...) — milestones for
   building the system.
2. **Explorer “two-phase exploration”** inside Stage 2 (**Phase 1** and **Phase 2**) — the
   runtime behavior that happens *per news event*.

When this README says **Phase 1/Phase 2** below, it is referring to **(2)**: the explorer
runtime exploration phases.

### Where the explorer phases run in the pipeline

The runtime path is:

1. `trader/online/orchestrator.py::run_watch_loop()` watches `output/alpaca/*.json`
   (watchdog → queue → worker thread).
2. The worker calls `trader/online/orchestrator.py::process_news_file(...)`.
3. Stage 1 triage runs: `trader/online/triage.py::run_triage(...)`.
4. If triage returns `action == "investigate"`, Stage 2 runs:
   `trader/online/explorer.py::explore_two_phase(...)`.
5. A `SnapshotBuilder` accumulates traces/context and then `.seal()` produces a frozen Snapshot
   which is persisted to both JSON and SQLite.

### Phase 1 (exploration): broad / cheap / shallow

**Intent:** quickly gather a small amount of evidence and generate **2–5 competing hypotheses**.
You are looking for *disagreement* (different plausible narratives), not volume.

Implemented in `trader/online/explorer.py::explore_two_phase(...)` as:

- Pick a small set of Phase 1 actions via `_choose_phase1_actions(...)`.
  The candidates come from `trader/models/actions.py::PHASE1_ACTIONS` (examples:)
  - `price_spike_check`, `volume_regime_shift` (market-only, if Schwab is available)
  - `breaking_followup` (web)
  - `google_broad_search` (Gemini grounding)
  - `x_realtime_rumor` (Grok x_search)
- Execute each selected action and record a **ToolTrace** hop (`trader/models/tool_trace.py`).
- Deduplicate evidence and then ask the LLM to propose hypotheses using:
  - prompt: `trader/prompts/explore_phase1.md`
  - output schema: `Hypothesis` (`trader/models/actions.py::Hypothesis`)

**Outputs of Phase 1:**
- Tool traces for the Phase 1 actions
- A hypothesis set (each with confidence + suggested follow-up actions)
- A “fresh vs stale” assessment for the trigger news

### Phase 2 (exploration): selective deepening (gated follow-ups)

**Intent:** keep only the best hypotheses (top-K) and do **one targeted follow-up per hypothesis**,
using different tools where possible to avoid redundant evidence.

Implemented in `trader/online/explorer.py::explore_two_phase(...)` as:

1. **Rank/select hypotheses and assign actions** (top-K = `MAX_PHASE2_BRANCHES`)
   - prompt: `trader/prompts/hypothesis_rank.md`
   - outputs: a list of `{hypothesis_id, assigned_action_id}`
2. **Execute follow-ups**
   - follow-up prompt: `trader/prompts/explore_phase2.md`
   - action templates come from `trader/models/actions.py::PHASE2_ACTIONS`
     (examples: `news_confirmation`, `filing_check`, `analyst_reaction`, `x_volume_alerts`, ...)
   - basic orthogonality: the explorer tries not to reuse the same tool for multiple branches
3. **Stop is explicit and logged**
   - Phase 2 returns a `stop_signal` with a constrained reason:
     `STOP_CONFIRMED | STOP_LOW_SIGNAL | STOP_BUDGET | STOP_REDUNDANT`
   - enforced via `trader/models/actions.py::StopReason`

**Outputs of Phase 2:**
- Tool traces for the follow-up hops
- Extracted signals (sentiment/novelty/confirmation strength)
- Stop reason (why we quit)

### Are Phase 1 and Phase 2 Run “real-time” (i.e. NOT after market close)?

**In the current architecture and code, BOTH Phase 1 and Phase 2 are part of the online (real-time)
pipeline and run immediately per incoming news item.**

The thing that is intended to run “after-hours” (offline/async) is **not** Phase 2 exploration.
It is the separate offline loop described in `docs/ARCHITECTURE.md`:

- outcome labeling (+15m/+60m/+1d returns)
- scoring hop/tool value
- learning/updating action weights, stop thresholds, allow/deny lists

(Those live conceptually under `trader/offline/` in the design plan; not implemented in this slice.)

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

## Backfill (process existing Alpaca files)

**Backfill** means: run the *same online pipeline* (triage → explore → seal Snapshot) over
**already-existing** `output/alpaca/*.json` files.

It’s useful for:

- Bootstrapping a dataset of Snapshots without waiting for live news
- Regression testing pipeline changes against a fixed set of articles
- Re-processing after you change prompts/models (while still avoiding duplicates)
- Catching up if the watcher was not running

Backfill does **not** mean “after-hours Phase 2”. It’s simply a batch driver that feeds
historical files through the *same* Stage 1 + Stage 2 code paths.

### How backfill fits into idempotent storage

This pipeline is designed so backfill can be safely re-run:

- Snapshot IDs are deterministic (derived from Alpaca article id) via
  `trader/models/snapshot.py::deterministic_snapshot_id(...)`.
- Before processing a file, the orchestrator/backfill checks
  `trader/db/database.py::snapshot_exists(...)`.
- Inserts are idempotent (DB insert is effectively “insert or ignore”).

Net effect: you can run backfill multiple times and it will skip files that already produced a
Snapshot.

### Running backfill

```bash
MOCK_LLM=true uv run python -m trader.online.backfill --limit 10
```

Re-running backfill on the same files is safe — duplicates are skipped via
deterministic snapshot IDs derived from the Alpaca article ID.

Tip: backfill is often easiest with `MOCK_LLM=true` and `SCHWAB_DISABLED=true` if you’re iterating
on orchestration logic locally.

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
- `TRIAGE_SYMBOL_COOLDOWN_MINUTES` (default 60)
