# Trade Performance Deep Dive

> How to systematically analyze trade outcomes, LLM signal quality, and skip pattern effectiveness.

The user periodically requests a full analysis of trading performance. This file documents the exact process, queries, and methodology so future runs are consistent and thorough.

## Quick reference

| What | Where |
|------|-------|
| SQLite DB | `data/trader.db` |
| Snapshot JSONs | `data/snapshots/{snapshot_id}.json` |
| Watch JSONs | `data/watches/{watch_id}.json` |
| Skip patterns | `data/knowledge/skip_patterns.jsonc` |
| Previous analysis | [docs/TRADE-ANALYSIS-DEEP-DIVE.md](../TRADE-ANALYSIS-DEEP-DIVE.md) |
| Key tables | `snapshots`, `watches`, `live_configs`, `alpaca_transactions`, `follow_ups` |

## The Process (Step by Step)

### Phase 1: Quantitative overview

Run these SQL queries against `data/trader.db` to get the statistical baseline. **Always do this first** — it grounds the rest of the analysis in data.

#### 1A. Overall trade statistics
```sql
SELECT count(*) as total,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 0 THEN 1 ELSE 0 END) as winners,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') <= 0 THEN 1 ELSE 0 END) as losers,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),3) as avg_pnl,
  round(sum(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),2) as total_pnl_pct,
  round(avg(json_extract(w.watch_json, '$.peak_pnl_pct')),2) as avg_peak,
  round(avg(json_extract(w.watch_json, '$.trough_pnl_pct')),2) as avg_trough
FROM watches w
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL;
```

#### 1B. P&L by exit reason
```sql
SELECT json_extract(w.watch_json, '$.exit.reason') as exit_reason,
  count(*) as cnt,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),2) as avg_pnl
FROM watches w
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
GROUP BY exit_reason ORDER BY cnt DESC;
```

#### 1C. P&L by prediction confidence
```sql
SELECT round(json_extract(w.watch_json, '$.entry.confidence'),2) as conf,
  count(*) as cnt,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),2) as avg_pnl,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 0 THEN 1 ELSE 0 END) as winners,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') <= 0 THEN 1 ELSE 0 END) as losers
FROM watches w
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
GROUP BY conf ORDER BY conf;
```

#### 1D. P&L by news source
```sql
SELECT COALESCE(json_extract(s.snapshot_json, '$.trigger.source'), 'unknown') as source,
  count(*) as cnt,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),2) as avg_pnl,
  round(100.0 * sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 0 THEN 1 ELSE 0 END) / count(*), 1) as win_rate
FROM watches w
JOIN snapshots s ON json_extract(w.watch_json, '$.entry.snapshot_id') = s.snapshot_id
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
GROUP BY source HAVING cnt >= 3 ORDER BY cnt DESC;
```

#### 1E. P&L by catalyst type (headline keywords)
```sql
SELECT
  CASE
    WHEN json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%report%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%earnings%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%quarter%' THEN 'earnings'
    WHEN json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%upgrade%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%price target%' THEN 'analyst'
    WHEN json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%acqui%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%merge%' THEN 'deal'
    WHEN json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%FDA%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%trial%' THEN 'clinical'
    WHEN json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%contract%' OR
         json_extract(s.snapshot_json, '$.trigger.headline') LIKE '%order%' THEN 'business_win'
    ELSE 'other'
  END as catalyst_type,
  count(*) as cnt,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')),2) as avg_pnl,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 0 THEN 1 ELSE 0 END) as winners,
  sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') <= 0 THEN 1 ELSE 0 END) as losers
FROM watches w
JOIN snapshots s ON json_extract(w.watch_json, '$.entry.snapshot_id') = s.snapshot_id
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
GROUP BY catalyst_type ORDER BY cnt DESC;
```

