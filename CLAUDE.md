# Project Guidelines

## Project Documentation

Docs live in `docs/`, organized by topic. Two legacy overview docs plus topic-specific docs:

- **`docs/ARCHITECTURE.md`** — High-level system map: components, data flow, project structure. May lag behind topic docs.
- **Topic docs** (the real living documentation):
  - `docs/LIVE-TRADING.md` — Live trading plan and implementation
  - `docs/ALPACA-TRADING.md` — Alpaca order execution, multi-account, fills
  - `docs/VOLUME-DELTA-REALTIME.md` — Shadow collector, Schwab streaming, tick-level VDD
  - `docs/VDD-COMPARISON.md` — Bar-based vs tick-level VDD comparison experiment
  - `docs/TICK-COLLECTOR.md` — Tick collector service: raw trade data, TimescaleDB, roadmap
  - `docs/BACKTEST-*.md` — Backtest architecture, strategies, metrics
  - `docs/ALLOCATION-STRATEGIES.md` — Portfolio allocation methods

Reference papers are in `docs/refs/`. Archived older docs in `docs/archive/`.

## Documentation Lookup

When working with external libraries, frameworks, or APIs — **always use the Context7 MCP server** (`resolve-library-id` then `query-docs`) to look up current documentation and code examples before writing code or giving advice. Do not rely on training data for API signatures, parameter names, or usage patterns — they may be outdated or wrong.

Specifically:
- **Before using any library API** you haven't already verified in this session, look it up via Context7.
- **When the user asks about a framework** (features, limitations, how something works), query Context7 for authoritative answers rather than guessing.
- **When writing code that uses an external library**, verify the import paths, method signatures, and parameter names against Context7 docs.
- If Context7 doesn't have the library, fall back to `WebSearch` or `WebFetch` — but prefer Context7 first since it returns structured, verified documentation.

# Behavioral guidelines to reduce common LLM coding mistakes

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.