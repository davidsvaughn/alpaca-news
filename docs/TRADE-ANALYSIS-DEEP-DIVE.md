# Deep Dive: Trade Performance Analysis & Signal Quality

## Dataset Overview

| Metric | Count |
|--------|-------|
| Total snapshots | 13,051 |
| Investigated (triage = "investigate") | 2,225 (17%) |
| Bullish investigated | 1,370 |
| Bearish investigated | 683 |
| Neutral investigated | 92 |
| Unique snapshots that led to buys | 249 (18% of investigated bullish) |
| Total closed trades analyzed | 515 |
| Winners | 233 (45.2%) |
| Losers | 282 (54.8%) |
| Average P&L per trade | +0.27% |
| Cumulative P&L | +139.67% |

The system is slightly profitable overall but has a sub-50% win rate, meaning the winners are larger than the losers on average.

---

## Part 1: What Predicts Good vs Bad Trades?

### 1. NEWS SOURCE (Strongest signal)

This is the single most actionable filter I found:

| Source | Trades | Avg P&L | Win Rate | Verdict |
|--------|--------|---------|----------|---------|
| **Quartr** | 10 | **+3.56%** | 50.0% | BEST - fresh earnings data |
| **GuruFocus** | 45 | +0.69% | **55.6%** | Good |
| **Dow Jones** | 170 | +0.60% | 44.7% | Solid, high volume |
| **Reuters** | 111 | +0.16% | 51.4% | OK but modest returns |
| **Stocktwits** | 19 | +0.42% | 36.8% | Low win rate |
| **Barchart** | 11 | -1.30% | 36.4% | Avoid |
| **Stock Story** | 7 | -0.33% | **14.3%** | Terrible - avoid |
| **GlobeNewswire** | 6 | -1.74% | **0.0%** | Zero winners - block |
| **Benzinga** | 5 | **-3.09%** | **0.0%** | Zero winners - block |

**Insight**: GlobeNewswire and Benzinga articles have produced zero winning trades. GlobeNewswire typically publishes company press releases (often promotional), and Benzinga articles tend to be recycled/aggregated content. Stock Story is retrospective "why stock moved" articles — by definition stale. These three sources should be filtered at the triage level.

### 2. CATALYST TYPE IN KEY_CATALYST (Strong signal)

| Catalyst Keyword | Trades | Avg P&L | Win Rate | Verdict |
|------------------|--------|---------|----------|---------|
| **Contract/order wins** | 21 | **+2.76%** | 42.9% | Best avg return |
| **Earnings beat** (specific) | 24 | +1.94% | **58.3%** | Highest win rate |
| **Technical/oversold** | 36 | +1.05% | **61.1%** | Surprisingly good |
| Clinical/FDA | 14 | +0.70% | 35.7% | Mixed |
| Upgrades | 46 | +0.20% | 41.3% | Disappointing |
| M&A | 27 | -0.60% | 55.6% | Decent win rate but avg loss |
| **Buyback** | 28 | **-0.61%** | **35.7%** | Bad - avoid |
| **Short squeeze** | 19 | **-0.66%** | **36.8%** | Bad - avoid |
| **Guidance** | 19 | **-2.46%** | **21.1%** | Worst - strongly avoid |

**Key Insight**: "Short squeeze" as a catalyst is a **trap**. The LLMs love citing high short interest as bullish fuel, but it produces -0.66% avg returns with only 36.8% win rate. Similarly, "buyback" announcements and "guidance" (forward-looking statements without earnings beats) are negative-return catalysts.

The best catalysts are **specific, concrete, and already realized**: actual contract values, actual earnings numbers that beat estimates, and technical oversold bounces.

### 3. HOLD TIME (Critical for exit strategy)

| Hold Duration | Trades | Avg P&L | Win Rate |
|---------------|--------|---------|----------|
| **<30 min** | 30 | **-2.87%** | **0.0%** |
| **30-60 min** | 24 | **-1.45%** | **4.2%** |
| 1-2 hr | 71 | -1.02% | 46.5% |
| 2-4 hr | 109 | -0.10% | 49.5% |
| **4-8 hr** | 32 | **+1.17%** | **56.3%** |
| **8 hr+** | 249 | **+1.23%** | **51.0%** |