#### 1F. P&L by key_catalyst keywords
```sql
SELECT
  CASE
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%short%squeeze%' THEN 'short_squeeze'
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%buyback%' THEN 'buyback'
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%eps%beat%' OR
         lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%earnings%beat%' THEN 'earnings_beat'
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%upgrade%' THEN 'upgrade'
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%guidance%' THEN 'guidance'
    WHEN lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%oversold%' OR
         lower(json_extract(s.snapshot_json, '$.prediction.key_catalyst')) LIKE '%rsi%' THEN 'technical'
    ELSE 'other'
  END as keyword,
  count(*) as cnt,
  round(avg(json_extract(w.watch_json, '$.exit.realized_pnl_pct')), 2) as avg_pnl,
  round(100.0 * sum(CASE WHEN json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 0 THEN 1 ELSE 0 END) / count(*), 1) as win_rate
FROM watches w
JOIN snapshots s ON json_extract(w.watch_json, '$.entry.snapshot_id') = s.snapshot_id
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
GROUP BY keyword HAVING cnt >= 3 ORDER BY avg_pnl DESC;
```

#### 1G. Hold time analysis
```sql
SELECT
  CASE
    WHEN hold_minutes < 30 THEN '<30min'
    WHEN hold_minutes < 60 THEN '30-60min'
    WHEN hold_minutes < 120 THEN '1-2hr'
    WHEN hold_minutes < 240 THEN '2-4hr'
    WHEN hold_minutes < 480 THEN '4-8hr'
    ELSE '8hr+'
  END as hold_time,
  count(*) as cnt,
  round(avg(pnl),2) as avg_pnl,
  sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winners,
  sum(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losers
FROM (
  SELECT
    (julianday(json_extract(w.watch_json, '$.exit.time')) - julianday(json_extract(w.watch_json, '$.entry.time'))) * 24 * 60 as hold_minutes,
    json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl
  FROM watches w
  WHERE w.status IN ('sealed', 'cooling_off')
    AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
) GROUP BY hold_time ORDER BY hold_minutes;
```

#### 1H. Time of day analysis
```sql
SELECT
  CASE
    WHEN entry_hour < 10 THEN 'pre-market'
    WHEN entry_hour < 12 THEN 'morning'
    WHEN entry_hour < 14 THEN 'early_afternoon'
    WHEN entry_hour < 16 THEN 'late_afternoon'
    ELSE 'after_hours'
  END as session,
  count(*) as cnt,
  round(avg(pnl),2) as avg_pnl,
  round(100.0 * sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / count(*), 1) as win_rate
FROM (
  SELECT
    CAST(strftime('%H', json_extract(w.watch_json, '$.entry.time'), '-5 hours') AS INTEGER) as entry_hour,
    json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl
  FROM watches w
  WHERE w.status IN ('sealed', 'cooling_off')
    AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
) GROUP BY session ORDER BY entry_hour;
```

#### 1I. Price range analysis
```sql
SELECT
  CASE
    WHEN entry_price < 5 THEN '<$5'
    WHEN entry_price < 20 THEN '$5-20'
    WHEN entry_price < 50 THEN '$20-50'
    WHEN entry_price < 100 THEN '$50-100'
    ELSE '$100+'
  END as price_range,
  count(*) as cnt,
  round(avg(pnl),2) as avg_pnl,
  round(100.0 * sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / count(*), 1) as win_rate
FROM (
  SELECT json_extract(w.watch_json, '$.entry.price') as entry_price,
    json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl
  FROM watches w
  WHERE w.status IN ('sealed', 'cooling_off')
    AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
) GROUP BY price_range ORDER BY entry_price;
```

#### 1J. Repeat symbol performance
```sql
SELECT symbol, count(*) as trades,
  round(avg(pnl),2) as avg_pnl,
  round(sum(pnl),2) as total_pnl,
  sum(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
FROM (
  SELECT w.symbol,
    json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl
  FROM watches w
  WHERE w.status IN ('sealed', 'cooling_off')
    AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
) GROUP BY symbol HAVING trades >= 3 ORDER BY total_pnl DESC LIMIT 30;
```

