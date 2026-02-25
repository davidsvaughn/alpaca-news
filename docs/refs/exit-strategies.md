# **EXIT STRATEGIES**

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

$$
P_t \le P_{entry}(1 - s)
$$

**Market Data Inputs**

* Current price (P_t)

**State Variables**

* Entry price (P_{entry})

**User Parameters**

* Stop percentage (s)

---

## 2. Fixed % Take Profit

$$
P_t \ge P_{entry}(1 + r)
$$

**Market Data**

* (P_t)

**State**

* (P_{entry})

**User**

* Reward percentage (r)

---

## 3. Risk/Reward Target

$$
Target = P_{entry} + k \cdot (P_{entry} - P_{stop})
$$

**Market Data**

* (P_t)

**State**

* Entry price
* Initial stop price

**User**

* Risk multiple (k)

---

# SECTION 2 — Trailing Logic

---

## 4. Percent Trailing Stop

$$
P_t \le P_{max}(1 - \tau)
$$

**Market Data**

* (P_t)

**State**

* Highest price since entry (P_{max})

**User**

* Trail percent (\tau)

---

## 5. ATR Trailing Stop

$$
P_t \le P_{max} - k \cdot ATR_n
$$

**Market Data**

* (P_t)
* ATR value

**State**

* (P_{max})

**User**

* ATR period (n)
* Multiplier (k)

---

# SECTION 3 — Volatility-Based

---

## 6. ATR Fixed Stop

$$
P_t \le P_{entry} - k \cdot ATR_n
$$

**Market Data**

* (P_t)
* ATR

**State**

* (P_{entry})

**User**

* ATR period
* Multiplier

---

# SECTION 4 — Moving Average / Trend

---

## 7. Close Below Moving Average

$$
P_t < MA_n
$$

**Market Data**

* Closing prices

**Derived Market Data**

* Moving average (MA_n)

**User**

* Lookback period (n)

---

## 8. Moving Average Cross Exit

$$
MA_{short} < MA_{long}
$$

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

$$
RSI_t \ge \theta
$$

**Market Data**

* Historical prices

**Derived**

* RSI

**User**

* RSI period
* Threshold (\theta) (e.g., 70)

---

## 10. MACD Bearish Cross

Exit if:
$$
MACD_t < Signal_t
$$

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

# SECTION 6 — Volume / Intraday

---

## 11. VWAP Breakdown

$$
P_t < VWAP_t
$$

**Market Data**

* Intraday trade price
* Intraday volume

**Derived**

* VWAP

**User**

* Session definition

---

## 12. Volume Fade Exit

Exit if:
$$
Volume_t < \alpha \cdot AvgVolume
$$

**Market Data**

* Volume

**Derived**

* Moving average volume

**User**

* Volume lookback
* Multiplier (\alpha)

---

# SECTION 7 — Statistical / Model-Based

---

## 13. Expected Return Threshold Exit

$$
E[R_{future} | X_t] < \theta
$$

**Market Data**

* Feature vector (X_t)

**Derived**

* Model prediction

**User**

* Threshold (\theta)
* Forecast horizon

---

# SECTION 8 — Time-Based

---

## 14. Max Holding Period

$$
t - t_{entry} \ge T
$$

**Market Data**

* Current timestamp

**State**

* Entry timestamp

**User**

* Max duration (T)

---

# SECTION 9 — Account-Level Controls

---

## 15. Max Daily Loss

Exit and halt if:
$$
DailyPnL \le -D
$$

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

$$
MA_n = \frac{1}{n} \sum_{i=0}^{n-1} P_{t-i}
$$

**Inputs**

* Historical closing prices
* Period (n)

---

## A2. Exponential Moving Average (EMA)

$$
EMA_t = \alpha P_t + (1-\alpha) EMA_{t-1}
$$

$$
\alpha = \frac{2}{n+1}
$$

---

## A3. ATR (Average True Range)

Step 1: True Range

$$
TR_t = \max
\begin{cases}
High_t - Low_t \\
|High_t - Close_{t-1}| \\
|Low_t - Close_{t-1}|
\end{cases}
$$

Step 2: ATR

$$
ATR_n = \text{EMA of } TR_t
$$

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

$$
RS = \frac{AvgGain}{AvgLoss}
$$

5. RSI:

$$
RSI = 100 - \frac{100}{1 + RS}
$$

---

## A5. MACD

$$
MACD = EMA_{fast} - EMA_{slow}
$$

Signal line:

$$
Signal = EMA_{signal}(MACD)
$$

---

## A6. VWAP

$$
VWAP_t = \frac{\sum_{i=1}^{t} Price_i \cdot Volume_i}{\sum_{i=1}^{t} Volume_i}
$$

**Inputs**

* Intraday trade prices
* Intraday volume

Reset each session.

---

## A7. Rate of Change (ROC)

$$
ROC = \frac{P_t - P_{t-n}}{P_{t-n}}
$$

---

# Data Requirements Summary

| Data Type      | Needed For         |
| -------------- | ------------------ |
| Tick data      | VWAP               |
| OHLC bars      | ATR, RSI, MACD     |
| Volume         | VWAP, volume exits |
| Timestamp      | Time exits         |
| Account equity | Risk controls      |

---

# Design Note for Automated Agents

Every exit rule can be abstracted as:

$$
Exit = f(MarketData_t, State_t, UserParams)
$$

This lets you modularize exit logic.

If building a trading agent, you should:

* Separate signal generation from exit logic
* Make exit modules composable
* Log trigger reason for post-trade analysis
* Store state variables explicitly (P_max, ATR_at_entry, etc.)
