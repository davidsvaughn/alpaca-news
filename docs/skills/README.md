# Skills: Agent Knowledge Base

This folder contains **operational knowledge for AI agents** working on this codebase. Each file covers a specific domain and is designed to help an agent quickly find information, diagnose problems, and avoid common mistakes.

## How to use these files

**Before investigating an issue**, check the relevant skill file first. It will tell you where to look, what queries to run, and what pitfalls to avoid.

**Before writing code** in an unfamiliar area, read the relevant skill file to understand the architecture, key files, and conventions.

## File index

| File | Domain | Summary |
|------|--------|---------|
| [DIAGNOSTICS.md](DIAGNOSTICS.md) | Debugging & logs | Where to find errors, log sources, key SQL queries, common gotchas |
| [ALPACA.md](ALPACA.md) | Alpaca broker | Order lifecycle, common failures, reconciliation, transaction log |

### Planned files

These don't exist yet. Create them when the knowledge is needed:

| File | Domain | Would cover |
|------|--------|-------------|
| `PIPELINE.md` | Agent pipeline | Orchestrator, runners (Grok/OpenAI/Gemini), tool registry, snapshots |
| `LIVE-TRADING.md` | Live monitor | Exit strategies, portfolio manager, watch lifecycle, cooling/sealing |
| `DATA-SOURCES.md` | Market data | Schwab, yfinance, Alpaca data API, bars, volume delta |
| `WEB-API.md` | Dashboard & API | Routes, templates, SSE events, activity tracker |
| `DATABASE.md` | SQLite schema | Tables, key queries, JSON structures inside columns |

## Conventions

### Structure

Each skill file should follow this pattern:

```markdown
# Title

> One-line summary of what this file helps with.

## Quick reference
(Cheat sheet: most-used commands, queries, file paths)

## Detailed sections
(Organized by topic, not chronologically)

## Gotchas
(Mistakes an agent is likely to make — learned from real incidents)

## Cross-references
(Links to related skill files and project docs)
```

### Cross-linking

**Always cross-link** between files. Links make the knowledge graph navigable.

- **Within skills/**: `[DIAGNOSTICS.md](DIAGNOSTICS.md)` or `[DIAGNOSTICS.md > Gotchas](DIAGNOSTICS.md#gotchas)`
- **To project docs**: `[ALPACA-TRADING.md](../ALPACA-TRADING.md)` (relative from `docs/skills/`)
- **To source code**: `[alpaca_broker.py](../../trader/market/alpaca_broker.py)` (relative from `docs/skills/`)
- **Section anchors**: Use `#section-name` (lowercase, hyphens) for deep links

When adding info to one file that's relevant to another, add a link in both directions.

### Adding a new file

1. Create the `.md` file in this directory
2. Add it to the **File index** table in this README
3. Add **Cross-references** section linking to related skill files
4. Add links FROM related skill files back to the new one
5. Keep it concise — an agent should be able to scan the file in one read

### Writing style

- **Lead with the answer**, not the explanation. Put the most useful info first.
- **Use tables and code blocks** for quick scanning. Avoid long prose.
- **Include real examples** — actual queries, actual log lines, actual file paths.
- **Document mistakes** in the Gotchas section. Every debugging dead-end is valuable.
- **Keep it current** — if you discover something is wrong, fix it immediately.