#### 1K. Trades that reversed from profitable peak
```sql
SELECT w.symbol,
  round(json_extract(w.watch_json, '$.peak_pnl_pct'),2) as peak,
  round(json_extract(w.watch_json, '$.exit.realized_pnl_pct'),2) as final_pnl,
  json_extract(w.watch_json, '$.exit.reason') as exit_reason
FROM watches w
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
  AND json_extract(w.watch_json, '$.peak_pnl_pct') > 5
  AND json_extract(w.watch_json, '$.exit.realized_pnl_pct') < 0
ORDER BY json_extract(w.watch_json, '$.peak_pnl_pct') DESC;
```

#### 1L. Magnitude prediction accuracy
```sql
SELECT
  CASE
    WHEN peak >= mag_low THEN 'reached_target'
    WHEN peak >= mag_low * 0.5 THEN 'reached_half'
    ELSE 'missed'
  END as target_hit,
  count(*) as cnt,
  round(avg(pnl), 2) as avg_pnl
FROM (
  SELECT json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl,
    json_extract(w.watch_json, '$.peak_pnl_pct') as peak,
    CAST(substr(json_extract(s.snapshot_json, '$.prediction.magnitude_estimate'), 1,
      instr(json_extract(s.snapshot_json, '$.prediction.magnitude_estimate'), '-') - 1) AS REAL) as mag_low
  FROM watches w
  JOIN snapshots s ON json_extract(w.watch_json, '$.entry.snapshot_id') = s.snapshot_id
  WHERE w.status IN ('sealed', 'cooling_off')
    AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
    AND json_extract(s.snapshot_json, '$.prediction.magnitude_estimate') LIKE '%-%'
) GROUP BY target_hit ORDER BY cnt DESC;
```

### Phase 2: Snapshot deep dive (50-100 snapshots)

Read the actual snapshot JSON files to understand LLM reasoning quality.

#### 2A. Identify best and worst trades
```sql
-- Worst trades (by snapshot_id, deduplicated)
SELECT DISTINCT json_extract(w.watch_json, '$.entry.snapshot_id') as snapshot_id,
  w.symbol, json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl,
  json_extract(w.watch_json, '$.entry.confidence') as conf
FROM watches w
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
ORDER BY pnl ASC LIMIT 30;

-- Best trades
-- (same query, ORDER BY pnl DESC)
```

#### 2B. Read snapshot JSONs
For each snapshot, extract:
1. **Trigger**: headline, source, news age
2. **Triage**: confidence, reasoning
3. **Rounds**: each agent's findings (summarize, don't paste full text)
4. **Prediction**: direction, confidence, horizon, key_catalyst, magnitude, bull_case, bear_case, risk_factors
5. **Tools used**: from tool_traces or rounds[].tool_calls

#### 2C. Pattern identification
Compare winners vs losers across:
- Catalyst specificity (concrete numbers vs vague narrative)
- News freshness
- Whether stock had already moved significantly
- Whether short squeeze was cited
- Whether volume delta contradicted the direction
- Whether the synthesis agent (Gemini) dissented or amplified
- Source quality

### Phase 3: Bearish signal analysis (for shorting)

#### 3A. Find bearish signals
```sql
SELECT s.snapshot_id, s.symbols, s.created_at,
  json_extract(s.snapshot_json, '$.prediction.confidence') as confidence,
  json_extract(s.snapshot_json, '$.prediction.magnitude_estimate') as magnitude,
  json_extract(s.snapshot_json, '$.prediction.key_catalyst') as catalyst,
  json_extract(s.snapshot_json, '$.trigger.headline') as headline
FROM snapshots s
WHERE json_extract(s.snapshot_json, '$.prediction.direction') = 'bearish'
  AND json_extract(s.snapshot_json, '$.triage.action') = 'investigate'
ORDER BY json_extract(s.snapshot_json, '$.prediction.confidence') DESC
LIMIT 50;
```

#### 3B. Check actual outcomes
For bearish signals older than 1 day, check what actually happened to the stock price. Use price_context from the snapshot to get the price at signal time, then compare to subsequent prices.

#### 3C. Shortability assessment
Categorize each bearish signal by:
- Price tier (<$1, $1-5, $5-20, $20+)
- Catalyst type (dilution, earnings_miss, distress, insider_selling, etc.)
- Whether the stock is practically shortable (exchange, liquidity)