**This is the most striking finding**: Every single trade that exited in under 30 minutes was a loser. Under 1 hour has a 1.9% win rate. This strongly suggests a **minimum hold time** of at least 2 hours, and ideally 4+ hours. Trades exiting quickly are likely getting whipsawed by initial volatility after entry.

### 4. TIME OF DAY (Moderate signal)

| Session (ET) | Trades | Avg P&L | Win Rate |
|--------------|--------|---------|----------|
| Pre-market (<10am) | 171 | -0.01% | 46.8% |
| Morning (10-12) | 100 | +0.38% | **51.0%** |
| Early afternoon (12-2) | 157 | +0.56% | 49.0% |
| **Late afternoon (2-4)** | 84 | +0.21% | **29.8%** |
| After hours | 3 | -0.28% | 0.0% |

**Late afternoon entries are terrible** — 29.8% win rate. This makes sense: you're buying into end-of-day profit-taking and getting little time for the thesis to develop.

### 5. CONFIDENCE SCORES (Poorly calibrated — not useful for filtering)

| Prediction Confidence | Trades | Avg P&L | Win Rate |
|----------------------|--------|---------|----------|
| 0.50 | 40 | +0.44% | 47.5% |
| 0.75 | 152 | +0.33% | 50.0% |
| 0.85 | 242 | +0.23% | 41.7% |
| 0.90 | 6 | -1.15% | 0.0% |

**The confidence score is not predictive**. In fact, there's a slight *inverse* relationship — lower confidence trades actually do marginally better! The 0.90 confidence bucket has zero winners (though small sample). This tells us the LLMs are not well-calibrated: they assign high confidence based on narrative strength, not actual predictive accuracy.

**Triage confidence** is slightly better (0.95+ = +0.71% vs <0.70 = -1.80%), but still weak.

### 6. STOCK PRICE RANGE

| Price Range | Trades | Avg P&L | Win Rate |
|-------------|--------|---------|----------|
| <$5 | 24 | +0.94% | 45.8% |
| **$5-20** | 133 | **-0.53%** | **38.3%** |
| $20-50 | 102 | +0.46% | 50.0% |
| $50-100 | 78 | +0.83% | 50.0% |
| $100+ | 178 | +0.43% | 45.5% |

**$5-20 stocks are the worst bucket** — likely small/mid-caps with enough liquidity to trade but high volatility and susceptibility to manipulation. The sweet spot is $20-100.

### 7. EXIT STRATEGY PROBLEM: Trades That Reversed

12 trades hit the -5% stop loss AFTER achieving a peak of +5% or more:

| Symbol | Peak P&L | Final P&L | Reversal |
|--------|----------|-----------|----------|
| CVGI | +20.6% | -5.1% | **25.7% reversal** |
| CVGI | +20.4% | -5.3% | 25.7% reversal |
| KSS | +8.7% | -5.0% | 13.7% reversal |
| FTRE | +8.0% | -5.0% | 13.0% reversal |
| SOC | +7.1% | -5.0% | 12.1% reversal |
| TSSI | +6.5% | -5.2% | 11.7% reversal |

This represents a serious exit strategy gap. A trailing stop or take-profit at +5% would have captured these gains. CVGI alone left ~25% on the table.

### 8. MAGNITUDE PREDICTION ACCURACY

| Outcome | Count | Avg P&L |
|---------|-------|---------|
| Reached predicted magnitude | 91 (19%) | +3.76% |
| Reached half of prediction | 106 (22%) | +0.79% |
| Missed entirely | 276 (58%) | -1.06% |

The LLMs systematically overestimate magnitude. Only 19% of trades reach their predicted target. This suggests the system should use much lower magnitude expectations (roughly half of what the LLM predicts).

---

## Part 2: Patterns from Detailed Snapshot Analysis (50+ snapshots examined)

### What Winners Have in Common

From analyzing the 10 best trades (AAOI +24.5%, AMPX +12.6%, PROF +12.2%, TEN +10.6%, etc.):

