# PART 1: **EXIT STRATEGIES**

Each exit strategy clearly separates:

* **Market Data Inputs** (observed from market)
* **User Parameters** (tunable knobs)
* **State Variables** (tracked internally by your system)
* **Exit Condition Formula**

This structure is suitable for both discretionary use and automation.

---

# SECTION 1 — Price-Based Exits

---

## 1. Fixed % Stop Loss

**Exit Condition**

$P_t \le P_{entry}(1 - s)$

**Market Data Inputs**

* Current price ($P_t$)

**State Variables**

* Entry price ($P_{entry}$)

**User Parameters**

* Stop percentage (s)

---

## 2. Fixed % Take Profit

$P_t \ge P_{entry}(1 + r)$

**Market Data**

* ($P_t$)

**State**

* ($P_{entry}$)

**User**

* Reward percentage (r)

---

## 3. Risk/Reward Target

$Target = P_{entry} + k \cdot (P_{entry} - P_{stop})$

**Market Data**

* ($P_t$)

**State**

* Entry price
* Initial stop price

**User**

* Risk multiple (k)

---

# SECTION 2 — Trailing Logic

---

## 4. Percent Trailing Stop

$P_t \le P_{max}(1 - \tau)$

**Market Data**

* ($P_t$)

**State**

* Highest price since entry ($P_{max}$)

**User**

* Trail percent ($\tau$)

---

## 5. ATR Trailing Stop

$P_t \le P_{max} - k \cdot ATR_n$

**Market Data**

* ($P_t$)
* ATR value

**State**

* ($P_{max}$)

**User**

* ATR period (n)
* Multiplier (k)

---

# SECTION 3 — Volatility-Based

---

## 6. ATR Fixed Stop

$P_t \le P_{entry} - k \cdot ATR_n$

**Market Data**

* ($P_t$)
* ATR

**State**

* ($P_{entry}$)

**User**

* ATR period
* Multiplier

---

# SECTION 4 — Moving Average / Trend

---

## 7. Close Below Moving Average

$P_t < MA_n$

**Market Data**

* Closing prices

**Derived Market Data**

* Moving average ($MA_n$)

**User**

* Lookback period (n)

---

## 8. Moving Average Cross Exit

$MA_{short} < MA_{long}$

**Market Data**

* Historical prices

**Derived**

* Short MA
* Long MA

**User**

* Short period
* Long period

---

# SECTION 5 — Momentum Indicators

---

## 9. RSI Overbought Exit

$RSI_t \ge \theta$

**Market Data**

* Historical prices

**Derived**

* RSI

**User**

* RSI period
* Threshold ($\theta$) (e.g., 70)

---

## 10. MACD Bearish Cross

Exit if:
$MACD_t < Signal_t$

**Market Data**

* Price series

**Derived**

* EMA fast
* EMA slow
* Signal EMA

**User**

* EMA fast length
* EMA slow length
* Signal length

---

## 11. ROC Reversal Exit

Exit when momentum flips from positive to negative:

$ROC_{n,t} < 0 \quad \text{AND} \quad ROC_{n,t-1} > 0$

**Market Data**

* Close prices

**Derived**

* Rate of Change ($ROC_n$)

**User**

* ROC period ($n$) (e.g., 10)

---

## 12. Stochastic Overbought Cross Exit

Exit when %K crosses below %D in the overbought zone:

$\%K_t < \%D_t \quad \text{AND} \quad \%K_{t-1} \ge \%D_{t-1} \quad \text{AND} \quad \%K_{t-1} > \theta$

**Market Data**

* High, Low, Close

**Derived**

* Stochastic %K
* Stochastic %D (SMA of %K)

**User**

* Lookback period ($n$) (e.g., 14)
* %K smoothing ($k$) (e.g., 3)
* %D smoothing ($d$) (e.g., 3)
* Overbought threshold ($\theta$) (e.g., 80)

---

## 13. ADX Trend Decay Exit

Exit when trend strength drops from strong to weak:

