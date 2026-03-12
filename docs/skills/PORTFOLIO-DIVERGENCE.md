# Portfolio Divergence Analysis

> How to investigate why two parallel portfolios with the same config have diverged. Use this when comparing Alpaca vs simulated, or any two LiveConfigs that should behave identically.

## Quick reference

| What you need | Where to look | How |
|---------------|---------------|-----|
| Live config parameters | `live_configs` table | [Compare configs](#step-1-confirm-configs-match) |
| Trade history per portfolio | `watches` table | [Compare trades](#step-2-compare-trade-histories) |
| Alpaca fill details | `alpaca_transactions` table | [Check fills](#step-3-check-alpaca-fill-prices) |
| Replacement exits | `watches.exit_reason` | [Identify churn](#step-4-identify-replacement-churn) |
| Current holdings | `watches WHERE status='holding'` | [Compare holdings](#step-5-compare-current-holdings) |
| Entry price slippage | `watches` + `alpaca_transactions` | [Measure slippage](#step-6-measure-entry-slippage) |

## The process

### Step 1: Confirm configs match

Before assuming a bug, verify both LiveConfigs have identical parameters.

```sql
SELECT id, config_json FROM live_configs
WHERE id IN ('lc_AAAA', 'lc_BBBB');
```

Parse `config_json` and diff key fields: `filters`, `exit_strategy`, `exit_params`, `allocation`, `max_positions`, `when_full`, `rank_method`. If they differ, the divergence may be intentional.

### Step 2: Compare trade histories

Pull all exited watches for both configs side-by-side.

```sql
-- Summary stats per config
SELECT live_config_id,
       COUNT(*) AS total_trades,
       SUM(CASE WHEN status IN ('exited','cooling_off','sealed') THEN 1 ELSE 0 END) AS closed,
       SUM(CASE WHEN exit_reason = 'replacement' THEN 1 ELSE 0 END) AS replacements,
       SUM(CASE WHEN exit_reason = 'stop_loss' THEN 1 ELSE 0 END) AS stops,
       SUM(CASE WHEN exit_reason LIKE '%vdd%' OR exit_reason LIKE '%signal%' THEN 1 ELSE 0 END) AS signal_exits
FROM watches
WHERE live_config_id IN ('lc_AAAA', 'lc_BBBB')
GROUP BY live_config_id;
```

**Red flags:**
- One config has many more `replacement` exits than the other
- One config has significantly more total trades (churn)
- Win/loss ratio differs dramatically

### Step 3: Check Alpaca fill prices

For the Alpaca-connected portfolio, compare intended vs actual fill prices.

```sql
SELECT symbol, event, status,
       json_extract(detail_json, '$.filled_avg_price') AS fill_price,
       json_extract(detail_json, '$.notional') AS intended_notional,
       created_at
FROM alpaca_transactions
WHERE account_id = 'ACCOUNT_ID'
  AND event IN ('buy_confirmed', 'sell_confirmed')
  AND created_at >= '2026-03-11'
ORDER BY created_at;
```

Compare `fill_price` against the simulated portfolio's `entry_price` in watches. Systematic slippage compounds over many trades.

### Step 4: Identify replacement churn

This is the most common divergence source. Pull all replacement exits and calculate their P&L.

```sql
SELECT symbol, entry_price, exit_price,
       ROUND((exit_price - entry_price) / entry_price * 100, 2) AS pct_pl,
       exit_reason, exited_at
FROM watches
WHERE live_config_id = 'lc_XXXX'
  AND exit_reason = 'replacement'
ORDER BY exited_at;
```

**Churn death spiral pattern:** Many small negative replacement exits (-0.5% to -2%) in rapid succession. This means the replacement scoring threshold is too aggressive — positions are being sold for being slightly underwater due to intraday noise.

### Step 5: Compare current holdings

```sql
SELECT w1.symbol AS config_A, w2.symbol AS config_B
FROM (SELECT symbol FROM watches WHERE live_config_id='lc_AAAA' AND status='holding') w1
FULL OUTER JOIN
     (SELECT symbol FROM watches WHERE live_config_id='lc_BBBB' AND status='holding') w2
ON w1.symbol = w2.symbol
WHERE w1.symbol IS NULL OR w2.symbol IS NULL;
```

If portfolios have diverged significantly, they'll hold different symbols. The count of non-overlapping symbols quantifies how far they've drifted.

### Step 6: Measure entry slippage

For positions that exist in both portfolios, compare entry prices.

```sql
SELECT a.symbol,
       a.entry_price AS price_A,
       b.entry_price AS price_B,
       ROUND((b.entry_price - a.entry_price) / a.entry_price * 100, 2) AS slippage_pct
FROM watches a
JOIN watches b ON a.symbol = b.symbol
WHERE a.live_config_id = 'lc_AAAA'
  AND b.live_config_id = 'lc_BBBB'
  AND a.status = 'holding' AND b.status = 'holding';
```

Systematic positive slippage on the Alpaca side means limit price buffers may be too generous, or market orders are getting poor fills.

## Known divergence causes

### 1. Replacement scoring asymmetry (found 2026-03-12)

**Symptom:** Alpaca portfolio churns heavily (many replacement exits), simulated portfolio makes zero replacements.

**Root cause:** In `_score_holding_watches` ([live_monitor.py](../../trader/online/live_monitor.py)), non-Alpaca portfolios have no `current_price`, so all positions score 0.0. New signals also score 0.0. Since `new_score > worst_score` is never true (0.0 > 0.0 is false), replacements never fire. The Alpaca portfolio has real prices, so slightly-negative positions get replaced constantly.

**Impact:** 12 unnecessary replacement exits totaling -$745 in one day. Churn cascaded as new positions also went slightly negative and got replaced.

**Key files:** [live_monitor.py](../../trader/online/live_monitor.py) `_score_holding_watches`, `_find_replacement_victim`

### 2. Fill slippage compounding

**Symptom:** Alpaca portfolio consistently enters positions at higher prices than simulated.

**Root cause:** Market/limit order fills during volatile moments. Extended hours fills are worse (thin liquidity + wider spreads).

**Impact:** +0.5% average slippage per entry. Compounds with replacement churn — positions start underwater and are more likely to be replaced.

### 3. Stop-loss trigger differences

**Symptom:** Alpaca portfolio hits stop losses that the simulated portfolio avoids.

**Root cause:** Higher entry prices from slippage mean the -5% stop is at a higher absolute price. The simulated portfolio, with its lower entry, may exit via VDD signal before the stop is reached.

## Gotchas

### Don't compare raw equity curves alone

The equity chart shows the combined effect of all trades. To find the root cause, you need to compare **individual trade outcomes** — which trades were the same, which were different, and why.

### Check `exit_reason` carefully

Exit reasons tell you whether a trade was closed by signal, stop, replacement, or reconciliation. If one portfolio has many `replacement` exits and the other has none, that's your divergence.

### Simulated portfolios aren't truly simulated

The "non-Alpaca" portfolio still tracks real prices for exit evaluation. The key difference is: no broker connection means no fill slippage, no order failures, and (critically) no `current_price` from broker for scoring. This can make features like replacement scoring silently non-functional.

### Timestamps differ between tables

- `watches.exited_at` — UTC ISO 8601
- `alpaca_transactions.created_at` — UTC (`func.now()`)
- Dashboard charts — ET (local)

When cross-referencing events, convert to the same timezone.

## Cross-references

- [DIAGNOSTICS.md](DIAGNOSTICS.md) — General debugging, log sources, SQL patterns
- [ALPACA.md](ALPACA.md) — Order lifecycle, fill confirmation, reconciliation
- [TRADE-ANALYSIS.md](TRADE-ANALYSIS.md) — Performance analysis queries
- [LIVE-TRADING.md](../LIVE-TRADING.md) — Live trading implementation details
- [live_monitor.py](../../trader/online/live_monitor.py) — Exit strategies, replacement logic, portfolio manager
- [alpaca_broker.py](../../trader/market/alpaca_broker.py) — Order execution, fill slippage source
