# Live Config Patching: Modify Portfolio Parameters While Live

> How to change filters, allocation, exit strategy, or other parameters on active live configs without restarting.

## Quick reference

| Task | How |
|------|-----|
| List active configs | `SELECT config_id, name, config_json FROM live_configs WHERE active = 1` |
| Patch a single filter | Update `config_json` in SQLite (see recipes below) |
| Patch via API | `POST /api/live/config` with `config_id` + changed fields |
| Verify change took | Re-query the DB or `GET /api/live/config` |

**Key files:**

| File | What it does |
|------|--------------|
| [live_config.py](../../trader/models/live_config.py) | `LiveConfig` dataclass, `to_dict()` / `from_dict()` |
| [database.py](../../trader/db/database.py) | `get_live_config()`, `update_live_config()` CRUD |
| [live_monitor.py](../../trader/online/live_monitor.py) | `_evaluate_snapshot_in_config()` reads filters each cycle |
| [app.py](../../trader/web/app.py) | `POST /api/live/config` update endpoint |

## Why this works without a restart

The live monitor re-reads the config from SQLite **every time** it evaluates a snapshot (see `_evaluate_snapshot_in_config()` in [live_monitor.py](../../trader/online/live_monitor.py)). There is no in-memory cache to invalidate. Update the DB row and the next evaluation cycle picks up the change.

## Recipes

### Patch a filter on all active configs (Python one-liner)

Set `conf_min` to 75% on all active configs:

```bash
uv run python -c "
import sqlite3, json
db = sqlite3.connect('data/trader.db')
db.row_factory = sqlite3.Row
rows = db.execute('SELECT id, config_id, name, config_json FROM live_configs WHERE active = 1').fetchall()
for r in rows:
    cfg = json.loads(r['config_json'])
    cfg.setdefault('filters', {})['conf_min'] = '75'
    db.execute('UPDATE live_configs SET config_json = ?, updated_at = datetime(\"now\") WHERE id = ?', (json.dumps(cfg), r['id']))
    print(f'  {r[\"config_id\"]} ({r[\"name\"]}): conf_min -> 75')
db.commit()
db.close()
"
```

To change a different filter, replace `'conf_min'` and `'75'` with the desired key/value. See **Filter keys** below.

### Patch a single config via the API

```bash
# Fetch current config
curl -s http://localhost:8000/api/live/config | jq '.[] | select(.config_id=="lc_XXXX")'

# Update — NOTE: sending "filters" replaces the ENTIRE filters dict
curl -X POST http://localhost:8000/api/live/config \
  -H 'Content-Type: application/json' \
  -d '{"config_id": "lc_XXXX", "filters": { "conf_min": "75", "price_min": "5", "avg_vol_min": "1", "mkt_cap_min": "2" }}'
```

**Warning:** The API does a shallow `dict.update()` — sending `"filters": {"conf_min": "75"}` replaces the entire filters dict, losing other filter values. Always include all filter keys you want to keep.

### Patch non-filter parameters

The same pattern works for any `config_json` field — allocation, exit strategy, guards, etc.:

```python
cfg['allocation'] = 'equal_weight'
cfg['exit_params']['stop_loss'] = 0.03
cfg['guards']['max_positions'] = 10
```

## Filter keys

All values are **strings** (even numeric ones). Empty string `""` means disabled.

| Key | Type | Example | Meaning |
|-----|------|---------|---------|
| `conf_min` | percentage string | `"75"` | Min confidence (75% = 0.75). Bearish signals are negated before comparison. |
| `symbol` | string | `"AAPL"` | Exact match, case-insensitive. Empty = all symbols. |
| `price_min` / `price_max` | dollar string | `"5"` / `"500"` | Stock price range |
| `avg_vol_min` / `avg_vol_max` | millions string | `"1"` / `"50"` | Average volume in millions |
| `mkt_cap_min` / `mkt_cap_max` | billions string | `"2"` / `"1000"` | Market cap in billions |
| `pe_min` / `pe_max` | ratio string | `"5"` / `"50"` | P/E ratio range |
| `created_after` | ISO date string | `"2026-03-10"` | Only snapshots created after this date |

## Gotchas

- **All filter values are strings.** Write `"75"` not `75`. The live monitor does `float(val) / 100.0` — a bare int in JSON will still work but breaks the convention.
- **API replaces entire sub-dicts.** If you POST `{"filters": {"conf_min": "80"}}`, all other filters are wiped. Use the direct DB approach or include all filter keys in the API call.
- **Empty string = disabled.** `"conf_min": ""` means no confidence filter. This is distinct from missing the key entirely (same effect, but existing configs have all keys present with `""` defaults).
- **DB path:** `data/trader.db` (relative to project root). Not `data/db/alpaca_news.db`.

## Cross-references

- [ALPACA.md](ALPACA.md) — Alpaca order lifecycle, buy/sell flows
- [BACKTEST-LIVE-PIPELINE.md](BACKTEST-LIVE-PIPELINE.md) — Adding new params to the backtest-to-live flow
- [DIAGNOSTICS.md](DIAGNOSTICS.md) — Debugging queries and log sources
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading plan and implementation details