$ADX_t < \theta_{weak} \quad \text{AND} \quad \max(ADX_{t-k:t}) > \theta_{strong}$

**Market Data**

* High, Low, Close

**Derived**

* ADX (Average Directional Index)

**User**

* ADX period ($n$) (e.g., 14)
* Weak threshold ($\theta_{weak}$) (e.g., 20)
* Strong threshold ($\theta_{strong}$) (e.g., 30)
* Lookback for recent strength ($k$) (e.g., 10)

---

# SECTION 6 — Volume / Intraday

---

## 14. VWAP Breakdown

$P_t < VWAP_t$

**Market Data**

* Intraday trade price
* Intraday volume

**Derived**

* VWAP

**User**

* Session definition

---

## 15. Volume Fade Exit

Exit if:
$Volume_t < \alpha \cdot AvgVolume$

**Market Data**

* Volume

**Derived**

* Moving average volume

**User**

* Volume lookback
* Multiplier ($\alpha$)

---

## 16. Volume Delta Divergence Exit

Exit when price makes a new rolling high but cumulative volume delta is declining (buyers losing conviction):

$P_t \ge \max(P_{t-n:t}) \quad \text{AND} \quad \Delta_{cum,t} < \Delta_{cum,t-n}$

Where $\Delta_{cum}$ is the running sum of per-bar net delta (uptick volume − downtick volume), computed via the inter-bar tick rule on 1-min bars.

**Market Data**

* Close prices (for tick rule direction)
* Volume (for uptick/downtick assignment)

**Derived**

* Per-bar uptick/downtick volume (inter-bar tick rule)
* Cumulative volume delta ($\Delta_{cum}$)
* Rolling price high ($P_{max,n}$)

**State Variables**

* Running cumulative delta
* Rolling window of delta values

**User**

* Lookback window ($n$) (e.g., 30 bars)

---

## 17. Volume Imbalance Flip Exit

Exit when the rolling volume imbalance ratio flips from bullish to bearish:

$Imbalance_t < -\alpha \quad \text{AND} \quad \max(Imbalance_{t-k:t}) > \alpha$

Where $Imbalance = \frac{uptick\_vol - downtick\_vol}{uptick\_vol + downtick\_vol}$ over a rolling window, computed via the inter-bar tick rule.

**Market Data**

* Close prices
* Volume

**Derived**

* Per-bar uptick/downtick volume (inter-bar tick rule)
* Rolling imbalance ratio

**User**

* Rolling window ($n$) (e.g., 30 bars)
* Imbalance threshold ($\alpha$) (e.g., 0.05)
* Lookback for recent bullish check ($k$) (e.g., same as $n$)

---

# SECTION 7 — Statistical / Model-Based

---

## 18. Expected Return Threshold Exit

$E[R_{future} | X_t] < \theta$

**Market Data**

* Feature vector ($X_t$)

**Derived**

* Model prediction

**User**

* Threshold ($\theta$)
* Forecast horizon

---

# SECTION 8 — Time-Based

---

## 19. Max Holding Period

$t - t_{entry} \ge T$

**Market Data**

* Current timestamp

**State**

* Entry timestamp

**User**

* Max duration (T)

---

# SECTION 9 — Account-Level Controls

---

## 20. Max Daily Loss

Exit and halt if:
$DailyPnL \le -D$

**Market Data**

* Trade fills

**State**

* Running PnL

**User**

* Daily loss limit (D)

---

# APPENDIX — Derived Input Definitions

Below are formulas for the derived inputs referenced above.

---

## A1. Moving Average (SMA)

$MA_n = \frac{1}{n} \sum_{i=0}^{n-1} P_{t-i}$

**Inputs**

* Historical closing prices
* Period (n)

---

## A2. Exponential Moving Average (EMA)

$EMA_t = \alpha P_t + (1-\alpha) EMA_{t-1}$

$\alpha = \frac{2}{n+1}$

---

## A3. ATR (Average True Range)

Step 1: True Range