### Phase 4: Skip pattern audit

#### 4A. Quantify what's being skipped
```sql
SELECT
  sum(CASE WHEN json_extract(s.snapshot_json, '$.triage.action') = 'skip' THEN 1 ELSE 0 END) as skipped,
  sum(CASE WHEN json_extract(s.snapshot_json, '$.triage.action') = 'investigate' THEN 1 ELSE 0 END) as investigated,
  count(*) as total
FROM snapshots s;
```

#### 4B. Find potentially missed opportunities
Look for skipped snapshots whose symbols later appeared in profitable watches:
```sql
SELECT s.symbols, s.snapshot_id, s.created_at,
  json_extract(s.snapshot_json, '$.trigger.headline') as headline,
  json_extract(s.snapshot_json, '$.triage.reasoning') as skip_reason
FROM snapshots s
WHERE json_extract(s.snapshot_json, '$.triage.action') = 'skip'
  AND EXISTS (
    SELECT 1 FROM watches w
    WHERE w.symbol = s.symbols
    AND json_extract(w.watch_json, '$.exit.realized_pnl_pct') > 3
    AND json_extract(w.watch_json, '$.entry.time') > s.created_at
    AND julianday(json_extract(w.watch_json, '$.entry.time')) - julianday(s.created_at) < 1
  )
LIMIT 30;
```

#### 4C. Check losing trade headlines for new skip pattern candidates
```sql
SELECT json_extract(s.snapshot_json, '$.trigger.headline') as headline,
  json_extract(s.snapshot_json, '$.trigger.source') as source,
  json_extract(w.watch_json, '$.exit.realized_pnl_pct') as pnl,
  w.symbol
FROM watches w
JOIN snapshots s ON json_extract(w.watch_json, '$.entry.snapshot_id') = s.snapshot_id
WHERE w.status IN ('sealed', 'cooling_off')
  AND json_extract(w.watch_json, '$.exit.price') IS NOT NULL
  AND json_extract(w.watch_json, '$.exit.realized_pnl_pct') < -3
ORDER BY pnl ASC LIMIT 40;
```

Look for recurring headline patterns among losers that could be added to `skip_patterns.jsonc`.

#### 4D. Evaluate commented-out patterns
Check if re-enabling commented-out patterns would prevent losses without blocking winners.

### Phase 5: Archive-based skip pattern study

**This is the rigorous way to evaluate skip patterns.** Do NOT draw conclusions about skip patterns from trade-only data (survivorship bias). Instead, scan the full news archive.

#### Script: `scripts/skip_pattern_study.py`

```bash
uv run python scripts/skip_pattern_study.py --days 12 --workers 10
```

**What it does:**
1. Loads all articles from `data/news/archive/insight_sentry/` (zip files) + `data/news/incoming/insight_sentry/`
2. Filters symbols to US-traded equities: keeps only `NASDAQ:`, `NYSE:`, `AMEX:`, `ARCA:`, `NYSEARCA:`, `BATS:` prefixes, then validates against Alpaca's tradeable asset list
3. Matches headlines against configurable regex patterns (both current skip patterns and proposed new ones)
4. Fetches 5-min price bars from **Schwab** (`get_candles_by_date_range`), yfinance as fallback
5. For each article-symbol pair, computes `max_gain` and `max_drawdown` over 6 windows (1h, 2h, 4h, 8h, 1d, 2d) relative to the entry price (Open of first bar after article timestamp)
6. Aggregates by pattern, compares each pattern's performance to the baseline (all articles)

**Output files:**
- `data/skip_pattern_study.csv` — raw per-article returns (one row per article-symbol-pair)
- `data/skip_pattern_summary.csv` — aggregated stats per pattern

**To add new patterns to test**, edit the `PATTERNS` dict in the script. Each entry is a name → compiled regex.

**Key constraints:**
- Schwab intraday data available for ~14 trading days back (date-range API)
- Windows are calendar time, not trading hours (a "1d" window = 24 clock hours, crossing overnight)
- Bars are regular hours only (`extended_hours=False`)
- Minimum 5 matches required before a pattern appears in the summary

