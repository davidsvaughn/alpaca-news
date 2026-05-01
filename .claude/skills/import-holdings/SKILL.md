---
name: import-holdings
description: Import or refresh a read-only "tracking" portfolio that mirrors externally-held holdings (e.g. Vanguard IRA) from a screenshot. Use when the user provides a brokerage holdings screenshot and wants the trader app to track those positions, or wants to refresh an existing tracking portfolio with updated quantities. Triggers on "import holdings", "update tracking portfolio", "mirror my <brokerage> holdings", or pointing at a holdings image.
---

# Import / refresh a tracking portfolio

Tracking portfolios mirror externally-held positions read-only. They cannot
auto-buy (news pipeline gate in `LivePortfolioManager.evaluate_snapshot`) and
cannot auto-sell (exit monitor gate in `LiveExitMonitor.run_cycle`).

**Existing portfolio** at the time this skill was written: `Vanguard IRA`
(`lc_249f3f155c57`) — verify with the SQL query below before assuming.

## Workflow

### 1. Identify intent: new vs refresh

Ask the user *before* running anything:

- **New portfolio** (different brokerage / account): create fresh.
- **Refresh existing** (new screenshot of the same account): drop the old
  config + its watches first, then re-import. The script does not yet have
  an `--update` flag.

If they say "refresh" / "update" / mention an existing portfolio, look up the
target config:

```bash
uv run python -c "
from trader.config import load_settings
from trader.db.database import get_all_live_configs, open_sqlite
db = open_sqlite(load_settings().sqlite_path)
for c in get_all_live_configs(db):
    if c.get('mode') == 'tracking':
        print(c['config_id'], '|', c['name'], '| watches via SQL below')
"
```

### 2. Dry-run first — always

```bash
uv run python scripts/import_holdings.py <image_path> --name "<Portfolio Name>" --dry-run
```

This calls `gemini-3-flash-preview` to OCR the screenshot, then fetches fresh
Schwab quotes. The output table is the user's source of truth — spot-check
quantities against the screenshot and confirm with the user before persisting.

If any quote is missing the script will warn; the real run aborts loudly
rather than partial-writing.

### 3. For a refresh: delete the old portfolio first

Order matters — delete watches before the config so nothing references a
ghost config_id:

```bash
uv run python -c "
import sys
from sqlalchemy import text
from trader.config import load_settings
from trader.db.database import delete_live_config, open_sqlite
CFG = sys.argv[1]
db = open_sqlite(load_settings().sqlite_path)
with db.engine.begin() as conn:
    n = conn.execute(text('DELETE FROM watches WHERE entry_snapshot_id = :s'),
                     {'s': f'manual_import_{CFG}'}).rowcount
print(f'deleted {n} watches')
print('config deleted:', delete_live_config(db, CFG))
" <old_config_id>
```

Confirm the deletion count matches what was there before continuing.

### 4. Real import

```bash
uv run python scripts/import_holdings.py <image_path> --name "<Portfolio Name>" --yes
```

`--yes` skips the interactive prompt; the user already confirmed in step 2.

### 5. Verify

```bash
uv run python -c "
import sys
from sqlalchemy import text
from trader.config import load_settings
from trader.db.database import get_live_config, open_sqlite
CFG = sys.argv[1]
db = open_sqlite(load_settings().sqlite_path)
cfg = get_live_config(db, CFG)
print('cfg:', cfg['name'], '| mode:', cfg.get('mode'), '| paused:', cfg.get('paused'),
      '| alpaca:', cfg.get('alpaca_account_id'), '| active:', cfg.get('active'))
with db.engine.connect() as conn:
    rows = conn.execute(text(\"SELECT symbol, status FROM watches WHERE entry_snapshot_id = :s ORDER BY symbol\"),
                        {'s': f'manual_import_{CFG}'}).fetchall()
print(f'{len(rows)} watches:', [r[0] for r in rows])
" <new_config_id>
```

Required state: `mode=tracking`, `paused=True`, `alpaca=None`, `active=True`,
one watch per holding all in `holding` status.

## Things to verify by reading code, not assuming

The on-disk app changes faster than docs. Before relying on details:

- Mode field + gates: `trader/models/live_config.py` (`mode` field) and
  `trader/online/live_monitor.py` (search `mode == "tracking"` and
  `tracking_config_ids`). If those gates moved or were renamed, the
  read-only invariant could be broken.
- Manual watch builder: `WatchBuilder.create_from_manual_holding` in
  `trader/models/watch.py`. The `entry_snapshot_id` it sets is
  `manual_import_<config_id>` — that's the magic key the verify and refresh
  queries above join on. If the prefix changes, fix the queries.
- Script entrypoint: `scripts/import_holdings.py`. CLI args, env vars
  (`GOOGLE_API_KEY`, `HOLDINGS_IMPORT_MODEL`).

## Future work the user mentioned

- **Per-stock alerts** (price thresholds, % drops) on tracking watches with
  email/SMS delivery. Hook would be a new monitor loop or an extension to
  `LiveExitMonitor` that, for tracking-mode configs, evaluates alert rules
  instead of exit strategies. Don't build until asked — flag it as the
  natural next step if the user starts asking about price-based notifications.
- **`--update` flag** on the script so refresh becomes one command instead of
  the delete-then-import dance above. Worth doing on the second or third
  refresh, not the first.