1. **Concrete, quantified catalysts**: "$200M order", "EPS beat by 121%", "revenue +95% YoY". Real numbers, not vague positives.
2. **Structural inflection points**: Not just beating estimates but representing a change in the company narrative (AMPX pivoting to capital-light manufacturing, DTI's 16% FCF yield hidden behind GAAP loss).
3. **Agents looked past misleading headlines**: DTI's GAAP loss headline hid an earnings beat. AMPX's one-time impairment was actually a positive signal.
4. **Multiple reinforcing mechanics**: Short squeeze fuel + earnings beat + index inclusion creates compounding pressure.
5. **Fresh news**: All top winners had news < 5 minutes old from reliable sources.

### What Losers Have in Common

From analyzing the 14 worst trades (all hit -5% stop loss):

1. **Short squeeze as primary thesis** (6 of 14): High short interest was treated as bullish rather than recognized as a warning.
2. **Buying into extended moves**: GCO already up +19.9% intraday, HRTG rallied +18% over prior week, ONDS already jumped +8%.
3. **Stale or recycled news**: SOFI news was 3 days old (Zacks rehash), HIMS hire announced 17 hours prior.
4. **Non-exclusive or indirect catalysts**: GDRX's partnership was shared with 15+ administrators. BTDR was a secondary mention in a CLSK article. CRCL was an indirect beneficiary of Ripple news.
5. **Bearish volume delta ignored**: IQV and MMED both showed bearish volume delta but got bullish predictions anyway.
6. **Gemini never dissented**: In every snapshot, the synthesis agent (Gemini) amplified the bullish thesis. It never pushed back or said "this doesn't look good."

### Red Flag Checklist (would have filtered out most losers)

- [ ] Stock already moved >10% on the day
- [ ] Short squeeze is the primary catalyst
- [ ] News is >30 minutes old
- [ ] Catalyst is non-exclusive (shared with competitors)
- [ ] Catalyst is indirect (news about a different company)
- [ ] Bearish volume delta at time of analysis
- [ ] Source is Benzinga, GlobeNewswire, Stock Story, or Barchart
- [ ] RSI > 75 (overbought)
- [ ] Guidance/outlook as primary catalyst (not backed by actual numbers)
- [ ] Buyback as primary catalyst

---

## Part 3: Consistently Profitable vs Unprofitable Symbols

| Symbol | Trades | Win Rate | Avg P&L | Total P&L |
|--------|--------|----------|---------|-----------|
| **DNTH** | 7 | **100%** | +7.42% | +51.94% |
| **ADEA** | 6 | **100%** | +6.62% | +39.74% |
| **XPEV** | 7 | **100%** | +4.15% | +29.04% |
| **AMD** | 7 | **86%** | +3.18% | +22.25% |
| **BMY** | 7 | **100%** | +0.38% | +2.64% |
| **GOOG** | 5 | **100%** | +0.53% | +2.63% |
| PBR | 6 | 17% | +0.41% | +2.48% |
| COST | 8 | 38% | +0.25% | +1.97% |

Some symbols (DNTH, ADEA, XPEV, AMD) are consistent winners across multiple trades. This suggests certain stocks have characteristics that align well with the system. These could be given higher allocation.

---

## Part 4: Shorting Analysis

### Bearish Signal Accuracy: 76% (16/21 verifiable)

From 25 high-confidence bearish signals checked against subsequent prices:

**Correct bearish calls** (stock dropped as predicted):
- RBOT: -76.6% (NYSE delisting)
- MOBX: -33.0% (massive dilution)
- GO: -20.0% (store closures, guidance miss)
- USEG: -19.2% (offering at 23% discount)
- STUB: -17.2% (EBITDA guidance miss)
- CMLS: -17.5% (Chapter 11 bankruptcy)

**Wrong calls** (stock rose despite bearish signal):
- DXST: +182.7% (penny stock pump)
- QURE: +61.0% (short squeeze overwhelmed FDA bear case)
- ELEK: +42.9% (penny stock pump)

### Pattern: Where Shorting Works vs Doesn't

**HIGH confidence shorts** (do this):
- Mid/large-cap stocks with concrete negative catalysts (delisting, bankruptcy filing, massive dilution, earnings miss + guidance cut)
- Price > $5, listed on major exchange
- Bearish confidence > 0.85

**DON'T short**:
- Sub-$1 penny stocks (4 of 5 wrong calls were OTC/penny stocks)
- Stocks with >30% short float (squeeze risk)
- Stocks in momentum sectors (meme dynamics overwhelm fundamentals)

### Shortable Candidates Framework

The strongest short setups from the data are:
1. **Dilution events** (28% of bearish signals) - secondary offerings, ATM programs, convertible notes at discount
2. **Earnings misses with guidance cuts** - double negative (past + future)
3. **Corporate distress** - going concern warnings, Chapter 11, delisting notices
4. **Coordinated insider selling** - multiple insiders selling simultaneously

Most bearish signals (44%) are in unshorTable micro-caps. To make shorting practical, you'd need to filter to stocks > $10, market cap > $500M, and listed on NYSE/NASDAQ.

---

## Part 5: Specific Recommendations

### Immediate Filters (High confidence, easy to implement)

1. **Block bad news sources**: Filter out Benzinga, GlobeNewswire, Stock Story, Barchart at triage level. Zero or near-zero win rates.

2. **Minimum hold time**: Set a minimum hold of at least 60 minutes, ideally 120 minutes. Trades exiting in <30 min have a 0% win rate.

3. **Reject "short squeeze" as primary catalyst**: When `key_catalyst` mentions short squeeze/short interest as the primary thesis, skip. 36.8% win rate, negative returns.

4. **Reject "guidance/outlook" as primary catalyst** unless backed by actual earnings beat. 21.1% win rate, -2.46% avg.

5. **Reject buyback-only catalysts**: 35.7% win rate, negative returns.

6. **No late afternoon entries (2-4pm ET)**: 29.8% win rate.

7. **Price range filter**: Prefer $20-100. The $5-20 range is the worst bucket.

### Exit Strategy Improvements

8. **Add trailing stop**: The data shows 12 trades achieved +5-20% peaks but ended at -5%. A trailing stop that activates after +3% gain (e.g., trailing 3% from peak) would capture significant value.

9. **Take profit at predicted magnitude / 2**: Since only 19% of trades reach the predicted magnitude, consider taking partial profits at half the predicted target.

### LLM Pipeline Improvements

10. **Add a "devil's advocate" round**: The synthesis agent (Gemini) never dissents. Add a round that explicitly argues against the trade. If the bear case is stronger than the bull case, skip.

11. **Stale news detection**: Auto-reject when the underlying news is > 30 minutes old (regardless of when the article was published). Several losers involved recycled/rehashed articles.

12. **Already-moved detection**: If the stock has already moved > 10% on the day, require higher confidence or skip. Buying into extended moves is a consistent pattern among losers.

13. **Volume delta confirmation**: If real-time volume delta is bearish at analysis time, do NOT assign bullish direction. IQV and MMED both had bearish volume delta yet got bullish calls.

14. **Recalibrate confidence or stop using it for filtering**: The prediction confidence is not predictive of outcomes. Either redesign confidence extraction to be grounded in specific factors, or remove it as a filter criterion entirely and replace with a rules-based scoring system using the factors above.

### For Shorting

15. **Pilot with mid/large-cap bearish signals**: Filter to stocks > $10, market cap > $500M, confidence >= 0.85, catalyst = dilution/earnings_miss/distress. The 76% accuracy rate suggests this is viable.

16. **Avoid shorting penny/OTC stocks**: Momentum overwhelms fundamentals in this space.

17. **Cluster-based shorts**: The private credit theme (BLK, BX, OWL) suggests sector-wide short opportunities. When multiple stocks in a sector get bearish signals, the conviction should be higher.

---

## Summary of Expected Impact

If you had applied just filters #1 (bad sources), #3 (no short-squeeze catalysts), #4 (no guidance-only), and #6 (no late afternoon) to the historical data:

- **Blocked sources**: ~28 trades avoided, saving ~$1.67% avg loss per trade
- **Short squeeze filter**: 19 trades avoided, saving 0.66% avg per trade
- **Guidance filter**: 19 trades avoided, saving 2.46% avg per trade
- **Late afternoon**: ~84 trades with improved win rate

These filters would reduce trade count by roughly 15-20% while eliminating some of the worst-performing segments, significantly improving the overall win rate and average return.
