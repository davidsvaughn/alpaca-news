# Plan: Add News Source Filter (alpaca_news vs insight_sentry_news)

## Context

Snapshots already store their source in the `trigger_type` column (`"alpaca_news"` or `"insight_sentry_news"`) and in `$.trigger.type` in the JSON. But there's no way to filter by source when querying — not in the DB layer, the backtest API, or the dashboard UI. The user wants to backtest using just one source or the other.

## Changes

### 1. DB layer — `trader/db/database.py`

Add `news_source: str | None = None` param to these 3 functions:

- **`_add_common_snapshot_clauses()`** (line ~1082): new param, add clause `trigger_type = :news_source`
- **`get_all_snapshots()`** (line ~1116): accept + pass through
- **`count_snapshots()`** (line ~1142): accept + pass through

### 2. Shared helper — `trader/web/app.py :: _snapshot_rows_for_filters()` (line ~419)

- Add `news_source: str | None = None` param
- Pass to both `count_snapshots()` and `get_all_snapshots()` calls

### 3. Snapshots page endpoint — `trader/web/app.py :: snapshots_page()` (line ~608)

- Add `news_source: str | None = None` query param
- Pass to `count_snapshots()` call
- Add `"news_source_filter": news_source or ""` to template context

### 4. API snapshots endpoint — `trader/web/app.py :: api_snapshots()` (line ~861)

- Add `news_source: str | None = None` query param
- Pass to `_snapshot_rows_for_filters()`
- Add to `_build_query_suffix()` dict and template context

### 5. Backtest API endpoint — `trader/web/app.py :: api_backtest()` (line ~1050)

- Extract `news_source` from `filters` dict (line ~1089–1107)
- Pass to `_snapshot_rows_for_filters()` call

### 6. Dashboard UI — filter row in `trader/web/templates/partials/_snapshots_table.html`

Add a `<select>` dropdown in the filter row (after the date filters, before headline — or in the empty checkbox `<th>`):

```html
<th>
  <div class="fcw">
    <select class="form-control form-control-sm srv-filter" data-key="news_source" style="font-size:.7rem;">
      <option value="">All</option>
      <option value="alpaca_news" {% if news_source_filter == 'alpaca_news' %}selected{% endif %}>Alpaca</option>
      <option value="insight_sentry_news" {% if news_source_filter == 'insight_sentry_news' %}selected{% endif %}>Sentry</option>
    </select>
    <span class="fx" onclick="clearFilter(this)">&times;</span>
  </div>
</th>
```

### 7. JS — `trader/web/templates/snapshots.html`

- Add `'news_source'` to the `_SRV_KEYS` array (line ~559)
- Add `news_source` to `_collectBacktestFilters()` (already covered — it iterates `_SRV_KEYS`)

### 8. Live config creation — strip `news_source` from live filters

In the "Go Live" JS handler (~line 1340), `news_source` should **not** be stripped — it's a meaningful filter for live configs too (only filter snapshots from the desired source).

## Files to Modify

| File | Lines | Change |
|------|-------|--------|
| `trader/db/database.py` | ~1082, ~1116, ~1142 | Add `news_source` param to 3 functions |
| `trader/web/app.py` | ~419, ~608, ~631, ~861, ~884, ~908, ~1089 | Thread `news_source` through endpoints |
| `trader/web/templates/partials/_snapshots_table.html` | ~27–55 | Add source dropdown in filter row |
| `trader/web/templates/snapshots.html` | ~559 | Add `'news_source'` to `_SRV_KEYS` |

## Verification

1. `uv run python -m pytest tests/ -v -s` — existing tests pass
2. Manual DB check:
   ```python
   from trader.db.database import count_snapshots, open_sqlite
   from trader.config import load_settings
   db = open_sqlite(load_settings().sqlite_path)
   print("alpaca:", count_snapshots(db, news_source="alpaca_news"))
   print("sentry:", count_snapshots(db, news_source="insight_sentry_news"))
   print("all:", count_snapshots(db))
   ```
3. Dashboard: open `/snapshots`, use the new Source dropdown to filter, confirm counts change
4. Backtest: run a backtest with source filter selected, confirm it only uses matching snapshots