**Interpreting results:**
- Compare each pattern's avg/median gain to `_BASELINE_ALL` (all articles regardless of pattern)
- A pattern is only a skip candidate if it **consistently underperforms** baseline with a large sample (n >= 50)
- Patterns that perform AT or ABOVE baseline should NOT be skipped, even if our *trades* on them lost money (that's a pipeline issue, not a news quality issue)

**Baseline results (2026-03-12, n=17,247):**

| Window | avg_gain | med_gain | pct>=2% gain |
|--------|----------|----------|-------------|
| 1h | 1.32% | 0.64% | 17.5% |
| 4h | 2.62% | 1.29% | 37.2% |
| 1d | 3.84% | 2.08% | 50.9% |
| 2d | 4.62% | 2.46% | 55.8% |

### Phase 6: Write-up

Append all new findings to `docs/TRADE-ANALYSIS-DEEP-DIVE.md` in a new dated section. Include:
- Updated statistics (compare to previous run)
- New patterns discovered
- Skip pattern changes made (or recommended)
- Archive study results (always run Phase 5 before making skip pattern recommendations)
- Any filter/exit strategy recommendations
- Bearish/shorting updates

## Key metrics to track across runs

| Metric | Baseline (2026-03-11) |
|--------|----------------------|
| Total closed trades | 515 |
| Win rate | 45.2% |
| Avg P&L | +0.27% |
| Cumulative P&L | +139.67% |
| Avg peak P&L | +2.46% |
| Avg trough P&L | -2.23% |
| Stop-loss trades | 76 (14.8%) |
| Skip rate | 83% |
| Bearish signal accuracy | 76% |
| Archive study baseline (1d avg_gain) | 3.84% (n=17,247) |
| Archive study symbols fetched | 3,401/3,406 (99.85%) |

Update this table each run for trend tracking.

## Gotchas

- **Survivorship bias in trade-only analysis**: NEVER evaluate skip patterns using only trades that resulted in buys. This is a biased subset. Always run the full archive study (Phase 5) first. The 2026-03-12 analysis showed trade-only data recommended skipping patterns that actually perform +25-100% above baseline in the full archive.
- **Reconcile-adopted watches**: Some watches have `snapshot_id = 'reconcile_adopted'` — these don't link to real snapshots. Join queries will miss them. Filter or handle separately.
- **Duplicate watches per snapshot**: Multiple portfolios can buy the same snapshot, creating duplicate watch entries. Use `DISTINCT` on snapshot_id when counting unique trades.
- **Hold time <30 min = 0% win rate**: Every trade that exited in under 30 minutes was a loser (as of 2026-03-11). This is not a small-sample fluke — it's 30 trades.
- **Confidence is not predictive**: Don't assume higher LLM confidence = better trade. The data shows slight inverse correlation.
- **Source matters more than confidence**: News source is the strongest single predictor of trade outcome.
- **Gemini never dissents**: The synthesis agent has never pushed back on a bullish thesis in any examined snapshot. This is a pipeline design flaw, not a feature.
- **Schwab for price data**: Always use Schwab (`SchwabMarketClient`) for price fetching in analysis scripts, not yfinance. yfinance is only a fallback. See `docs/src/SCHWABDEV.md`.
- **insight_sentry symbols have exchange prefixes**: ALL symbols in the archive use prefixes like `NASDAQ:AAPL`, `NYSE:GE`. Filter by prefix to get US equities, then validate against Alpaca's asset list for tradeability.

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — Data sources for debugging trade issues
- [ALPACA.md](ALPACA.md) — Order lifecycle, transaction log queries
- [TRADE-ANALYSIS-DEEP-DIVE.md](../TRADE-ANALYSIS-DEEP-DIVE.md) — Full analysis results
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Exit strategies, portfolio management
- [ARCHITECTURE.md](../ARCHITECTURE.md) — System overview
- [skip_patterns.jsonc](../../data/knowledge/skip_patterns.jsonc) — Headline skip patterns