$TR_t = \max(High_t - Low_t,\ |High_t - Close_{t-1}|,\ |Low_t - Close_{t-1}|)$

Step 2: ATR

$ATR_n = \text{EMA of } TR_t$

**Inputs**

* High
* Low
* Close

---

## A4. RSI

1. Compute price changes
2. Separate gains and losses
3. Compute average gain and loss
4. Compute RS:

$RS = \frac{AvgGain}{AvgLoss}$

5. RSI:

$RSI = 100 - \frac{100}{1 + RS}$

---

## A5. MACD

$MACD = EMA_{fast} - EMA_{slow}$

Signal line:

$Signal = EMA_{signal}(MACD)$

---

## A6. VWAP

$VWAP_t = \frac{\sum_{i=1}^{t} Price_i \cdot Volume_i}{\sum_{i=1}^{t} Volume_i}$

**Inputs**

* Intraday trade prices
* Intraday volume

Reset each session.

---

## A7. Rate of Change (ROC)

$ROC = \frac{P_t - P_{t-n}}{P_{t-n}}$

---

## A8. Stochastic Oscillator

$\%K_{raw} = \frac{P_t - L_n}{H_n - L_n} \times 100$

Where $L_n$ = lowest low and $H_n$ = highest high over the lookback period.

$\%K = SMA_k(\%K_{raw})$

$\%D = SMA_d(\%K)$

**Inputs**

* High, Low, Close
* Lookback period ($n$), %K smoothing ($k$), %D smoothing ($d$)

---

## A9. ADX (Average Directional Index)

Step 1: Directional Movement

$+DM_t = High_t - High_{t-1}$ (if positive and > $-DM$, else 0)

$-DM_t = Low_{t-1} - Low_t$ (if positive and > $+DM$, else 0)

Step 2: Smoothed directional indicators

$+DI_n = \frac{EMA_n(+DM)}{ATR_n} \times 100$

$-DI_n = \frac{EMA_n(-DM)}{ATR_n} \times 100$

Step 3: ADX

$DX = \frac{|+DI - (-DI)|}{+DI + (-DI)} \times 100$

$ADX_n = EMA_n(DX)$

**Inputs**

* High, Low, Close
* Period ($n$)

---

## A10. Volume Delta (Inter-bar Tick Rule)

Assigns each bar's entire volume as uptick or downtick based on the bar-to-bar price change:

$\delta_t = \begin{cases} +V_t & \text{if } Close_t > Close_{t-1} \\ -V_t & \text{if } Close_t < Close_{t-1} \\ \delta_{t-1} & \text{if } Close_t = Close_{t-1} \end{cases}$

Cumulative delta:

$\Delta_{cum,t} = \sum_{i=1}^{t} \delta_i$

Imbalance ratio over rolling window of $n$ bars:

$Imbalance_n = \frac{\sum uptick\_vol - \sum downtick\_vol}{\sum uptick\_vol + \sum downtick\_vol}$

**Inputs**

* Close prices (for direction)
* Volume (for magnitude)

---

# Data Requirements Summary

| Data Type      | Needed For                                    |
| -------------- | --------------------------------------------- |
| Tick data      | VWAP                                          |
| OHLC bars      | ATR, RSI, MACD, Stochastic, ADX, ROC          |
| Volume         | VWAP, volume exits, volume delta              |
| Timestamp      | Time exits                                    |
| Account equity | Risk controls                                 |

---

# Design Note for Automated Agents

Every exit rule can be abstracted as:

$Exit = f(MarketData_t, State_t, UserParams)$

This lets you modularize exit logic.

If building a trading agent, you should:

* Separate signal generation from exit logic
* Make exit modules composable
* Log trigger reason for post-trade analysis
* Store state variables explicitly ($P_max, ATR_at_entry, etc.$)

---

# PART 2 : **COMBINING MULTIPLE STRATEGIES**

Yes. Combining exits is not only possible — it’s standard practice.

