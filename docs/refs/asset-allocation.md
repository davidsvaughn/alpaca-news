Q:

so, back to "option B" : "Capital divided equally among active trades at each moment."

realistically, how would you most closely approximate this in real life?
if you have new "buy" signals rolling in throughout the day (random, but on average let's say once every 30 minutes)..... 
also, you have "sell" signals rolling in (again random, but on average let's say once every 30 minutes)...

then on each event (buy or sell signal) you would have to redistribute by either selling or buying a small fraction of all your other holdings, right? selling a small fraction of each to buy a new stock, or buy a small fraction of each when you sell another stock.... right?  correct me if I'm confused though....

I imagine what people would more realistically do, to reduce transaction costs, is to pick one or two of the least attractive stocks to sell (instead of selling a tiny piece of all)... or pick one or two of the most attractive stocks to buy more of (instead of increasing all holdings by a small amount)... right?  am I missing anything?  other considerations? 

---

A:

Good — now you’re thinking about *execution realism*, not just math.

You’re correct:

If you strictly enforce

> “capital divided equally among all active trades at each moment”

then yes — mathematically you would have to rebalance continuously:

* New buy → sell tiny fraction of all existing positions
* Sell signal → redistribute freed capital across all remaining positions

That is a **continuous equal-weight portfolio**.

In practice, almost nobody does that intraday.

Transaction costs and slippage would kill you.

---

# What Real Systems Actually Do

There are 4 realistic approaches.

---

# 1️⃣ Fixed Dollar Per Trade (Most Common Approximation)

When a buy signal arrives:

* Allocate fixed capital chunk (e.g. 5% of portfolio)
* Do not rebalance existing positions

When a position exits:

* Capital goes to cash
* Reused on next signal

No continuous rebalancing.

This is the most common research setup.

Pros:

* Simple
* Realistic
* Low turnover

Cons:

* Portfolio weights drift over time

---

# 2️⃣ Max Positions Constraint

Example:

* Max 10 concurrent trades
* Each trade = 10% capital

If a new signal comes and 10 slots are full:

* Ignore it
  OR
* Replace weakest position (ranking-based)

This is very common in systematic funds.

This avoids micro-rebalancing entirely.

---

# 3️⃣ Discrete Rebalancing Schedule

Instead of rebalancing on every event:

* Rebalance once per day
* Or once per hour
* Or when weight drift exceeds threshold

Much more realistic than continuous adjustment.

---

# 4️⃣ Ranking-Based Capital Reallocation (Closer to Reality)

What you described is exactly what many quant systems do:

When a new buy signal appears:

* Rank all active positions by attractiveness score
* If capital full:

  * Sell lowest-ranked
  * Replace with new higher-ranked
* Otherwise:

  * Allocate remaining cash

This avoids fractional trimming of everything.

This is how most cross-sectional equity strategies operate.

---

# Why Continuous Equal Weight Is Rare Intraday

Because:

* You would generate enormous turnover
* You would trade constantly
* Slippage would dominate returns
* Market impact increases

Equal weight is a theoretical construct, not an execution plan.

---

# For Methods that Require Ranking all Current Holdings...

---

## Signal Strength Ranking (Most Common in Systematic Trading)

If your entry signal has magnitude:

Examples:

### RSI example

[
Score_i = RSI_{threshold} - RSI_i
]

The more oversold → higher score.

### Breakout system

[
Score_i = \frac{P_t - High_n}{ATR}
]

The stronger the breakout → higher score.

### Mean reversion

[
Score_i = -z\text{-score}(return)
]

Signal magnitude becomes rank.

This is extremely common.

---

## Momentum Ranking (Classic Cross-Sectional)

One of the most researched strategies:

[
Score_i = R_{past,n,months}
]

Rank by past returns.

Variants:

* 12m momentum skipping last month
* 3m momentum
* Residual momentum

Extremely robust historically.

---

## Composite Ranking (Used by Many Funds)

Combine multiple standardized factors:

[
Score_i =
w_1 Z_{momentum}
+
w_2 Z_{meanrev}
+
w_3 Z_{quality}
]

Where Z = cross-sectional z-score.

This smooths instability.

Very common in professional quant equity.

---

# What Works Surprisingly Well

Across decades of research:

1. Rank by signal strength
2. Normalize by volatility
3. Take top N
4. Equal weight
5. Rebalance discretely

Simple beats fancy in most cases.