Most real systems use **multiple concurrent exit rules**. The question isn’t whether you can combine them. The question is:

> How do you define precedence and interaction?

Below is the clean way to think about it.

---

# 1. Core Concept: Exit = OR of Independent Triggers

The standard architecture:

[
Exit = StopLoss ;; \lor ;; ProfitTarget ;; \lor ;; IndicatorSignal ;; \lor ;; TimeExit
]

In practice:

* If **any risk rule fires**, exit immediately.
* If **any profit rule fires**, exit.
* If **any signal rule fires**, exit.

This is almost always implemented as logical **OR**.

---

# 2. Your Specific Example

You already have:

RSI Exit:
[
RSI_t \ge 80
]

You want to add:

Stop Loss:
[
P_t \le P_{entry}(1 - s)
]

Combined:

[
Exit = (RSI_t \ge 80) ;; \lor ;; (P_t \le P_{entry}(1 - s))
]

This is completely valid and very common.

---

# 3. Should You Use AND?

Almost never for stop losses.

Example AND logic:

[
Exit = (RSI_t \ge 80) ;; \land ;; (P_t < MA)
]

This delays exit and increases risk.

AND logic is usually reserved for:

* Entry filters
* Scaling logic
* Confirmations

For exits, OR is safer.

---

# 4. Separating Loss vs Gain Logic

Best practice:

### Loss Rules (Risk Layer)

These are always OR'd and highest priority:

* Fixed stop
* ATR stop
* Trailing stop
* Max drawdown

They are “hard stops”.

---

### Profit / Signal Rules (Alpha Layer)

* RSI overbought
* MACD cross
* Resistance hit
* Time decay

Also typically OR’d.

---

# 5. Recommended Architecture

Instead of Boolean spaghetti, structure it as:

```
RiskExit = any(risk_rules_triggered)
AlphaExit = any(signal_rules_triggered)

Exit = RiskExit OR AlphaExit
```

Then log:

* exit_reason = "risk" or "signal"

This helps evaluation.

---

# 6. Advanced Option: Hierarchical Priority

Sometimes you want:

1. If StopLoss → exit immediately.
2. Else if ProfitTarget → exit.
3. Else if RSI → exit.

That is ordered OR:

```
if stop_loss:
    exit(reason="risk")
elif profit_target:
    exit(reason="target")
elif rsi_signal:
    exit(reason="signal")
```

Still OR logic — but with classification.

---

# 7. More Advanced: Conditional Activation

You can also gate exits.

Example:

Only activate RSI exit after trade is profitable:

[
ActivateRSI = P_t > P_{entry}
]

[
Exit = StopLoss \lor (ActivateRSI \land RSIExit)
]

This prevents RSI from closing losing trades prematurely.

---

# 8. For Backtesting Cleanly

The cleanest abstraction:

[
Exit = f(MarketData, State, Params)
]

Where each rule returns:

* True/False
* Exit price
* Reason
* Priority

Then the engine chooses the first triggered by priority.

---

# 9. Professional Reality

Almost all profitable systems have:

* Hard stop (risk containment)
* Trailing stop (profit protection)
* Signal exit (edge decay)

All running simultaneously.

---

# 10. What You Should Avoid

* Deeply nested AND conditions
* Dynamic rule toggling without logging
* Optimizing combinations without cross-validation
* Overfitting exit parameters

Exit rules are extremely easy to overfit.

---

# 11. For Your AI / Agent System

Since you're building modular systems:

Make each exit rule:

```
class ExitRule:
    def check(self, market_data, state) -> ExitSignal:
        return {triggered, price, reason, priority}
```

Then combine with:

* OR logic
* Priority ordering
* Layer separation (risk vs alpha)

---

# Bottom Line

Yes — combine them.
Use OR logic.
Keep risk exits independent and always active.
Layer signal exits on top.
Track which rule fired.

---

If you want next level, I can show:

* A formal composable exit rule algebra
* How to measure marginal contribution of each exit
* Or how to optimize multi-exit systems without overfitting
