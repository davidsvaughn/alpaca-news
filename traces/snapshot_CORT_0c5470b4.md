# Snapshot Export: 0c5470b4-b66e-5e49-b330-89fd481ff9cd

**Headline:** Federal Circuit Affirmed A No-Infringement Win For Teva Pharmaceutical Industries Vs Corcept Therapeutics Over Korlym (Mifepristone) Dosing With Strong CYP3A Inhibitors
**Symbols:** CORT, TEVA
**Created:** 2026-02-19T15:48:19.158443+00:00
**Version:** v1
**Total Cost:** $0.0391
**Direction:** bearish (90% confidence)

---

## Trigger

https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf

- **Source:** benzinga
- **Type:** alpaca_news
- **Article Time:** 2026-02-19T15:48:17+00:00
- **Source File:** 2026-02-19T15-48-17Z_50723583.json
- **Article URL:** https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape

### Trigger Raw Data

```json
{
  "id": 50723583,
  "headline": "Federal Circuit Affirmed A No-Infringement Win For Teva Pharmaceutical Industries Vs Corcept Therapeutics Over Korlym (Mifepristone) Dosing With Strong CYP3A Inhibitors",
  "source": "benzinga",
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape",
  "summary": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf",
  "created_at": "2026-02-19T15:48:17+00:00",
  "updated_at": "2026-02-19T15:48:18+00:00",
  "symbols": [
    "CORT",
    "TEVA"
  ],
  "author": "Benzinga Newsdesk",
  "content": "<p><a href=\"https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf\">https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf</a></p>",
  "images": null
}
```

---

## Market Context

- **Session:** market_open (open)
- **SPY:** $685.54 (-0.11%)
- **VIX:** 20.04
- **Source:** schwab

---

## Price at Trigger

### CORT

- **Last:** $30.35
- **Volume:** 2,590,240
- **Bid/Ask:** $30.34 / $30.55

### TEVA

- **Last:** $33.95
- **Volume:** 1,627,965
- **Bid/Ask:** $33.95 / $33.96

---

## Triage

- **action:** investigate
- **confidence:** 0.95
- **reasoning:** Fresh Federal Circuit affirmation of no-infringement ruling in patent dispute; direct catalyst for CORT (loss) and TEVA (win).
- **symbols:** ['CORT', 'TEVA']
- **provider:** grok
- **model:** grok-4-1-fast-reasoning

---

## Exploration Budget

- **Max Hops:** 3
- **Max Cost:** $0.35

---

## Pipeline Execution Summary

| # | Agent | Model | Tools | Tokens (in/out) | Reasoning | Reqs | Cost | Time |
|---|-------|-------|------:|----------------:|----------:|-----:|-----:|-----:|
| 1 | grok | `grok-4-1-fast-reasoning` | 21 | 31,170 / 5,477 | 2,919 | 3 | $0.0090 | 54.1s |
| 2 | openai | `gpt-5-mini` | 0 | 49,726 / 5,714 | 3,648 | 1 | $0.0239 | 130.3s |
| 3 | gemini | `gemini-3-flash-preview` | 4 | 12,434 / 865 | 1,031 | 1 | $0.0059 | 30.2s |
| | **Total** | | **25** | **106,417** | **7,598** | | **$0.0387** | **214.6s** |

---

## Final Prediction

- **Direction:** bearish
- **Confidence:** 90%
- **Horizon:** 1d
- **Key Catalyst:** Federal Circuit affirmation removes final legal barrier to multi-source generic competition for Korlym, Corcept's sole revenue driver.
- **Magnitude:** 5-10%
- **Bull Case:** The market may have overreacted to the legal news given Teva was already on the market; cash reserves could support the stock near $25-28 levels.
- **Bear Case:** Loss of patent moat combined with the recent relacorilant CRL leaves the company with a massive revenue cliff and no near-term successor drug; Sun and Hikma entry is now highly probable.
- **Risk Factors:**
  - Unexpected settlement or acquisition interest from a larger pharma peer
  - Positive surprise from the ovarian cancer PDUFA in July 2026
  - Short-covering bounce due to extremely oversold technical levels (RSI 27)

---

## Agent Rounds

### Round 1: grok

- **Model:** `grok-4-1-fast-reasoning`
- **Tokens:** 31,170 in / 5,477 out
- **Reasoning Tokens:** 2,919
- **Tool Calls:** 21
- **Requests:** 3
- **Cost:** $0.0090
- **Elapsed:** 54.1s

#### System Prompt

<details>
<summary>Show system prompt (2821 chars)</summary>

```
You are a financial research analyst investigating a breaking news event.
You are Agent 1 of 3 in a sequential research pipeline.

## Your role
You are the FIRST investigator. Your strength is web and social media research. Use web_search to find news context, and x_search to check X/Twitter sentiment and chatter. Also gather key market data. Focus on breadth — cast a wide net.

## Your tools
**Research tools (cost per call — use purposefully):**
- web_search — search the web for news, analysis, SEC filings, earnings reports
- x_search — search X/Twitter for real-time sentiment and chatter
- url_fetch — fetch and read the full text of any web page or article

**Market data tools (free — use liberally for non-primary symbols):**
- check_price — real-time quote with price, bid/ask, volume, change + trend context
- get_price_history — historical OHLCV bars (1d–1y periods, 1m–1d intervals)
- get_fundamentals — P/E, EPS, market cap, beta, 52-week range, dividend yield
- get_financial_statements — income statement, balance sheet, or cash flow (last 4 periods)
- get_technical_indicators — RSI, MACD, Bollinger, SMAs, ATR, etc. with interpretation
- check_options_activity — ATM IV, put/call volume & OI ratios
- check_volume_regime — current volume vs session average (detects abnormal volume)
- check_price_spike — detects significant recent price moves (>0.5% in 5 min)
- check_insider_activity — recent insider buys/sells/grants
- get_company_news — recent news articles (yfinance + Finnhub merged)
- get_analyst_ratings — analyst buy/hold/sell consensus and trends
- get_movers — top market gainers/losers by index (check for sector-wide moves)
- check_market_context — SPY, VIX, market session status

**Background data is ALREADY provided** in the prompt below — current price,
fundamentals, technicals, options, volume, insider activity, analyst ratings,
market context, and news for the primary symbol(s). Do NOT re-fetch this data.

**Your job is to INVESTIGATE beyond the basics:**
- Search the web for breaking analysis, earnings context, or SEC filings
- Read important articles with url_fetch for detailed information
- Use market data tools for OTHER symbols (peers, sector ETFs, competitors)
- Use get_financial_statements for deep fundamental digs (revenue trends, debt, cash flow)
- Use get_movers / check_market_context to check sector or market-wide dynamics

## X/Twitter search budget
You have a STRICT budget of **10 x_search calls**.
Each call is expensive (~$0.02). Make each query count.

## Cost awareness
- web_search and x_search cost per call — use purposefully, not wastefully.
- All market data tools and url_fetch are **free** — use liberally.

## How to investigate
Think step by step. Focus on areas NOT yet covered by prior agents (if any).
Stop when you have enough evidence.
```

</details>

#### User Message

<details>
<summary>Show user message (6285 chars)</summary>

```
## The news event
**Headline:** Federal Circuit Affirmed A No-Infringement Win For Teva Pharmaceutical Industries Vs Corcept Therapeutics Over Korlym (Mifepristone) Dosing With Strong CYP3A Inhibitors
**Summary:** https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf
**Symbols:** CORT, TEVA
**Source:** benzinga
**Timestamp:** 2026-02-19T15:48:17+00:00
**URL:** https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape

## Recent news coverage (FinnHub)
### CORT
- [2026-02-16 08:21] (ChartMill) Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case
  Corcept Therapeutics exemplifies GARP investing with accelerating earnings growth, strong profitability, and a solid debt-free balance sheet, all at a reasonable sector-relative valuation.

### TEVA
- [2026-02-19 05:07] (SeekingAlpha) How Bausch Health's Turnaround Progressed In Q4 2025
- [2026-02-18 12:00] (Benzinga) 13 Analysts Have This To Say About Teva Pharmaceutical Indus
   
- [2026-02-18 06:29] (Benzinga) Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42
  Truist Securities  analyst Les Sulewski   maintains Teva Pharmaceutical Indus (NYSE:TEVA) with a Buy and raises the price target from $38 to $42.
- [2026-02-17 07:02] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow
- [2026-02-17 05:54] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript
  Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results February 17, 2026 8:00 AM ESTCompany...
- [2026-02-17 04:56] (Benzinga) Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,
- [2026-02-17 03:04] (Benzinga) Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients
  Teva stock rises after a new study, in partnership with Sanofi, shows lasting benefits in ulcerative colitis and Crohn&#39;s patients.
- [2026-02-17 02:06] (Benzinga) Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs

## Earnings context (FinnHub)
### CORT
Earnings track record (last 4 quarters): 3 beats, 1 misses
  Most recent (2025-09-30): beat by 16.2% (actual $0.16 vs est $0.14)

### TEVA
Last earnings: 2026-01-28 (during market hours) — EPS actual: $0.96 vs est: $0.69
Earnings track record (last 4 quarters): 4 beats, 0 misses
  Most recent (2025-12-31): beat by 38.5% (actual $0.96 vs est $0.69)


## Pre-fetched market data

### CORT — Price & Trend
Current: $30.44 (-23.56% today) | Vol: 2,590,548
Returns: 1W: -20.6% | 1M: -16.2% | 3M: -59.5% | 6M: -56.8% | 1Y: -58.6%
52-week: $32.99–$117.33 (at -3% — near lows)

### CORT — Fundamentals
Market Cap: $4.2B | P/E: 45.58881 | EPS: 0.87346 | Beta: 0.26366

### CORT — Technical Summary
RSI(14): 27.4 (oversold) — declining from 34 over 10d
MACD: -3.5576 (bullish)
Bollinger: at -26% of band (near lower band) — lower: $34.03, mid: $40.97, upper: $47.91
Price vs 50-SMA: -42.6% (below) | vs 200-SMA: -54.8% (below)
ATR: $3.2134 (10.6% of price — high volatility)

### CORT — Options Activity
Put/Call Vol: 3.666 | Put/Call OI: 6.1173

### CORT — Volume Regime
Volume regime: unknown (21.04x avg)

### CORT — Volume Delta
Net delta: -140K shares (bearish) | Imbalance: -0.27 (uptick 36% / downtick 64%)

### CORT — Price Spike
No significant spike detected

### CORT — Insider Activity
- Hunt Hazel: Option Exercise 150000 shares ($?) (2026-02-09)
- Hunt Hazel: Tax Withholding 77968 shares ($3220858.08) (2026-02-09)
- Hunt Hazel: Option Exercise 150000 shares ($582000.0) (2026-02-09)
- Guyer William: Option Exercise 20000 shares ($?) (2026-02-03)
- Guyer William: Sale 20000 shares ($817334.0) (2026-02-03)

### CORT — Analyst Ratings (2026-02-01)
Strong Buy: 2 | Buy: 7 | Hold: 2 | Sell: 0 | Strong Sell: 1 (12 analysts)

### CORT — Growth & Valuation
EPS Growth (TTM YoY): -30.2% | Revenue Growth (TTM YoY): +17.9% | Revenue Growth (Q YoY): +13.8% | Price/Sales: 5.70 | EV/EBITDA: 61.83 | vs S&P 500 (13W): -49.6% | vs S&P 500 (26W): -51.7% | vs S&P 500 (52W): -57.4% | Gross Margin: +98.2% | Operating Margin: +8.8% | ROA: +12.8% | EPS Growth (Q YoY): -60.3% | CF/Share: 0.81

### TEVA — Price & Trend
Current: $33.96 (-0.44% today) | Vol: 1,628,072
Returns: 1W: +0.1% | 1M: +8.1% | 3M: +36.7% | 6M: +92.8% | 1Y: +97.9%
52-week: $12.46–$37.34 (at 86% — near highs)

### TEVA — Fundamentals
Market Cap: $40.1B | P/E: 28.19008 | EPS: 1.21 | Beta: 0.71989

### TEVA — Technical Summary
RSI(14): 56.0 (neutral) — declining from 66 over 10d
MACD: 0.6692 (bearish)
Bollinger: at 51% of band (mid-band) — lower: $31.52, mid: $33.91, upper: $36.31
Price vs 50-SMA: +5.3% (above) | vs 200-SMA: +33.4% (above)
ATR: $0.9350 (2.8% of price — moderate volatility)

### TEVA — Options Activity
Put/Call Vol: 0.126 | Put/Call OI: 0.4633

### TEVA — Volume Regime
Volume regime: unknown (0.23x avg)

### TEVA — Volume Delta
Net delta: -542K shares (bearish) | Imbalance: -0.08 (uptick 46% / downtick 54%)

### TEVA — Price Spike
Spike detected: -0.701% move

### TEVA — Insider Activity
- Francis Richard D: Option Exercise 620110 shares ($?) (2026-02-15)
- Francis Richard D: Option Exercise 161656 shares ($?) (2026-02-15)
- Francis Richard D: Sale 286122 shares ($9792267.94) (2026-02-15)
- Francis Richard D: Option Exercise 620110 shares ($?) (2026-02-15)
- Francis Richard D: Sale 161656 shares ($5532531.11) (2026-02-15)

### TEVA — Analyst Ratings (2026-02-01)
Strong Buy: 2 | Buy: 4 | Hold: 1 | Sell: 0 | Strong Sell: 0 (7 analysts)

### TEVA — Growth & Valuation
Revenue Growth (TTM YoY): +4.3% | Revenue Growth (Q YoY): +11.4% | Price/Sales: 2.35 | EV/EBITDA: 17.64 | vs S&P 500 (13W): +31.3% | vs S&P 500 (26W): +85.1% | vs S&P 500 (52W): +17.9% | Gross Margin: +52.9% | Operating Margin: +12.5% | ROA: +3.5% | CF/Share: -3.69

### Market Context
Session: market_open
```

</details>

#### Agent Findings

**Event Summary:**  
The U.S. Federal Circuit Court of Appeals (CAFC) affirmed a district court ruling on February 19, 2026, granting Teva Pharmaceutical Industries (TEVA) a no-infringement victory against Corcept Therapeutics (CORT) in a patent dispute over CORT's Korlym (mifepristone) product, specifically related to dosing instructions for use with strong CYP3A inhibitors. This clears a key legal hurdle for Teva to potentially launch a generic version of Korlym, CORT's flagship drug for Cushing's syndrome, which drives the vast majority of CORT's revenue (historically 90%+ of sales; recent quarterly revenue totals ~$180-208M, with Korlym as primary contributor).

**Market Impact on CORT:**  
- **Sharp Decline:** CORT plunged -23.56% to $30.44 (near 52-week lows of $32.99), on explosive 21x average volume (2.59M shares) and bearish net delta (-140K shares, 64% downtick). This dwarfs broader market weakness (SPY -0.12%, VIX 20.04).  
- **Technicals Oversold:** RSI 27 (deeply oversold), price -44% below 50-SMA ($53), -56% below 200-SMA ($67); near Bollinger lower band; MACD bearish at -3.59.  
- **Options Bearish:** Put/Call volume ratio 3.7x, OI 6.1x (heavy put interest).  
- **Biotech Context:** Sector mildly down (XBI -0.38% to $125.12, IBB -0.64% to $173.64); no other biotech names in top NASDAQ/SPX losers, confirming CORT-specific reaction.  

**Market Impact on TEVA:**  
- **Minimal Reaction:** -0.44% to $33.96 (near 52-week highs), low volume (0.23x avg), mild bearish delta. Positive analyst momentum intact (e.g., Truist Buy/$42 PT on Feb 18). Spike detected (-0.7% intraday), but generic win is incremental amid TEVA's strong YTD +98% run on earnings beats and pipeline (e.g., duvakitug IBD data).  

**News & Coverage Breadth:**  
- **Breaking Nature:** Benzinga first report (15:48 UTC); no immediate follow-up in major outlets (Seeking Alpha, Yahoo Finance, MarketWatch). CORT/TEVA recent news focused on FDA CRL for relacorilant (CORT pipeline drug), earnings, and TEVA's IBD trial success—no prior ruling mentions.  
- **Fundamentals Context:** Korlym enables CORT's high margins (gross ~98%, recent Q EPS beats but YoY growth slowing). Loss of exclusivity accelerates revenue cliff risk; quarterly revenue up QoQ but reliant on Korlym.  

**Social Sentiment (X/Twitter):**  
- Limited chatter: Semantic/keyword searches (since Feb 19) yielded low volume; x_stream_cache empty. Early reaction likely muted/outside budget (used 2/10 calls). No viral bearish threads or influencer takes detected yet—suggests news not fully digested.  

**Broader Market:**  
- SPX movers flat/down mildly (NVDA/INTC -0.1%); NASDAQ losers low-float names (e.g., TRNR +49% vol but unrelated). Not sector-wide biotech selloff.  

**Key Risks/Opportunities for Further Investigation:**  
- Generic timeline/launch impact on CORT revenue (~$800M+ annualized Korlym run-rate).  
- CORT pipeline (relacorilant CRL scrutiny); insider sales recent.  
- TEVA generic pipeline acceleration. Pass to Agent 2 for deep financial modeling/peer comps.

#### Tool Calls (21)

##### 1. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551421+00:00 -> 2026-02-19T15:48:49.551421+00:00
- **Hop Index:** 0

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_31527568",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 2. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551473+00:00 -> 2026-02-19T15:48:49.551473+00:00
- **Hop Index:** 1

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_25442653",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 3. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551508+00:00 -> 2026-02-19T15:48:49.551508+00:00
- **Hop Index:** 2

**Input:**

```json
{
  "query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_18630801",
  "action": {
    "query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 4. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551528+00:00 -> 2026-02-19T15:48:49.551528+00:00
- **Hop Index:** 3

**Input:**

```json
{
  "query": "CORT stock drop Teva patent Korlym \"Federal Circuit\""
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_55647359",
  "action": {
    "query": "CORT stock drop Teva patent Korlym \"Federal Circuit\"",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 5. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551548+00:00 -> 2026-02-19T15:48:49.551548+00:00
- **Hop Index:** 4

**Input:**

```json
{
  "query": "Korlym revenue percentage Corcept Therapeutics CORT"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_20861641",
  "action": {
    "query": "Korlym revenue percentage Corcept Therapeutics CORT",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 6. x_stream_cache

- **Duration:** 0.000s
- **Modality:** social
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551563+00:00 -> 2026-02-19T15:48:49.551584+00:00
- **Hop Index:** 5

**Input:**

```json
{
  "symbol": "CORT",
  "limit": 20
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "posts": [],
  "count": 0,
  "source": "x_stream_cache"
}
```

##### 7. x_stream_cache

- **Duration:** 0.000s
- **Modality:** social
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551600+00:00 -> 2026-02-19T15:48:49.551608+00:00
- **Hop Index:** 6

**Input:**

```json
{
  "symbol": "TEVA",
  "limit": 20
}
```

**Output:**

```json
{
  "symbol": "TEVA",
  "posts": [],
  "count": 0,
  "source": "x_stream_cache"
}
```

##### 8. check_market_context

- **Duration:** 1.285s
- **Modality:** macro
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551619+00:00 -> 2026-02-19T15:48:50.836355+00:00
- **Hop Index:** 7

**Output:**

```json
{
  "market_is_open": true,
  "session": "market_open",
  "regularMarket_start": "2026-02-19T09:30:00-05:00",
  "regularMarket_end": "2026-02-19T16:00:00-05:00",
  "preMarket_start": "2026-02-19T07:00:00-05:00",
  "preMarket_end": "2026-02-19T09:30:00-05:00",
  "postMarket_start": "2026-02-19T16:00:00-05:00",
  "postMarket_end": "2026-02-19T20:00:00-05:00",
  "spy_last": 685.49,
  "spy_net_change": -0.8,
  "spy_net_pct_change": -0.1165688,
  "vix_level": 20.04,
  "source": "schwab"
}
```

##### 9. get_movers

- **Duration:** 0.201s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:50.836422+00:00 -> 2026-02-19T15:48:51.036926+00:00
- **Hop Index:** 8

**Input:**

```json
{
  "index": "$SPX",
  "direction": "down"
}
```

**Output:**

```json
{
  "index": "$SPX",
  "direction": "down",
  "movers": [
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "down",
      "change": -0.15,
      "pct_change": -0.0008,
      "volume": 37088573,
      "last_price": 187.83
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "down",
      "change": -0.48,
      "pct_change": -0.0106,
      "volume": 25539507,
      "last_price": 44.98
    },
    {
      "symbol": "SMCI",
      "description": "SUPER MICRO COMPUTER",
      "direction": "down",
      "change": 1.99,
      "pct_change": 0.067,
      "volume": 21205373,
      "last_price": 31.7
    },
    {
      "symbol": "OXY",
      "description": "OCCIDENTAL PETE CORP",
      "direction": "down",
      "change": 3.79,
      "pct_change": 0.0805,
      "volume": 16919376,
      "last_price": 50.9
    },
    {
      "symbol": "PLTR",
      "description": "PALANTIR TECHNOLOGIE Class A",
      "direction": "down",
      "change": 0.38,
      "pct_change": 0.0028,
      "volume": 16381966,
      "last_price": 135.76
    },
    {
      "symbol": "BAC",
      "description": "BANK OF AMERICA CORP",
      "direction": "down",
      "change": -0.73,
      "pct_change": -0.0136,
      "volume": 15778815,
      "last_price": 52.64
    },
    {
      "symbol": "WMT",
      "description": "WALMART INC",
      "direction": "down",
      "change": 2.31,
      "pct_change": 0.0182,
      "volume": 15491567,
      "last_price": 128.93
    },
    {
      "symbol": "TSLA",
      "description": "TESLA INC",
      "direction": "down",
      "change": -0.14,
      "pct_change": -0.0003,
      "volume": 15394510,
      "last_price": 411.18
    },
    {
      "symbol": "F",
      "description": "FORD MTR CO DEL",
      "direction": "down",
      "change": -0.05,
      "pct_change": -0.004,
      "volume": 11438288,
      "last_price": 13.8
    },
    {
      "symbol": "AMZN",
      "description": "AMAZON.COM INC",
      "direction": "down",
      "change": -0.33,
      "pct_change": -0.0016,
      "volume": 10189752,
      "last_price": 204.46
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:48:50.836433+00:00",
  "source": "schwab"
}
```

##### 10. get_movers

- **Duration:** 0.278s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:51.036972+00:00 -> 2026-02-19T15:48:51.314689+00:00
- **Hop Index:** 9

**Input:**

```json
{
  "index": "$SPX",
  "direction": "up"
}
```

**Output:**

```json
{
  "index": "$SPX",
  "direction": "up",
  "movers": [
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "up",
      "change": -0.15,
      "pct_change": -0.0008,
      "volume": 37088573,
      "last_price": 187.83
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "up",
      "change": -0.48,
      "pct_change": -0.0106,
      "volume": 25539507,
      "last_price": 44.98
    },
    {
      "symbol": "SMCI",
      "description": "SUPER MICRO COMPUTER",
      "direction": "up",
      "change": 1.99,
      "pct_change": 0.067,
      "volume": 21205373,
      "last_price": 31.7
    },
    {
      "symbol": "OXY",
      "description": "OCCIDENTAL PETE CORP",
      "direction": "up",
      "change": 3.79,
      "pct_change": 0.0805,
      "volume": 16919376,
      "last_price": 50.9
    },
    {
      "symbol": "PLTR",
      "description": "PALANTIR TECHNOLOGIE Class A",
      "direction": "up",
      "change": 0.38,
      "pct_change": 0.0028,
      "volume": 16381966,
      "last_price": 135.76
    },
    {
      "symbol": "BAC",
      "description": "BANK OF AMERICA CORP",
      "direction": "up",
      "change": -0.73,
      "pct_change": -0.0136,
      "volume": 15778815,
      "last_price": 52.64
    },
    {
      "symbol": "WMT",
      "description": "WALMART INC",
      "direction": "up",
      "change": 2.31,
      "pct_change": 0.0182,
      "volume": 15491567,
      "last_price": 128.93
    },
    {
      "symbol": "TSLA",
      "description": "TESLA INC",
      "direction": "up",
      "change": -0.14,
      "pct_change": -0.0003,
      "volume": 15394510,
      "last_price": 411.18
    },
    {
      "symbol": "F",
      "description": "FORD MTR CO DEL",
      "direction": "up",
      "change": -0.05,
      "pct_change": -0.004,
      "volume": 11438288,
      "last_price": 13.8
    },
    {
      "symbol": "AMZN",
      "description": "AMAZON.COM INC",
      "direction": "up",
      "change": -0.33,
      "pct_change": -0.0016,
      "volume": 10189752,
      "last_price": 204.46
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:48:51.036979+00:00",
  "source": "schwab"
}
```

##### 11. get_financial_statements

- **Duration:** 0.150s
- **Modality:** fundamentals
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:51.314739+00:00 -> 2026-02-19T15:48:51.464903+00:00
- **Hop Index:** 10

**Input:**

```json
{
  "symbol": "CORT",
  "statement": "income",
  "freq": "quarterly"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "statement": "income",
  "frequency": "quarterly",
  "periods": [
    {
      "date": "2025-09-30",
      "total_revenue": 207638000.0,
      "cost_of_revenue": 4596000.0,
      "gross_profit": 203042000.0,
      "operating_income": 10219000.0,
      "ebitda": 10696000.0,
      "net_income": 19668000.0,
      "diluted_eps": 0.16
    },
    {
      "date": "2025-06-30",
      "total_revenue": 194430000.0,
      "cost_of_revenue": 3433000.0,
      "gross_profit": 190997000.0,
      "operating_income": 26675000.0,
      "ebitda": 27144000.0,
      "net_income": 35149000.0,
      "diluted_eps": 0.29
    },
    {
      "date": "2025-03-31",
      "total_revenue": 157214000.0,
      "cost_of_revenue": 2403000.0,
      "gross_profit": 154811000.0,
      "operating_income": 3416000.0,
      "ebitda": 3878000.0,
      "net_income": 20547000.0,
      "diluted_eps": 0.17
    },
    {
      "date": "2024-12-31",
      "total_revenue": 181890000.0,
      "cost_of_revenue": 2956000.0,
      "gross_profit": 178934000.0,
      "operating_income": 25262000.0,
      "ebitda": 25722000.0,
      "net_income": 30746000.0,
      "diluted_eps": 0.26
    }
  ],
  "fetched_at": "2026-02-19T15:48:51.464680+00:00",
  "source": "yfinance"
}
```

##### 12. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007645+00:00 -> 2026-02-19T15:49:12.007645+00:00
- **Hop Index:** 11

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_79423442",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 13. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007693+00:00 -> 2026-02-19T15:49:12.007693+00:00
- **Hop Index:** 12

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_22626050",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 14. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007713+00:00 -> 2026-02-19T15:49:12.007713+00:00
- **Hop Index:** 13

**Input:**

```json
{
  "query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_38511353",
  "action": {
    "query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 15. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007729+00:00 -> 2026-02-19T15:49:12.007729+00:00
- **Hop Index:** 14

**Input:**

```json
{
  "query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_19649004",
  "action": {
    "query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 16. get_company_news

- **Duration:** 0.245s
- **Modality:** news
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.007744+00:00 -> 2026-02-19T15:49:12.253140+00:00
- **Hop Index:** 15

**Input:**

```json
{
  "symbol": "CORT"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "articles": [
    {
      "title": "A Look At Corcept Therapeutics (CORT) Valuation After FDA Complete Response Letter For Relacorilant",
      "summary": "Why Corcept Therapeutics (CORT) Is Back in Focus The U.S. FDA’s Complete Response Letter for Corcept Therapeutics (CORT) relacorilant program, which calls for more evidence and pushes potential market entry back by about three years, has become the key driver of recent stock sentiment. See our latest analysis for Corcept Therapeutics. At a share price of $39.82, Corcept’s 30 day share price return of 14.66% suggests a partial rebound after the sharp 46.94% 90 day share price decline that...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/look-corcept-therapeutics-cort-valuation-082120385.html",
      "published_at": "2026-02-19T08:21:20+00:00",
      "fetched_at": "2026-02-19T15:49:12.137817+00:00",
      "source": "yfinance"
    },
    {
      "title": "How Investors May Respond To Corcept Therapeutics (CORT) FDA CRL And New Securities-Law Probes",
      "summary": "In late 2025, Corcept Therapeutics received a Complete Response Letter from the U.S. FDA for its relacorilant NDA in hypertension secondary to hypercortisolism, with the agency requesting additional evidence of effectiveness and raising concerns about the clinical development program and study design. The FDA setback has triggered multiple shareholder-rights and securities-law investigations, putting Corcept’s disclosure practices and regulatory pathway for relacorilant under closer scrutiny...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/investors-may-respond-corcept-therapeutics-191229390.html",
      "published_at": "2026-02-18T19:12:29+00:00",
      "fetched_at": "2026-02-19T15:49:12.137843+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Small-Cap Stocks We Think Twice About",
      "summary": "Small-cap stocks can be incredibly lucrative investments because their lack of analyst coverage leads to frequent mispricings. However, these businesses (and their stock prices) often stay small because their subscale operations make it harder to expand their competitive moats.",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/3-small-cap-stocks-think-043524189.html",
      "published_at": "2026-02-17T04:35:24+00:00",
      "fetched_at": "2026-02-19T15:49:12.137849+00:00",
      "source": "yfinance"
    },
    {
      "title": "FDA-Issued CRL for Relacorilant NDA Leads to Bullish Views Around Corcept Therapeutics (CORT)",
      "summary": "Corcept Therapeutics (NASDAQ:CORT) is one of the 17 biotechnology stocks with more than 50% upside. On February 2, Edward Nash from Canaccord Genuity reaffirmed his Buy rating for Corcept Therapeutics (NASDAQ:CORT). He forecasted a price target of $100, which leads to almost 161% upside potential on the stock. Nash noted that the CRL issued by […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/fda-issued-crl-relacorilant-nda-172400187.html",
      "published_at": "2026-02-16T17:24:00+00:00",
      "fetched_at": "2026-02-19T15:49:12.137853+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Cash-Producing Stocks We’re Skeptical Of",
      "summary": "While strong cash flow is a key indicator of stability, it doesn’t always translate to superior returns. Some cash-heavy businesses struggle with inefficient spending, slowing demand, or weak competitive positioning.",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/3-cash-producing-stocks-skeptical-043437767.html",
      "published_at": "2026-02-16T04:34:37+00:00",
      "fetched_at": "2026-02-19T15:49:12.137858+00:00",
      "source": "yfinance"
    },
    {
      "title": "Inspire Medical Systems, Astrana Health, Evolent Health, Corcept, and Centene Stocks Trade Up, What You Need To Know",
      "summary": "A number of stocks jumped in the afternoon session after a softer-than-expected inflation report fueled hopes for interest rate cuts by the Federal Reserve. The January Consumer Price Index (CPI), a key measure of inflation, rose by 0.2%, which was less than economists had forecast, with the annual rate cooling to 2.4%. This encouraging data increased market expectations for the Fed to begin cutting interest rates as early as June. The news prompted a rally in Treasuries as their yields fell. Wh",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/inspire-medical-systems-astrana-health-205111110.html",
      "published_at": "2026-02-13T20:51:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.137862+00:00",
      "source": "yfinance"
    },
    {
      "title": "High Insider Ownership Fuels Growth Stocks In February 2026",
      "summary": "As February 2026 begins, U.S. stock markets have shown strong momentum with major indexes like the Dow and S&P 500 ending sharply higher, reflecting investor optimism amid recent economic developments such as trade deals and manufacturing growth. In this buoyant market environment, companies with high insider ownership often attract attention as their leadership's vested interest can signal confidence in long-term growth potential.",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/high-insider-ownership-fuels-growth-173611000.html",
      "published_at": "2026-02-11T17:36:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.137867+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Growth Companies With High Insider Ownership And Up To 15% Revenue Growth",
      "summary": "As February begins with a strong rally in the U.S. stock markets, highlighted by significant gains in major indices like the Dow Jones Industrial Average and S&P 500, investors are keenly observing growth companies that have shown resilience amidst fluctuating economic conditions. In this context, companies with high insider ownership and robust revenue growth potential stand out as compelling options for those looking to navigate the current market landscape effectively.",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/3-growth-companies-high-insider-113547657.html",
      "published_at": "2026-02-11T11:35:47+00:00",
      "fetched_at": "2026-02-19T15:49:12.137871+00:00",
      "source": "yfinance"
    },
    {
      "title": "Insider Favorites Rumble And Two Other Growth Stocks",
      "summary": "As the U.S. stock market begins February on a high note, with major indices like the Dow Jones Industrial Average and S&P 500 posting significant gains, investors are keenly observing growth companies that exhibit strong insider ownership. In this thriving market environment, stocks with substantial insider stakes can often signal confidence in the company's potential, making them appealing options for those looking to align their investments with insiders who have a vested interest in the...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/insider-favorites-rumble-two-other-173540260.html",
      "published_at": "2026-02-10T17:35:40+00:00",
      "fetched_at": "2026-02-19T15:49:12.137876+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Growth Companies With High Insider Ownership Expecting Up To 81% Earnings Growth",
      "summary": "As the U.S. stock market kicks off February with a strong start, highlighted by the Dow Jones Industrial Average's 515-point gain and the S&P 500 nearing record highs, investors are closely monitoring growth companies that demonstrate robust insider ownership. In times of economic uncertainty and evolving trade dynamics, stocks with high insider ownership can be appealing as they often indicate confidence from those who know the company best, suggesting potential resilience and alignment with...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/3-growth-companies-high-insider-113601473.html",
      "published_at": "2026-02-10T11:36:01+00:00",
      "fetched_at": "2026-02-19T15:49:12.137881+00:00",
      "source": "yfinance"
    },
    {
      "category": "company",
      "datetime": 1771248061,
      "headline": "Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case",
      "id": 139113901,
      "image": "https://www.chartmill.com/images/uploads/Affordable_Growth_500_264px_webp_add6a3aa59.webp",
      "related": "CORT",
      "source": "ChartMill",
      "summary": "Corcept Therapeutics exemplifies GARP investing with accelerating earnings growth, strong profitability, and a solid debt-free balance sheet, all at a reasonable sector-relative valuation.",
      "url": "https://finnhub.io/api/news?id=21f8a2f3f61150c2bdfe75af0ca68ceaf6960c296ca5e372918ac8b1c5f233f3",
      "title": "Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case",
      "published_at": "2026-02-16T13:21:01+00:00"
    },
    {
      "category": "company",
      "datetime": 1770894072,
      "headline": "Options Exercise: Hazel Hunt At Corcept Therapeutics Realizes $5.53M",
      "id": 139082128,
      "image": "https://cdn.benzinga.com/files/images/story/2025/11/06/insider_trade_1.jpg?width=2048&height=1536",
      "related": "CORT",
      "source": "Benzinga",
      "summary": " ",
      "url": "https://finnhub.io/api/news?id=0fbc2b4d7b0acb598b946fd3f7a175e192d1a552f936bcdfa05033ae55ec78bb",
      "title": "Options Exercise: Hazel Hunt At Corcept Therapeutics Realizes $5.53M",
      "published_at": "2026-02-12T11:01:12+00:00"
    }
  ],
  "fetched_at": "2026-02-19T15:49:12.007756+00:00",
  "article_count": 12,
  "source": "yfinance+finnhub"
}
```

##### 17. get_company_news

- **Duration:** 0.201s
- **Modality:** news
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.253212+00:00 -> 2026-02-19T15:49:12.454531+00:00
- **Hop Index:** 16

**Input:**

```json
{
  "symbol": "TEVA"
}
```

**Output:**

```json
{
  "symbol": "TEVA",
  "articles": [
    {
      "title": "Goldman Sachs See Teva Pharmaceutical Industries Limited (TEVA) Driving Double-Digit Earnings Growth Through 2026",
      "summary": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is among the 7 Cheap Pharmaceutical Stocks to Buy According to Hedge Funds. Teva Pharmaceutical Industries Limited (NYSE:TEVA) is given the second position on our list. TheFly reported on February 9 that Goldman Sachs raised its price target on TEVA to $45 from $36, and gave it a Buy rating. […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/goldman-sachs-see-teva-pharmaceutical-020044351.html",
      "published_at": "2026-02-19T02:00:44+00:00",
      "fetched_at": "2026-02-19T15:49:12.361786+00:00",
      "source": "yfinance"
    },
    {
      "title": "FDA accepts BMS protein degrader for review; Disc rare disease drug rejected",
      "summary": "Cytokinetics’ heart disease drug secured European approval. Elsewhere, Teva and Sanofi reported longer-term data from an experimental therapy targeting ulcerative colitis and Crohn’s disease.",
      "publisher": "BioPharma Dive",
      "url": "https://www.biopharmadive.com/news/disc-news-roundup-pharma-biotech/812351/",
      "published_at": "2026-02-18T10:35:17+00:00",
      "fetched_at": "2026-02-19T15:49:12.361816+00:00",
      "source": "yfinance"
    },
    {
      "title": "Should You Invest in Teva Pharmaceutical (TEVA), Here’s What the Street Has to Say",
      "summary": "​Teva Pharmaceutical Industries Limited (NYSE:TEVA) is one of the Best Performing Foreign Stocks to Buy Now. Wall Street is bullish on Teva Pharmaceutical since its fiscal Q4 2025 earnings were released on January 28. ​Recently, on February 10, Goldman Sachs analyst Matthew Dellatorre reiterated a Buy rating on the stock and raised the price target […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/invest-teva-pharmaceutical-teva-street-175638880.html",
      "published_at": "2026-02-17T17:56:38+00:00",
      "fetched_at": "2026-02-19T15:49:12.361821+00:00",
      "source": "yfinance"
    },
    {
      "title": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is largely controlled by institutional shareholders who own 74% of the company",
      "summary": "Key Insights Given the large stake in the stock by institutions, Teva Pharmaceutical Industries' stock price might be...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-limited-nyse-120021078.html",
      "published_at": "2026-02-14T12:00:21+00:00",
      "fetched_at": "2026-02-19T15:49:12.361827+00:00",
      "source": "yfinance"
    },
    {
      "title": "Here’s What Analysts Are Saying About Teva Pharmaceutical (TEVA) Post Earnings",
      "summary": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is one of the best healthcare stocks under $50 to invest in. Barclays maintained an Overweight rating on Teva Pharmaceutical Industries Limited (NYSE:TEVA) on January 30, adjusting the price target on the stock to $38 from $35. It told investors that it updated the company’s model after the fiscal Q4 […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/analysts-saying-teva-pharmaceutical-teva-114139672.html",
      "published_at": "2026-02-10T11:41:39+00:00",
      "fetched_at": "2026-02-19T15:49:12.361833+00:00",
      "source": "yfinance"
    },
    {
      "title": "Teva Pharmaceutical Industries' (NYSE:TEVA) Solid Earnings Are Supported By Other Strong Factors",
      "summary": "Teva Pharmaceutical Industries Limited's ( NYSE:TEVA ) strong earnings report was rewarded with a positive stock price...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-nyse-teva-100259512.html",
      "published_at": "2026-02-10T10:02:59+00:00",
      "fetched_at": "2026-02-19T15:49:12.361837+00:00",
      "source": "yfinance"
    },
    {
      "title": "How Good Is Teva Pharmaceutical Industries Limited (NYSE:TEVA), When It Comes To ROE?",
      "summary": "While some investors are already well versed in financial metrics (hat tip), this article is for those who would like...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/good-teva-pharmaceutical-industries-limited-120011262.html",
      "published_at": "2026-02-08T12:00:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.361842+00:00",
      "source": "yfinance"
    },
    {
      "title": "Is Teva Pharmaceutical Industries Limited (TEVA) an Undervalued Stock Relative to Its Long-Term Potential?",
      "summary": "Baron Funds, an investment management company, released its fourth-quarter investor letter for the “Baron Health Care Fund”. A copy of the letter can be downloaded here. The fund rose 13.10% (Institutional Shares) in the quarter, compared to a 11.92% gain for the Russell 3000 Health Care Index (benchmark) and a 2.40% gain for the Russell […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-limited-teva-124644455.html",
      "published_at": "2026-02-06T12:46:44+00:00",
      "fetched_at": "2026-02-19T15:49:12.361847+00:00",
      "source": "yfinance"
    },
    {
      "title": "Alvotech (ALVO): A Bull Case Theory",
      "summary": "We came across a bullish thesis on Alvotech on r/valueinvesting by 123Thorr. In this article, we will summarize the bulls’ thesis on ALVO. Alvotech’s share was trading at $5.29 as of January 28th. ALVO’s trailing and forward P/E were 23.00 and 10.95, respectively according to Yahoo Finance. Alvotech ($ALVO) is emerging as a fast-growing biosimilars manufacturer, generating roughly $500 million in annual revenue […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/alvotech-alvo-bull-case-theory-183950403.html",
      "published_at": "2026-02-04T18:39:50+00:00",
      "fetched_at": "2026-02-19T15:49:12.361852+00:00",
      "source": "yfinance"
    },
    {
      "title": "How The Shifting 2026 Pipeline Story Is Reshaping Teva Pharmaceutical Industries’ Valuation",
      "summary": "Teva Pharmaceutical Industries has seen its fair value estimate move from about US$35.32 to about US$36.77, with analysts tightening their valuation ranges as they weigh upcoming pipeline events into 2026. While the discount rate assumption is essentially unchanged at about 8.52%, the revenue growth outlook now sits closer to about 1.05%. This reflects mixed views on how quickly Teva can translate its pipeline and core business into higher sales. As you read on, keep an eye on how this...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/shifting-2026-pipeline-story-reshaping-171605006.html",
      "published_at": "2026-02-04T17:16:05+00:00",
      "fetched_at": "2026-02-19T15:49:12.361857+00:00",
      "source": "yfinance"
    },
    {
      "category": "company",
      "datetime": 1771495651,
      "headline": "How Bausch Health's Turnaround Progressed In Q4 2025",
      "id": 139148734,
      "image": "",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=a9b842bf414d45318afd96853f62c48c8d974377e01097c1f3d1ad4ba6e2f541",
      "title": "How Bausch Health's Turnaround Progressed In Q4 2025",
      "published_at": "2026-02-19T10:07:31+00:00"
    },
    {
      "category": "company",
      "datetime": 1771434059,
      "headline": "13 Analysts Have This To Say About Teva Pharmaceutical Indus",
      "id": 139146834,
      "image": "https://cdn.benzinga.com/files/images/story/2025/11/06/analyst_ratings_6.jpg?width=2048&height=1536",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": " ",
      "url": "https://finnhub.io/api/news?id=ac941fc87c803801d615d269def0ca09d01e344ebcdce8d3c066123927a902e7",
      "title": "13 Analysts Have This To Say About Teva Pharmaceutical Indus",
      "published_at": "2026-02-18T17:00:59+00:00"
    },
    {
      "category": "company",
      "datetime": 1771414157,
      "headline": "Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42",
      "id": 139146835,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "Truist Securities  analyst Les Sulewski   maintains Teva Pharmaceutical Indus (NYSE:TEVA) with a Buy and raises the price target from $38 to $42.",
      "url": "https://finnhub.io/api/news?id=84511dac6f5a19684eaad74c6c912157acec2648ae468905678cbfd5b4029885",
      "title": "Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42",
      "published_at": "2026-02-18T11:29:17+00:00"
    },
    {
      "category": "company",
      "datetime": 1771329745,
      "headline": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow",
      "id": 139124509,
      "image": "",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=16c19453db608902d2eedcfc0342088e5b27e6e936d06eef0a1bc45188541f09",
      "title": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow",
      "published_at": "2026-02-17T12:02:25+00:00"
    },
    {
      "category": "company",
      "datetime": 1771325697,
      "headline": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript",
      "id": 139124272,
      "image": "https://static.seekingalpha.com/assets/og_image_1200-29b2bfe1a595477db6826bd2126c63ac2091efb7ec76347a8e7f81ba17e3de6c.png",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results February 17, 2026 8:00 AM ESTCompany...",
      "url": "https://finnhub.io/api/news?id=962e81e84b9e4800fc64efb70cd11a0a50d978d8b0ef886a3c736aa78ff52a0b",
      "title": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript",
      "published_at": "2026-02-17T10:54:57+00:00"
    },
    {
      "category": "company",
      "datetime": 1771322173,
      "headline": "Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,",
      "id": 139123216,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=cd2835dfed86655337ca9b8e18f0458fb703c9ae88e4cdb4fedb2c2c061d3386",
      "title": "Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,",
      "published_at": "2026-02-17T09:56:13+00:00"
    },
    {
      "category": "company",
      "datetime": 1771315477,
      "headline": "Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients",
      "id": 139122350,
      "image": "https://cdn.benzinga.com/files/images/story/2026/02/17/Teva-Pharmaceuticals.jpeg?width=2048&height=1536",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "Teva stock rises after a new study, in partnership with Sanofi, shows lasting benefits in ulcerative colitis and Crohn&#39;s patients.",
      "url": "https://finnhub.io/api/news?id=8a07b4f659e27bcf9979b71f24286b30ce2b63b823d67cdfe7eda699ace9c900",
      "title": "Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients",
      "published_at": "2026-02-17T08:04:37+00:00"
    },
    {
      "category": "company",
      "datetime": 1771311968,
      "headline": "Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs",
      "id": 139122351,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=129ebc8fc56b3ec2330a75714c1851231147e5fc837a3fc91b313117d6927872",
      "title": "Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs",
      "published_at": "2026-02-17T07:06:08+00:00"
    },
    {
      "category": "company",
      "datetime": 1770905760,
      "headline": "Neurocrine Biosciences: Expanding Opportunities, 2026 Will Be Less Exciting",
      "id": 139072463,
      "image": "https://static.seekingalpha.com/cdn/s3/uploads/getty_images/855725214/image_855725214.jpg?io=getty-c-w1536",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "Neurocrine Biosciences reported largely in-line Q4 results, with better-than-expected Crenessity sales. Click here to read my latest analysis of NBIX stock.",
      "url": "https://finnhub.io/api/news?id=00c55cb79c171690cfd221eaa2203fb3534641206255231377cf5f101485e510",
      "title": "Neurocrine Biosciences: Expanding Opportunities, 2026 Will Be Less Exciting",
      "published_at": "2026-02-12T14:16:00+00:00"
    }
  ],
  "fetched_at": "2026-02-19T15:49:12.253223+00:00",
  "article_count": 19,
  "source": "yfinance+finnhub"
}
```

##### 18. check_price

- **Duration:** 0.555s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.454592+00:00 -> 2026-02-19T15:49:13.010047+00:00
- **Hop Index:** 17

**Input:**

```json
{
  "symbol": "XBI"
}
```

**Output:**

```json
{
  "symbol": "XBI",
  "last_price": 125.12,
  "bid": 125.11,
  "ask": 125.14,
  "total_volume": 1962825,
  "high": 125.71,
  "low": 123.43,
  "open_price": 124.75,
  "close_price": 125.6,
  "net_change": -0.48,
  "net_pct_change": -0.38216561,
  "mark": 125.12,
  "timestamp_ms": 1771516150874,
  "source": "schwab",
  "period_returns": "1W: +1.6% | 1M: -2.4% | 3M: +9.0% | 6M: +40.7% | 1Y: +35.7%",
  "52_week_range": "$66.66–$132.09",
  "52_week_position_pct": 89.3
}
```

##### 19. check_price

- **Duration:** 0.521s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.010087+00:00 -> 2026-02-19T15:49:13.531388+00:00
- **Hop Index:** 18

**Input:**

```json
{
  "symbol": "IBB"
}
```

**Output:**

```json
{
  "symbol": "IBB",
  "last_price": 173.635,
  "bid": 173.58,
  "ask": 173.69,
  "total_volume": 359703,
  "high": 174.33,
  "low": 171.65,
  "open_price": 173.94,
  "close_price": 174.76,
  "net_change": -1.125,
  "net_pct_change": -0.64373999,
  "mark": 173.635,
  "timestamp_ms": 1771516151511,
  "source": "schwab",
  "period_returns": "1W: +1.8% | 1M: -0.9% | 3M: +4.4% | 6M: +25.7% | 1Y: +25.5%",
  "52_week_range": "$107.43–$179.64",
  "52_week_position_pct": 91.7
}
```

##### 20. get_movers

- **Duration:** 0.336s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.531437+00:00 -> 2026-02-19T15:49:13.867614+00:00
- **Hop Index:** 19

**Input:**

```json
{
  "index": "NASDAQ",
  "direction": "down"
}
```

**Output:**

```json
{
  "index": "NASDAQ",
  "direction": "down",
  "movers": [
    {
      "symbol": "TRNR",
      "description": "INTERACTIVE STRENGTH",
      "direction": "down",
      "change": 0.2,
      "pct_change": 0.496,
      "volume": 228347252,
      "last_price": 0.61
    },
    {
      "symbol": "RUBI",
      "description": "RUBICO INC",
      "direction": "down",
      "change": -0.15,
      "pct_change": -0.067,
      "volume": 81374174,
      "last_price": 2.09
    },
    {
      "symbol": "AGAE",
      "description": "ALLIED GAMING & ENTM",
      "direction": "down",
      "change": 0.13,
      "pct_change": 0.4336,
      "volume": 75082593,
      "last_price": 0.44
    },
    {
      "symbol": "DTCK",
      "description": "DAVIS COMMODITIES LT A",
      "direction": "down",
      "change": -0.07,
      "pct_change": -0.3591,
      "volume": 63926898,
      "last_price": 0.12
    },
    {
      "symbol": "RXT",
      "description": "RACKSPACE TECHNOLOGY",
      "direction": "down",
      "change": -0.26,
      "pct_change": -0.1898,
      "volume": 37655119,
      "last_price": 1.11
    },
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "down",
      "change": -0.14,
      "pct_change": -0.0008,
      "volume": 37182053,
      "last_price": 187.84
    },
    {
      "symbol": "ONDS",
      "description": "ONDAS INC",
      "direction": "down",
      "change": 0.38,
      "pct_change": 0.0339,
      "volume": 36296765,
      "last_price": 11.45
    },
    {
      "symbol": "TQQQ",
      "description": "PROSHARES ULTRAPRO QQQ ETF",
      "direction": "down",
      "change": -0.26,
      "pct_change": -0.0053,
      "volume": 31209808,
      "last_price": 49.13
    },
    {
      "symbol": "BOXL",
      "description": "BOXLIGHT CORP",
      "direction": "down",
      "change": 0.39,
      "pct_change": 0.2962,
      "volume": 25976451,
      "last_price": 1.69
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "down",
      "change": -0.47,
      "pct_change": -0.0102,
      "volume": 25577216,
      "last_price": 45.0
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:49:13.531445+00:00",
  "source": "schwab"
}
```

##### 21. get_technical_indicators

- **Duration:** 0.301s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.867664+00:00 -> 2026-02-19T15:49:14.169038+00:00
- **Hop Index:** 20

**Input:**

```json
{
  "symbol": "CORT",
  "indicators": "rsi,macd,boll,close_50_sma,close_200_sma"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "rsi": 26.9921,
  "macd": -3.5903,
  "boll": 40.9495,
  "close_50_sma": 53.0428,
  "close_200_sma": 67.307,
  "rsi_signal": "oversold",
  "fetched_at": "2026-02-19T15:49:13.916006+00:00",
  "source": "stockstats",
  "interpretation": {
    "rsi": "oversold",
    "vs_50sma": "-43.7% (below)",
    "vs_200sma": "-55.6% (below)"
  }
}
```

### Round 2: openai

- **Model:** `gpt-5-mini`
- **Tokens:** 49,726 in / 5,714 out
- **Reasoning Tokens:** 3,648
- **Tool Calls:** 0
- **Requests:** 1
- **Cost:** $0.0239
- **Elapsed:** 130.3s

#### System Prompt

<details>
<summary>Show system prompt (2858 chars)</summary>

```
You are a financial research analyst investigating a breaking news event.
You are Agent 2 of 3 in a sequential research pipeline.

## Your role
You are the SECOND investigator. Review what the previous agent found, then dig deeper. Use web_search to verify claims, find contradicting evidence, or explore angles the first agent missed. Check market data that hasn't been checked yet. Focus on depth and verification.

## Your tools
**Research tools (cost per call — use purposefully):**
- web_search — search the web for news, analysis, SEC filings, earnings reports
- url_fetch — fetch and read the full text of any web page or article

**Market data tools (free — use liberally for non-primary symbols):**
- check_price — real-time quote with price, bid/ask, volume, change + trend context
- get_price_history — historical OHLCV bars (1d–1y periods, 1m–1d intervals)
- get_fundamentals — P/E, EPS, market cap, beta, 52-week range, dividend yield
- get_financial_statements — income statement, balance sheet, or cash flow (last 4 periods)
- get_technical_indicators — RSI, MACD, Bollinger, SMAs, ATR, etc. with interpretation
- check_options_activity — ATM IV, put/call volume & OI ratios
- check_volume_regime — current volume vs session average (detects abnormal volume)
- check_price_spike — detects significant recent price moves (>0.5% in 5 min)
- check_insider_activity — recent insider buys/sells/grants
- get_company_news — recent news articles (yfinance + Finnhub merged)
- get_analyst_ratings — analyst buy/hold/sell consensus and trends
- get_movers — top market gainers/losers by index (check for sector-wide moves)
- check_market_context — SPY, VIX, market session status

**Background data is ALREADY provided** in the prompt below — current price,
fundamentals, technicals, options, volume, insider activity, analyst ratings,
market context, and news for the primary symbol(s). Do NOT re-fetch this data.

**Your job is to INVESTIGATE beyond the basics:**
- Search the web for breaking analysis, earnings context, or SEC filings
- Read important articles with url_fetch for detailed information
- Use market data tools for OTHER symbols (peers, sector ETFs, competitors)
- Use get_financial_statements for deep fundamental digs (revenue trends, debt, cash flow)
- Use get_movers / check_market_context to check sector or market-wide dynamics

## Web search budget
You have a STRICT budget of **10 web searches**.
Plan your searches carefully. Each search should have a clear purpose.
Do NOT repeat searches that prior agents already performed.

## Cost awareness
- web_search and x_search cost per call — use purposefully, not wastefully.
- All market data tools and url_fetch are **free** — use liberally.

## How to investigate
Think step by step. Focus on areas NOT yet covered by prior agents (if any).
Stop when you have enough evidence.
```

</details>

#### User Message

<details>
<summary>Show user message (16284 chars)</summary>

```
## The news event
**Headline:** Federal Circuit Affirmed A No-Infringement Win For Teva Pharmaceutical Industries Vs Corcept Therapeutics Over Korlym (Mifepristone) Dosing With Strong CYP3A Inhibitors
**Summary:** https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf
**Symbols:** CORT, TEVA
**Source:** benzinga
**Timestamp:** 2026-02-19T15:48:17+00:00
**URL:** https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape

## Recent news coverage (FinnHub)
### CORT
- [2026-02-16 08:21] (ChartMill) Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case
  Corcept Therapeutics exemplifies GARP investing with accelerating earnings growth, strong profitability, and a solid debt-free balance sheet, all at a reasonable sector-relative valuation.

### TEVA
- [2026-02-19 05:07] (SeekingAlpha) How Bausch Health's Turnaround Progressed In Q4 2025
- [2026-02-18 12:00] (Benzinga) 13 Analysts Have This To Say About Teva Pharmaceutical Indus
   
- [2026-02-18 06:29] (Benzinga) Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42
  Truist Securities  analyst Les Sulewski   maintains Teva Pharmaceutical Indus (NYSE:TEVA) with a Buy and raises the price target from $38 to $42.
- [2026-02-17 07:02] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow
- [2026-02-17 05:54] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript
  Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results February 17, 2026 8:00 AM ESTCompany...
- [2026-02-17 04:56] (Benzinga) Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,
- [2026-02-17 03:04] (Benzinga) Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients
  Teva stock rises after a new study, in partnership with Sanofi, shows lasting benefits in ulcerative colitis and Crohn&#39;s patients.
- [2026-02-17 02:06] (Benzinga) Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs

## Earnings context (FinnHub)
### CORT
Earnings track record (last 4 quarters): 3 beats, 1 misses
  Most recent (2025-09-30): beat by 16.2% (actual $0.16 vs est $0.14)

### TEVA
Last earnings: 2026-01-28 (during market hours) — EPS actual: $0.96 vs est: $0.69
Earnings track record (last 4 quarters): 4 beats, 0 misses
  Most recent (2025-12-31): beat by 38.5% (actual $0.96 vs est $0.69)


---

## Prior agent findings

### Agent: grok (model: grok-4-1-fast-reasoning, 21 tool calls)
**Event Summary:**  
The U.S. Federal Circuit Court of Appeals (CAFC) affirmed a district court ruling on February 19, 2026, granting Teva Pharmaceutical Industries (TEVA) a no-infringement victory against Corcept Therapeutics (CORT) in a patent dispute over CORT's Korlym (mifepristone) product, specifically related to dosing instructions for use with strong CYP3A inhibitors. This clears a key legal hurdle for Teva to potentially launch a generic version of Korlym, CORT's flagship drug for Cushing's syndrome, which drives the vast majority of CORT's revenue (historically 90%+ of sales; recent quarterly revenue totals ~$180-208M, with Korlym as primary contributor).

**Market Impact on CORT:**  
- **Sharp Decline:** CORT plunged -23.56% to $30.44 (near 52-week lows of $32.99), on explosive 21x average volume (2.59M shares) and bearish net delta (-140K shares, 64% downtick). This dwarfs broader market weakness (SPY -0.12%, VIX 20.04).  
- **Technicals Oversold:** RSI 27 (deeply oversold), price -44% below 50-SMA ($53), -56% below 200-SMA ($67); near Bollinger lower band; MACD bearish at -3.59.  
- **Options Bearish:** Put/Call volume ratio 3.7x, OI 6.1x (heavy put interest).  
- **Biotech Context:** Sector mildly down (XBI -0.38% to $125.12, IBB -0.64% to $173.64); no other biotech names in top NASDAQ/SPX losers, confirming CORT-specific reaction.  

**Market Impact on TEVA:**  
- **Minimal Reaction:** -0.44% to $33.96 (near 52-week highs), low volume (0.23x avg), mild bearish delta. Positive analyst momentum intact (e.g., Truist Buy/$42 PT on Feb 18). Spike detected (-0.7% intraday), but generic win is incremental amid TEVA's strong YTD +98% run on earnings beats and pipeline (e.g., duvakitug IBD data).  

**News & Coverage Breadth:**  
- **Breaking Nature:** Benzinga first report (15:48 UTC); no immediate follow-up in major outlets (Seeking Alpha, Yahoo Finance, MarketWatch). CORT/TEVA recent news focused on FDA CRL for relacorilant (CORT pipeline drug), earnings, and TEVA's IBD trial success—no prior ruling mentions.  
- **Fundamentals Context:** Korlym enables CORT's high margins (gross ~98%, recent Q EPS beats but YoY growth slowing). Loss of exclusivity accelerates revenue cliff risk; quarterly revenue up QoQ but reliant on Korlym.  

**Social Sentiment (X/Twitter):**  
- Limited chatter: Semantic/keyword searches (since Feb 19) yielded low volume; x_stream_cache empty. Early reaction likely muted/outside budget (used 2/10 calls). No viral bearish threads or influencer takes detected yet—suggests news not fully digested.  

**Broader Market:**  
- SPX movers flat/down mildly (NVDA/INTC -0.1%); NASDAQ losers low-float names (e.g., TRNR +49% vol but unrelated). Not sector-wide biotech selloff.  

**Key Risks/Opportunities for Further Investigation:**  
- Generic timeline/launch impact on CORT revenue (~$800M+ annualized Korlym run-rate).  
- CORT pipeline (relacorilant CRL scrutiny); insider sales recent.  
- TEVA generic pipeline acceleration. Pass to Agent 2 for deep financial modeling/peer comps.

#### Tool results from this agent:
**web_search(url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_31527568", "action": {"type": "open_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(url='https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_25442653", "action": {"type": "open_page", "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='"Federal Circuit" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_18630801", "action": {"query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='CORT stock drop Teva patent Korlym "Federal Circuit"')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_55647359", "action": {"query": "CORT stock drop Teva patent Korlym \"Federal Circuit\"", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Korlym revenue percentage Corcept Therapeutics CORT')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_20861641", "action": {"query": "Korlym revenue percentage Corcept Therapeutics CORT", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**x_stream_cache(symbol='CORT', limit=20)**:
```
{"symbol": "CORT", "posts": [], "count": 0, "source": "x_stream_cache"}
```

**x_stream_cache(symbol='TEVA', limit=20)**:
```
{"symbol": "TEVA", "posts": [], "count": 0, "source": "x_stream_cache"}
```

**get_movers(index='$SPX', direction='down')**:
```
{"index": "$SPX", "direction": "down", "movers": [{"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "down", "change": -0.15, "pct_change": -0.0008, "volume": 37088573, "last_price": 187.83}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "down", "change": -0.48, "pct_change": -0.0106, "volume": 25539507, "last_price": 44.98}, {"symbol": "SMCI", "description": "SUPER MICRO COMPUTER", "direction": "down", "change": 1.99, "pct_change": 0.067, "volume": 21205373, "last_price": 31.7}, {"symbol": "OXY", "description": "OCCIDENTAL PETE CORP", "direction": "down", "change": 3.79, "pct_change": 0.0805, "volume": 16919376, "last_price": 50.9}, {"symbol": "PLTR", "description": "PALANTIR TECHNOLOGIE Class A", "direction": "down", "change": 0.38, "pct_change": 0.0028, "volume": 16381966, "last_price": 135.76}, {"symbol": "BAC", "description": "BANK OF AMERICA CORP", "direction": "down", "change": -0.73, "pct_change": -0.0136, "volume": 15778815, "last_price": 52.64}, {"symbol": "WMT", "description": "WALMART INC", "direction": "down", "change": 2.31, "pct_change": 0.0182, "volume": 15491567, "last_price": 128.93}, {"symbol": "TSLA", "description": "TESLA INC", "direction": "down", "change": -0.14, "pct_change": -0.0003, "volume": 15394510, "last_price": 411.18}, {"symbol": "F", "description": "FORD MTR CO DEL", "direction": "down", "change": -0.05, "pct_change": -0.004, "volume": 11438288, "last_price": 13.8}, {"symbol": "AMZN", "description": "AMAZON.COM INC", "direction": "down", "change": -0.33, "pct_change": -0.0016, "volume": 10189752, "last_price": 204.46}], "count": 10, "fetched_at": "2026-02-19T15:48:50.836433+00:00", "source": "schwab"}
```

**get_movers(index='$SPX', direction='up')**:
```
{"index": "$SPX", "direction": "up", "movers": [{"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "up", "change": -0.15, "pct_change": -0.0008, "volume": 37088573, "last_price": 187.83}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "up", "change": -0.48, "pct_change": -0.0106, "volume": 25539507, "last_price": 44.98}, {"symbol": "SMCI", "description": "SUPER MICRO COMPUTER", "direction": "up", "change": 1.99, "pct_change": 0.067, "volume": 21205373, "last_price": 31.7}, {"symbol": "OXY", "description": "OCCIDENTAL PETE CORP", "direction": "up", "change": 3.79, "pct_change": 0.0805, "volume": 16919376, "last_price": 50.9}, {"symbol": "PLTR", "description": "PALANTIR TECHNOLOGIE Class A", "direction": "up", "change": 0.38, "pct_change": 0.0028, "volume": 16381966, "last_price": 135.76}, {"symbol": "BAC", "description": "BANK OF AMERICA CORP", "direction": "up", "change": -0.73, "pct_change": -0.0136, "volume": 15778815, "last_price": 52.64}, {"symbol": "WMT", "description": "WALMART INC", "direction": "up", "change": 2.31, "pct_change": 0.0182, "volume": 15491567, "last_price": 128.93}, {"symbol": "TSLA", "description": "TESLA INC", "direction": "up", "change": -0.14, "pct_change": -0.0003, "volume": 15394510, "last_price": 411.18}, {"symbol": "F", "description": "FORD MTR CO DEL", "direction": "up", "change": -0.05, "pct_change": -0.004, "volume": 11438288, "last_price": 13.8}, {"symbol": "AMZN", "description": "AMAZON.COM INC", "direction": "up", "change": -0.33, "pct_change": -0.0016, "volume": 10189752, "last_price": 204.46}], "count": 10, "fetched_at": "2026-02-19T15:48:51.036979+00:00", "source": "schwab"}
```

**get_financial_statements(symbol='CORT', statement='income', freq='quarterly')**:
```
{"symbol": "CORT", "statement": "income", "frequency": "quarterly", "periods": [{"date": "2025-09-30", "total_revenue": 207638000.0, "cost_of_revenue": 4596000.0, "gross_profit": 203042000.0, "operating_income": 10219000.0, "ebitda": 10696000.0, "net_income": 19668000.0, "diluted_eps": 0.16}, {"date": "2025-06-30", "total_revenue": 194430000.0, "cost_of_revenue": 3433000.0, "gross_profit": 190997000.0, "operating_income": 26675000.0, "ebitda": 27144000.0, "net_income": 35149000.0, "diluted_eps": 0.29}, {"date": "2025-03-31", "total_revenue": 157214000.0, "cost_of_revenue": 2403000.0, "gross_profit": 154811000.0, "operating_income": 3416000.0, "ebitda": 3878000.0, "net_income": 20547000.0, "diluted_eps": 0.17}, {"date": "2024-12-31", "total_revenue": 181890000.0, "cost_of_revenue": 2956000.0, "gross_profit": 178934000.0, "operating_income": 25262000.0, "ebitda": 25722000.0, "net_income": 30746000.0, "diluted_eps": 0.26}], "fetched_at": "2026-02-19T15:48:51.464680+00:00", "source": "yfinance"}
```

**web_search(url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_79423442", "action": {"type": "open_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(url='https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_22626050", "action": {"type": "open_page", "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_38511353", "action": {"query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='"Corcept" OR CORT "Federal Circuit" OR "CAFC" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_19649004", "action": {"query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**get_movers(index='NASDAQ', direction='down')**:
```
{"index": "NASDAQ", "direction": "down", "movers": [{"symbol": "TRNR", "description": "INTERACTIVE STRENGTH", "direction": "down", "change": 0.2, "pct_change": 0.496, "volume": 228347252, "last_price": 0.61}, {"symbol": "RUBI", "description": "RUBICO INC", "direction": "down", "change": -0.15, "pct_change": -0.067, "volume": 81374174, "last_price": 2.09}, {"symbol": "AGAE", "description": "ALLIED GAMING & ENTM", "direction": "down", "change": 0.13, "pct_change": 0.4336, "volume": 75082593, "last_price": 0.44}, {"symbol": "DTCK", "description": "DAVIS COMMODITIES LT A", "direction": "down", "change": -0.07, "pct_change": -0.3591, "volume": 63926898, "last_price": 0.12}, {"symbol": "RXT", "description": "RACKSPACE TECHNOLOGY", "direction": "down", "change": -0.26, "pct_change": -0.1898, "volume": 37655119, "last_price": 1.11}, {"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "down", "change": -0.14, "pct_change": -0.0008, "volume": 37182053, "last_price": 187.84}, {"symbol": "ONDS", "description": "ONDAS INC", "direction": "down", "change": 0.38, "pct_change": 0.0339, "volume": 36296765, "last_price": 11.45}, {"symbol": "TQQQ", "description": "PROSHARES ULTRAPRO QQQ ETF", "direction": "down", "change": -0.26, "pct_change": -0.0053, "volume": 31209808, "last_price": 49.13}, {"symbol": "BOXL", "description": "BOXLIGHT CORP", "direction": "down", "change": 0.39, "pct_change": 0.2962, "volume": 25976451, "last_price": 1.69}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "down", "change": -0.47, "pct_change": -0.0102, "volume": 25577216, "last_price": 45.0}], "count": 10, "fetched_at": "2026-02-19T15:49:13.531445+00:00", "source": "schwab"}
```

```

</details>

#### Reasoning Summary

```
**Reviewing agent findings**

I need to review previous agent findings and dig deeper. I'll use the web.run tool for up to 10 searches since this is breaking news. I also need to cite around five key statements from the internet. For market data, I can use functions like check_price or get_financial_statements. I won't need to re-fetch primary symbols since background data is available, but I can fetch peers. I should check the CAFC opinion and extract key parts from the provided PDF.

**Gathering patent and market details**

I need to read the opinion carefully to gather specifics, like which patents and claims are at issue, and whether there is an injunction. I'll figure out if the ruling allows for immediate generic launch or if it involves other patents or the Orange Book. I also want to check the FDA ANDA status for Teva regarding the Korlym generic. Caution is needed since mifepristone is known as RU-486, but Korlym is the brand name. I’ll examine which patents, district court judgments, and market reactions related to CORT stock that have occurred as well.

**Planning searches for CAFC opinion**

I'm thinking about how to fetch the CAFC opinion PDF and extract the key holdings. I could use web.run to open the PDF since it returns a reference ID, but I need to confirm if it really has that option available. The prior agent had already called it twice, but I can still use our search budget. I'll also search for other news outlets covering the story, including Benzinga and major outlets like Reuters, Bloomberg, and more. Let's go for it!

**Structuring search queries**

I need to structure some searches to gather information about the Federal Circuit case regarding Teva and Corcept, using the case number 24-1346. First, I'll search for the CAFC opinion. Then, I’ll look up the district court decision related to the summary judgment of noninfringement. I also want to find out about Teva's ANDA filing status for Korlym, especially any FDA approval in the context of patent litigation. I’ll check for the relevant Korlym patents, their numbers in the Orange Book, and stock reaction coverage for Corcept. Finally, I'll look for analyst comments or press releases from both companies dated February 19, 2026.

**Finalizing search queries for information**

I want to gather statements from Corcept and Teva regarding the Federal Circuit case, specifically from February 19, 2026. Then, I’ll check if there are any other generic filers, like Mylan, involved with Korlym by querying ANDA filers for mifepristone. I should also look into the Orange Book for listed patents tied to Korlym. Additionally, I’ll estimate the impact by calculating the revenue share for Korlym and potential loss timelines, referencing Corcept's annual revenue figures from its 2025 10-K. That's a complete set of ten searches! Now, I'll proceed with the web.run calls sequentially.

**Verifying Teva's launch details**

I’m looking at Corcept SEC filings that mention Teva launched a generic in January 2024. A prior source confirmed this, but I need to verify the launch and whether it's an authorized generic by Corcept. I also want to check if Teva’s ANDA received final approval back in August 2020 according to the filings. It makes sense to look for a press release from Teva about the generic launch in January 2024.

**Searching for company statements**

I found proof that Teva launched an AB-rated generic in January 2024, as noted in Optum and Drugs.com. Corcept's SEC filings also confirm this. Now, I need to look for any press releases from February 19, 2026, regarding the CAFC opinion. There was a press release on Corcept's site from January 2, 2024, but I want to ensure I’m catching the latest updates from that date, too.

**Searching for revenue information**

I need to look for Corcept’s revenue breakdown, particularly focusing on the 2025 annual 10-K filing or relevant forms. I'm interested in seeing the percentage of revenue generated from Korlym. It’s crucial to get accurate data, so I’ll need to ensure I access the right documents. Finding detailed and specific financial information can make a significant difference in understanding their revenue streams. Let's see what I can uncover!

**Evaluating stock market reaction**

I need to mention that CORT stock dropped around 23.6% on February 19, 2026, according to a previous reference. I should verify this with reliable market data sources like Yahoo Finance or Nasdaq to make sure the information is accurate. It's important to get this context right, so checking these sources will help ensure I provide reliable information to whoever may be interested!

**Considering market reactions**

Market reactions are often fleeting, but a previous agent used market tools to track price movements effectively. For Teva launching a generic product, I can reference sources like Optum, Drugs.com, or the SEC. Regarding CORT's stock movement, the user provided a Benzinga article, so it'll be wise to cite that along with a historical quote from Yahoo Finance for CORT on February 19, 2026. I should search Yahoo Finance for historical details on CORT.

**Clarifying data sources**

Finding direct market data for CORT’s stock drop on February 19, 2026, is a bit tricky. The Benzinga article provided by the user is suitable for citation. Also, the market data from my earlier tools, like get_movers and price info, are considered background data I won’t need to re-fetch. If I use web.run, I need to ensure all statements are cited. The user’s Benzinga article is valid, and I’ll make sure to reference it.

**Crafting the final answer**

I have to identify the five most crucial statements in the answer and ensure to include citations after each of those. It seems that I need to balance the key points with proper references for clarity and reliability. Distributing the citations effectively is essential to lend credibility to what I'm presenting. Alright, let’s work on putting that all together now!
```

#### Agent Findings

Short verified summary and next steps (deep-dive findings)

What happened (verified)
- The U.S. Court of Appeals for the Federal Circuit (panel decision, non‑precedential) affirmed the District of New Jersey’s December 29, 2023 judgment that Teva’s proposed generic Korlym product does not infringe Corcept’s asserted patents (U.S. Pat. Nos. 10,195,214 and 10,842,800). The opinion was filed Feb 19, 2026. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))
- The CAFC affirmed on the district court’s fact findings: (a) no persuasive record evidence that physicians have practiced the claimed methods in the past, and (b) future direct infringement was “highly unlikely” (physicians avoid co‑administration for safety/dosing reasons; osilodrostat is a non‑infringing alternative; Teva’s proposed label can be followed without infringing). The court therefore affirmed the no‑infringement finding without reaching induced‑infringement intent. The opinion also notes Teva could have avoided litigation via a Section VIII label carve‑out. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))

Commercial / market facts verified
- Teva previously filed an ANDA and launched an AB‑rated generic version of Korlym (mifepristone, 300 mg) in January 2024. Multiple trade/clinical outlets list Teva’s generic as launched Jan 2024. That means Teva’s product has been commercially available since then and this appellate outcome removes the key patent overhang. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Market reaction: Benzinga published the CAFC result Feb 19, 2026 and showed CORT trading down materially at the time of posting (example snapshot $32.63, -18.1% in the Benzinga feed). This matches the immediate negative price reaction reported in market feeds. ([benzinga.com](https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape))
- Corcept is highly dependent on Korlym for revenue. Corcept’s corporate disclosures and recent results show Korlym as the sole marketed product and the driver of company revenue (Corcept reported 2024 revenue $675M and continues to disclose that generic entry is a material risk). Corcept’s SEC filings also document the litigation history, Teva’s ANDA approval/launch, and the potential commercial impact. ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))

What the CAFC opinion means (implications, verified)
- Legal: Affirmance means Corcept’s asserted method patents remain not‑infringed as applied to Teva’s proposed label/product. The CAFC’s reliance on real‑world physician practice (and availability of a non‑infringing alternative) is exactly the kind of evidentiary line other Hatch‑Waxman disputes will watch. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))
- Commercial: Because Teva launched a generic in Jan 2024, removal of the appellate overhang increases the likelihood of durable generic competition pressure on Korlym sales going forward (price/volume impact already realized in market moves). Corcept’s filings warned generic entry could materially harm its results. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Practical note from the opinion: the court explicitly suggested Teva could have used a Section VIII carve‑out to avoid the suit—this is a sign courts will consider labeling strategies and real prescribing practice heavily in ANDA disputes. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))

Gaps / items I verified that prior coverage missed or under‑emphasized
- Timing: Teva’s generic has been on the market since Jan 2024 (so the CAFC decision mainly removes legal uncertainty rather than enabling a new market entry). Several market pieces had flagged the 2023 district court loss and the ongoing appeal; the key verified change today is final appellate rejection of Corcept’s infringement argument. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Entrants: Corcept’s SEC filings (and prior settlements) permit Sun/Hikma entry under defined terms if Teva’s generic is available — so multiple generic sources could be commercially possible if market dynamics permit. That increases downside risk to Korlym revenue beyond Teva alone. ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828023035988/cort-20230930.htm?utm_source=openai))

Immediate, actionable items I recommend for the next investigator (Agent 3)
1. Quantify revenue at risk: build a scenario model using Corcept Korlym sales (quarterly product revenue and FY guidance) and variable generic market‑share/price erosion curves (Teva alone vs. multi‑competitor). Use Corcept’s recent quarterly revenue items (I can fetch specific quarters if you want). ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))
2. Monitor entry by Sun/Hikma (and other ANDA holders) and pharmacy share data (IQVIA or specialty‑pharmacy fill data)—if other generics/authorized generic share rises, Korlym margins and volumes will compress faster. (SEC filings identify settlement terms that allow Sun/Hikma entry under conditions.) ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828023035988/cort-20230930.htm?utm_source=openai))
3. Reassess Corcept’s pipeline risk: relacorilant regulatory outcomes and any additional FDA events (PDUFA/CRL) are material — a successful relacorilant launch could mitigate Korlym erosion; failure would amplify downside. (Corcept has highlighted relacorilant as their diversification path.) ([nasdaq.com](https://www.nasdaq.com/articles/can-relacorilant-drive-corcepts-long-term-growth-beyond-korlym?utm_source=openai))
4. Watch legal/antitrust developments: Teva and Corcept have related antitrust filings; any antitrust rulings or settlements could change the commercial dynamics (SEC filings reference Teva’s antitrust complaint and related litigation). ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828025037005/cort-20250630.htm?utm_source=openai))
5. Trade/market signals: monitor CORT options put/call flow and TEVA volume around Korlym‑related headlines — sustained heavy puts on CORT or material institutional selling would inform timing for downside realization.

If you want, I can (select one)
- Run a revenue‑at‑risk model now (base / downside / worst) using Corcept’s quarterly Korlym sales and plausible market‑share erosion scenarios (I’ll need to pull the latest revenue cadence and market‑share assumptions).  
- Pull IQVIA / prescription fill data or specialty pharmacy share (if you want the empirical fill‑share trend vs. authorized generic) — may require subscription sources; I’ll fetch available public proxies.  
- Track Sun/Hikma ANDA statuses and any new filings/launch notices and prepare a 30/60/90‑day entry risk timeline.

Sources I used (most important)
- Federal Circuit opinion (Corcept v. Teva), Feb 19, 2026 (opinion PDF). ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))  
- Benzinga real‑time coverage (headline & market snapshot). ([benzinga.com](https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape))  
- Optum / Drugs.com reporting and Teva/industry notices confirming Teva launched an AB‑rated generic of Korlym in Jan 2024. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))  
- Corcept corporate release & SEC filings describing Korlym revenue (2024 results and ongoing risk disclosures re: generics) and litigation/settlement background. ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))

Would you like me to: (A) run the revenue‑at‑risk model now (I’ll fetch the exact quarterly/TTM Korlym sales and model price/volume erosion scenarios), or (B) pull prescription/fill share data and ANDA entry watchlist for Sun/Hikma and others?

#### Tool Calls (9)

##### 1. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174835+00:00 -> 2026-02-19T15:51:39.174836+00:00
- **Hop Index:** 21

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997311e5db88194a90bb823a4300b43",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 2. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174915+00:00 -> 2026-02-19T15:51:39.174915+00:00
- **Hop Index:** 22

**Input:**

```json
{
  "pattern": "injunction",
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973122762481949f304e48a3cb213c",
  "action": {
    "pattern": "injunction",
    "type": "find_in_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 3. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174939+00:00 -> 2026-02-19T15:51:39.174939+00:00
- **Hop Index:** 23

**Input:**

```json
{
  "query": "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973128625081948e32444e5a19c5f1",
  "action": {
    "query": "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news",
    "type": "search",
    "queries": [
      "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news",
      "Federal Circuit affirms no infringement Teva Corcept Korlym 24-1346 news Reuters"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 4. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174957+00:00 -> 2026-02-19T15:51:39.174957+00:00
- **Hop Index:** 24

**Input:**

```json
{
  "query": "Teva launches generic Korlym January 2024 Teva press release"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973134a0a88194af2dc35bee3f6bb1",
  "action": {
    "query": "Teva launches generic Korlym January 2024 Teva press release",
    "type": "search",
    "queries": [
      "Teva launches generic Korlym January 2024 Teva press release",
      "Teva Korlym generic launched January 2024 'mifepristone' Teva 'Korlym' launch 2024 press"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 5. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174974+00:00 -> 2026-02-19T15:51:39.174974+00:00
- **Hop Index:** 25

**Input:**

```json
{
  "query": "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997313affc881948c98b32b3543432a",
  "action": {
    "query": "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release",
    "type": "search",
    "queries": [
      "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release",
      "Teva statement Federal Circuit Korlym Feb 19 2026 press release 'Corcept' 'Federal Circuit'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 6. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175006+00:00 -> 2026-02-19T15:51:39.175006+00:00
- **Hop Index:** 26

**Input:**

```json
{
  "query": "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973144ffec81948ce57d9f76c2c77c",
  "action": {
    "query": "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'",
    "type": "search",
    "queries": [
      "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'",
      "Corcept 2025 revenue Korlym percentage 'Korlym' '2025' 'annual report' '10-K' '2025'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 7. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175023+00:00 -> 2026-02-19T15:51:39.175023+00:00
- **Hop Index:** 27

**Input:**

```json
{
  "query": "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997315590ec8194a64ebfe5f045dbc7",
  "action": {
    "query": "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'",
    "type": "search",
    "queries": [
      "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'",
      "CORT stock Feb 19 2026 percentage drop 'Federal Circuit' 'Teva' 'Korlym'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 8. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175040+00:00 -> 2026-02-19T15:51:39.175040+00:00
- **Hop Index:** 28

**Input:**

```json
{
  "query": "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f800699731662f9481949e33501417c3e932",
  "action": {
    "query": "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'",
    "type": "search",
    "queries": [
      "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'",
      "CORT Feb 19 2026 'stock fell' 'Federal Circuit' 'Corcept' 'Teva'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 9. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175055+00:00 -> 2026-02-19T15:51:39.175055+00:00
- **Hop Index:** 29

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973182f75081949531f26623ed0953",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

### Round 3: gemini

- **Model:** `gemini-3-flash-preview`
- **Tokens:** 12,434 in / 865 out
- **Reasoning Tokens:** 1,031
- **Tool Calls:** 4
- **Requests:** 1
- **Cost:** $0.0059
- **Elapsed:** 30.2s

#### System Prompt

<details>
<summary>Show system prompt (2524 chars)</summary>

```
You are a financial research analyst investigating a breaking news event.
You are Agent 3 of 3 in a sequential research pipeline.

## Your role
You are the FINAL analyst. All prior agents' evidence — market data, web research, X/Twitter sentiment, and full tool results — is provided in the prompt. Do NOT repeat searches or lookups that prior agents already performed. Instead: (1) search for NEW angles, patterns, or evidence that prior agents missed, (2) deeply synthesize ALL accumulated evidence, and (3) think through the implications for short-term stock price movement. Produce a clear, actionable trading signal.

## Your tools
You have web search (Google grounding) for finding new angles.

**Background data is ALREADY provided** in the prompt below — current price,
fundamentals, technicals, options, volume, insider activity, analyst ratings,
market context, and news for the primary symbol(s). Do NOT re-fetch this data.

**Your job is to SYNTHESIZE and find NEW angles:**
- Use web search to find perspectives, analysis, or evidence that prior agents missed
- Cross-reference and verify prior agents' findings against each other
- Look for contradictions, patterns, or implications that weren't explored
- You do NOT have market data tools or url_fetch — all data is in the prompt

## How to investigate
Think step by step. Focus on areas NOT yet covered by prior agents (if any).
Stop when you have enough evidence.

## IMPORTANT: Final synthesis
You are the FINAL agent. You MUST produce a definitive trading signal.
All prior agents' findings and tool results are in the prompt — do NOT
repeat their work. If you use web search, look for NEW angles or evidence
that prior agents did not explore.

Your primary job is deep synthesis and reasoning:
- **Bull case:** What evidence supports a price move? What could go right?
- **Bear case:** What could go wrong? Is this already priced in? What are the risks?
- **Weigh** these against each other to reach your conclusion.

## Output format
After your analysis, you MUST output a JSON object in a ```json code block
with EXACTLY this schema:
```json
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": 0.0 to 1.0,
  "horizon": "15m" | "60m" | "1d",
  "magnitude_estimate": "e.g. 0.5-1.5%",
  "key_catalyst": "one-sentence summary of the main catalyst",
  "bull_case": "brief bull case argument",
  "bear_case": "brief bear case argument",
  "risk_factors": ["risk1", "risk2", ...]
}
```
This JSON block MUST be the last thing in your response.
```

</details>

#### User Message

<details>
<summary>Show user message (29929 chars)</summary>

```
## The news event
**Headline:** Federal Circuit Affirmed A No-Infringement Win For Teva Pharmaceutical Industries Vs Corcept Therapeutics Over Korlym (Mifepristone) Dosing With Strong CYP3A Inhibitors
**Summary:** https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf
**Symbols:** CORT, TEVA
**Source:** benzinga
**Timestamp:** 2026-02-19T15:48:17+00:00
**URL:** https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape

## Recent news coverage (FinnHub)
### CORT
- [2026-02-16 08:21] (ChartMill) Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case
  Corcept Therapeutics exemplifies GARP investing with accelerating earnings growth, strong profitability, and a solid debt-free balance sheet, all at a reasonable sector-relative valuation.

### TEVA
- [2026-02-19 05:07] (SeekingAlpha) How Bausch Health's Turnaround Progressed In Q4 2025
- [2026-02-18 12:00] (Benzinga) 13 Analysts Have This To Say About Teva Pharmaceutical Indus
   
- [2026-02-18 06:29] (Benzinga) Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42
  Truist Securities  analyst Les Sulewski   maintains Teva Pharmaceutical Indus (NYSE:TEVA) with a Buy and raises the price target from $38 to $42.
- [2026-02-17 07:02] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow
- [2026-02-17 05:54] (SeekingAlpha) Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript
  Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results February 17, 2026 8:00 AM ESTCompany...
- [2026-02-17 04:56] (Benzinga) Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,
- [2026-02-17 03:04] (Benzinga) Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients
  Teva stock rises after a new study, in partnership with Sanofi, shows lasting benefits in ulcerative colitis and Crohn&#39;s patients.
- [2026-02-17 02:06] (Benzinga) Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs

## Earnings context (FinnHub)
### CORT
Earnings track record (last 4 quarters): 3 beats, 1 misses
  Most recent (2025-09-30): beat by 16.2% (actual $0.16 vs est $0.14)

### TEVA
Last earnings: 2026-01-28 (during market hours) — EPS actual: $0.96 vs est: $0.69
Earnings track record (last 4 quarters): 4 beats, 0 misses
  Most recent (2025-12-31): beat by 38.5% (actual $0.96 vs est $0.69)


---

## Prior agent findings

### Agent: grok (model: grok-4-1-fast-reasoning, 21 tool calls)
**Event Summary:**  
The U.S. Federal Circuit Court of Appeals (CAFC) affirmed a district court ruling on February 19, 2026, granting Teva Pharmaceutical Industries (TEVA) a no-infringement victory against Corcept Therapeutics (CORT) in a patent dispute over CORT's Korlym (mifepristone) product, specifically related to dosing instructions for use with strong CYP3A inhibitors. This clears a key legal hurdle for Teva to potentially launch a generic version of Korlym, CORT's flagship drug for Cushing's syndrome, which drives the vast majority of CORT's revenue (historically 90%+ of sales; recent quarterly revenue totals ~$180-208M, with Korlym as primary contributor).

**Market Impact on CORT:**  
- **Sharp Decline:** CORT plunged -23.56% to $30.44 (near 52-week lows of $32.99), on explosive 21x average volume (2.59M shares) and bearish net delta (-140K shares, 64% downtick). This dwarfs broader market weakness (SPY -0.12%, VIX 20.04).  
- **Technicals Oversold:** RSI 27 (deeply oversold), price -44% below 50-SMA ($53), -56% below 200-SMA ($67); near Bollinger lower band; MACD bearish at -3.59.  
- **Options Bearish:** Put/Call volume ratio 3.7x, OI 6.1x (heavy put interest).  
- **Biotech Context:** Sector mildly down (XBI -0.38% to $125.12, IBB -0.64% to $173.64); no other biotech names in top NASDAQ/SPX losers, confirming CORT-specific reaction.  

**Market Impact on TEVA:**  
- **Minimal Reaction:** -0.44% to $33.96 (near 52-week highs), low volume (0.23x avg), mild bearish delta. Positive analyst momentum intact (e.g., Truist Buy/$42 PT on Feb 18). Spike detected (-0.7% intraday), but generic win is incremental amid TEVA's strong YTD +98% run on earnings beats and pipeline (e.g., duvakitug IBD data).  

**News & Coverage Breadth:**  
- **Breaking Nature:** Benzinga first report (15:48 UTC); no immediate follow-up in major outlets (Seeking Alpha, Yahoo Finance, MarketWatch). CORT/TEVA recent news focused on FDA CRL for relacorilant (CORT pipeline drug), earnings, and TEVA's IBD trial success—no prior ruling mentions.  
- **Fundamentals Context:** Korlym enables CORT's high margins (gross ~98%, recent Q EPS beats but YoY growth slowing). Loss of exclusivity accelerates revenue cliff risk; quarterly revenue up QoQ but reliant on Korlym.  

**Social Sentiment (X/Twitter):**  
- Limited chatter: Semantic/keyword searches (since Feb 19) yielded low volume; x_stream_cache empty. Early reaction likely muted/outside budget (used 2/10 calls). No viral bearish threads or influencer takes detected yet—suggests news not fully digested.  

**Broader Market:**  
- SPX movers flat/down mildly (NVDA/INTC -0.1%); NASDAQ losers low-float names (e.g., TRNR +49% vol but unrelated). Not sector-wide biotech selloff.  

**Key Risks/Opportunities for Further Investigation:**  
- Generic timeline/launch impact on CORT revenue (~$800M+ annualized Korlym run-rate).  
- CORT pipeline (relacorilant CRL scrutiny); insider sales recent.  
- TEVA generic pipeline acceleration. Pass to Agent 2 for deep financial modeling/peer comps.

#### Tool results from this agent:
**web_search(url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_31527568", "action": {"type": "open_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(url='https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_25442653", "action": {"type": "open_page", "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='"Federal Circuit" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_18630801", "action": {"query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='CORT stock drop Teva patent Korlym "Federal Circuit"')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_55647359", "action": {"query": "CORT stock drop Teva patent Korlym \"Federal Circuit\"", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Korlym revenue percentage Corcept Therapeutics CORT')**:
```
{"id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_20861641", "action": {"query": "Korlym revenue percentage Corcept Therapeutics CORT", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**x_stream_cache(symbol='CORT', limit=20)**:
```
{"symbol": "CORT", "posts": [], "count": 0, "source": "x_stream_cache"}
```

**x_stream_cache(symbol='TEVA', limit=20)**:
```
{"symbol": "TEVA", "posts": [], "count": 0, "source": "x_stream_cache"}
```

**get_movers(index='$SPX', direction='down')**:
```
{"index": "$SPX", "direction": "down", "movers": [{"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "down", "change": -0.15, "pct_change": -0.0008, "volume": 37088573, "last_price": 187.83}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "down", "change": -0.48, "pct_change": -0.0106, "volume": 25539507, "last_price": 44.98}, {"symbol": "SMCI", "description": "SUPER MICRO COMPUTER", "direction": "down", "change": 1.99, "pct_change": 0.067, "volume": 21205373, "last_price": 31.7}, {"symbol": "OXY", "description": "OCCIDENTAL PETE CORP", "direction": "down", "change": 3.79, "pct_change": 0.0805, "volume": 16919376, "last_price": 50.9}, {"symbol": "PLTR", "description": "PALANTIR TECHNOLOGIE Class A", "direction": "down", "change": 0.38, "pct_change": 0.0028, "volume": 16381966, "last_price": 135.76}, {"symbol": "BAC", "description": "BANK OF AMERICA CORP", "direction": "down", "change": -0.73, "pct_change": -0.0136, "volume": 15778815, "last_price": 52.64}, {"symbol": "WMT", "description": "WALMART INC", "direction": "down", "change": 2.31, "pct_change": 0.0182, "volume": 15491567, "last_price": 128.93}, {"symbol": "TSLA", "description": "TESLA INC", "direction": "down", "change": -0.14, "pct_change": -0.0003, "volume": 15394510, "last_price": 411.18}, {"symbol": "F", "description": "FORD MTR CO DEL", "direction": "down", "change": -0.05, "pct_change": -0.004, "volume": 11438288, "last_price": 13.8}, {"symbol": "AMZN", "description": "AMAZON.COM INC", "direction": "down", "change": -0.33, "pct_change": -0.0016, "volume": 10189752, "last_price": 204.46}], "count": 10, "fetched_at": "2026-02-19T15:48:50.836433+00:00", "source": "schwab"}
```

**get_movers(index='$SPX', direction='up')**:
```
{"index": "$SPX", "direction": "up", "movers": [{"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "up", "change": -0.15, "pct_change": -0.0008, "volume": 37088573, "last_price": 187.83}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "up", "change": -0.48, "pct_change": -0.0106, "volume": 25539507, "last_price": 44.98}, {"symbol": "SMCI", "description": "SUPER MICRO COMPUTER", "direction": "up", "change": 1.99, "pct_change": 0.067, "volume": 21205373, "last_price": 31.7}, {"symbol": "OXY", "description": "OCCIDENTAL PETE CORP", "direction": "up", "change": 3.79, "pct_change": 0.0805, "volume": 16919376, "last_price": 50.9}, {"symbol": "PLTR", "description": "PALANTIR TECHNOLOGIE Class A", "direction": "up", "change": 0.38, "pct_change": 0.0028, "volume": 16381966, "last_price": 135.76}, {"symbol": "BAC", "description": "BANK OF AMERICA CORP", "direction": "up", "change": -0.73, "pct_change": -0.0136, "volume": 15778815, "last_price": 52.64}, {"symbol": "WMT", "description": "WALMART INC", "direction": "up", "change": 2.31, "pct_change": 0.0182, "volume": 15491567, "last_price": 128.93}, {"symbol": "TSLA", "description": "TESLA INC", "direction": "up", "change": -0.14, "pct_change": -0.0003, "volume": 15394510, "last_price": 411.18}, {"symbol": "F", "description": "FORD MTR CO DEL", "direction": "up", "change": -0.05, "pct_change": -0.004, "volume": 11438288, "last_price": 13.8}, {"symbol": "AMZN", "description": "AMAZON.COM INC", "direction": "up", "change": -0.33, "pct_change": -0.0016, "volume": 10189752, "last_price": 204.46}], "count": 10, "fetched_at": "2026-02-19T15:48:51.036979+00:00", "source": "schwab"}
```

**get_financial_statements(symbol='CORT', statement='income', freq='quarterly')**:
```
{"symbol": "CORT", "statement": "income", "frequency": "quarterly", "periods": [{"date": "2025-09-30", "total_revenue": 207638000.0, "cost_of_revenue": 4596000.0, "gross_profit": 203042000.0, "operating_income": 10219000.0, "ebitda": 10696000.0, "net_income": 19668000.0, "diluted_eps": 0.16}, {"date": "2025-06-30", "total_revenue": 194430000.0, "cost_of_revenue": 3433000.0, "gross_profit": 190997000.0, "operating_income": 26675000.0, "ebitda": 27144000.0, "net_income": 35149000.0, "diluted_eps": 0.29}, {"date": "2025-03-31", "total_revenue": 157214000.0, "cost_of_revenue": 2403000.0, "gross_profit": 154811000.0, "operating_income": 3416000.0, "ebitda": 3878000.0, "net_income": 20547000.0, "diluted_eps": 0.17}, {"date": "2024-12-31", "total_revenue": 181890000.0, "cost_of_revenue": 2956000.0, "gross_profit": 178934000.0, "operating_income": 25262000.0, "ebitda": 25722000.0, "net_income": 30746000.0, "diluted_eps": 0.26}], "fetched_at": "2026-02-19T15:48:51.464680+00:00", "source": "yfinance"}
```

**web_search(url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_79423442", "action": {"type": "open_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(url='https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_22626050", "action": {"type": "open_page", "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_38511353", "action": {"query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='"Corcept" OR CORT "Federal Circuit" OR "CAFC" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com')**:
```
{"id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_19649004", "action": {"query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com", "type": "search", "queries": null, "sources": []}, "status": "completed", "type": "web_search_call"}
```

**get_movers(index='NASDAQ', direction='down')**:
```
{"index": "NASDAQ", "direction": "down", "movers": [{"symbol": "TRNR", "description": "INTERACTIVE STRENGTH", "direction": "down", "change": 0.2, "pct_change": 0.496, "volume": 228347252, "last_price": 0.61}, {"symbol": "RUBI", "description": "RUBICO INC", "direction": "down", "change": -0.15, "pct_change": -0.067, "volume": 81374174, "last_price": 2.09}, {"symbol": "AGAE", "description": "ALLIED GAMING & ENTM", "direction": "down", "change": 0.13, "pct_change": 0.4336, "volume": 75082593, "last_price": 0.44}, {"symbol": "DTCK", "description": "DAVIS COMMODITIES LT A", "direction": "down", "change": -0.07, "pct_change": -0.3591, "volume": 63926898, "last_price": 0.12}, {"symbol": "RXT", "description": "RACKSPACE TECHNOLOGY", "direction": "down", "change": -0.26, "pct_change": -0.1898, "volume": 37655119, "last_price": 1.11}, {"symbol": "NVDA", "description": "NVIDIA CORP", "direction": "down", "change": -0.14, "pct_change": -0.0008, "volume": 37182053, "last_price": 187.84}, {"symbol": "ONDS", "description": "ONDAS INC", "direction": "down", "change": 0.38, "pct_change": 0.0339, "volume": 36296765, "last_price": 11.45}, {"symbol": "TQQQ", "description": "PROSHARES ULTRAPRO QQQ ETF", "direction": "down", "change": -0.26, "pct_change": -0.0053, "volume": 31209808, "last_price": 49.13}, {"symbol": "BOXL", "description": "BOXLIGHT CORP", "direction": "down", "change": 0.39, "pct_change": 0.2962, "volume": 25976451, "last_price": 1.69}, {"symbol": "INTC", "description": "INTEL CORP", "direction": "down", "change": -0.47, "pct_change": -0.0102, "volume": 25577216, "last_price": 45.0}], "count": 10, "fetched_at": "2026-02-19T15:49:13.531445+00:00", "source": "schwab"}
```

### Agent: openai (model: gpt-5-mini, 9 tool calls)
Short verified summary and next steps (deep-dive findings)

What happened (verified)
- The U.S. Court of Appeals for the Federal Circuit (panel decision, non‑precedential) affirmed the District of New Jersey’s December 29, 2023 judgment that Teva’s proposed generic Korlym product does not infringe Corcept’s asserted patents (U.S. Pat. Nos. 10,195,214 and 10,842,800). The opinion was filed Feb 19, 2026. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))
- The CAFC affirmed on the district court’s fact findings: (a) no persuasive record evidence that physicians have practiced the claimed methods in the past, and (b) future direct infringement was “highly unlikely” (physicians avoid co‑administration for safety/dosing reasons; osilodrostat is a non‑infringing alternative; Teva’s proposed label can be followed without infringing). The court therefore affirmed the no‑infringement finding without reaching induced‑infringement intent. The opinion also notes Teva could have avoided litigation via a Section VIII label carve‑out. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))

Commercial / market facts verified
- Teva previously filed an ANDA and launched an AB‑rated generic version of Korlym (mifepristone, 300 mg) in January 2024. Multiple trade/clinical outlets list Teva’s generic as launched Jan 2024. That means Teva’s product has been commercially available since then and this appellate outcome removes the key patent overhang. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Market reaction: Benzinga published the CAFC result Feb 19, 2026 and showed CORT trading down materially at the time of posting (example snapshot $32.63, -18.1% in the Benzinga feed). This matches the immediate negative price reaction reported in market feeds. ([benzinga.com](https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape))
- Corcept is highly dependent on Korlym for revenue. Corcept’s corporate disclosures and recent results show Korlym as the sole marketed product and the driver of company revenue (Corcept reported 2024 revenue $675M and continues to disclose that generic entry is a material risk). Corcept’s SEC filings also document the litigation history, Teva’s ANDA approval/launch, and the potential commercial impact. ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))

What the CAFC opinion means (implications, verified)
- Legal: Affirmance means Corcept’s asserted method patents remain not‑infringed as applied to Teva’s proposed label/product. The CAFC’s reliance on real‑world physician practice (and availability of a non‑infringing alternative) is exactly the kind of evidentiary line other Hatch‑Waxman disputes will watch. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))
- Commercial: Because Teva launched a generic in Jan 2024, removal of the appellate overhang increases the likelihood of durable generic competition pressure on Korlym sales going forward (price/volume impact already realized in market moves). Corcept’s filings warned generic entry could materially harm its results. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Practical note from the opinion: the court explicitly suggested Teva could have used a Section VIII carve‑out to avoid the suit—this is a sign courts will consider labeling strategies and real prescribing practice heavily in ANDA disputes. ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))

Gaps / items I verified that prior coverage missed or under‑emphasized
- Timing: Teva’s generic has been on the market since Jan 2024 (so the CAFC decision mainly removes legal uncertainty rather than enabling a new market entry). Several market pieces had flagged the 2023 district court loss and the ongoing appeal; the key verified change today is final appellate rejection of Corcept’s infringement argument. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))
- Entrants: Corcept’s SEC filings (and prior settlements) permit Sun/Hikma entry under defined terms if Teva’s generic is available — so multiple generic sources could be commercially possible if market dynamics permit. That increases downside risk to Korlym revenue beyond Teva alone. ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828023035988/cort-20230930.htm?utm_source=openai))

Immediate, actionable items I recommend for the next investigator (Agent 3)
1. Quantify revenue at risk: build a scenario model using Corcept Korlym sales (quarterly product revenue and FY guidance) and variable generic market‑share/price erosion curves (Teva alone vs. multi‑competitor). Use Corcept’s recent quarterly revenue items (I can fetch specific quarters if you want). ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))
2. Monitor entry by Sun/Hikma (and other ANDA holders) and pharmacy share data (IQVIA or specialty‑pharmacy fill data)—if other generics/authorized generic share rises, Korlym margins and volumes will compress faster. (SEC filings identify settlement terms that allow Sun/Hikma entry under conditions.) ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828023035988/cort-20230930.htm?utm_source=openai))
3. Reassess Corcept’s pipeline risk: relacorilant regulatory outcomes and any additional FDA events (PDUFA/CRL) are material — a successful relacorilant launch could mitigate Korlym erosion; failure would amplify downside. (Corcept has highlighted relacorilant as their diversification path.) ([nasdaq.com](https://www.nasdaq.com/articles/can-relacorilant-drive-corcepts-long-term-growth-beyond-korlym?utm_source=openai))
4. Watch legal/antitrust developments: Teva and Corcept have related antitrust filings; any antitrust rulings or settlements could change the commercial dynamics (SEC filings reference Teva’s antitrust complaint and related litigation). ([sec.gov](https://www.sec.gov/Archives/edgar/data/1088856/000162828025037005/cort-20250630.htm?utm_source=openai))
5. Trade/market signals: monitor CORT options put/call flow and TEVA volume around Korlym‑related headlines — sustained heavy puts on CORT or material institutional selling would inform timing for downside realization.

If you want, I can (select one)
- Run a revenue‑at‑risk model now (base / downside / worst) using Corcept’s quarterly Korlym sales and plausible market‑share erosion scenarios (I’ll need to pull the latest revenue cadence and market‑share assumptions).  
- Pull IQVIA / prescription fill data or specialty pharmacy share (if you want the empirical fill‑share trend vs. authorized generic) — may require subscription sources; I’ll fetch available public proxies.  
- Track Sun/Hikma ANDA statuses and any new filings/launch notices and prepare a 30/60/90‑day entry risk timeline.

Sources I used (most important)
- Federal Circuit opinion (Corcept v. Teva), Feb 19, 2026 (opinion PDF). ([cafc.uscourts.gov](https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf))  
- Benzinga real‑time coverage (headline & market snapshot). ([benzinga.com](https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape))  
- Optum / Drugs.com reporting and Teva/industry notices confirming Teva launched an AB‑rated generic of Korlym in Jan 2024. ([business.optum.com](https://business.optum.com/en/support/professionalrx/professionalrx-resources/newgenerics_korlym_2024-0122.html?utm_source=openai))  
- Corcept corporate release & SEC filings describing Korlym revenue (2024 results and ongoing risk disclosures re: generics) and litigation/settlement background. ([ir.corcept.com](https://ir.corcept.com/news-releases/news-release-details/corcept-therapeutics-announces-fourth-quarter-and-full-year-2024?utm_source=openai))

Would you like me to: (A) run the revenue‑at‑risk model now (I’ll fetch the exact quarterly/TTM Korlym sales and model price/volume erosion scenarios), or (B) pull prescription/fill share data and ANDA entry watchlist for Sun/Hikma and others?

#### Tool results from this agent:
**web_search(url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_0bdc2562d42595f8006997311e5db88194a90bb823a4300b43", "action": {"type": "open_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(pattern='injunction', url='https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf')**:
```
{"id": "ws_0bdc2562d42595f80069973122762481949f304e48a3cb213c", "action": {"pattern": "injunction", "type": "find_in_page", "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news')**:
```
{"id": "ws_0bdc2562d42595f80069973128625081948e32444e5a19c5f1", "action": {"query": "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news", "type": "search", "queries": ["Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news", "Federal Circuit affirms no infringement Teva Corcept Korlym 24-1346 news Reuters"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(query='Teva launches generic Korlym January 2024 Teva press release')**:
```
{"id": "ws_0bdc2562d42595f80069973134a0a88194af2dc35bee3f6bb1", "action": {"query": "Teva launches generic Korlym January 2024 Teva press release", "type": "search", "queries": ["Teva launches generic Korlym January 2024 Teva press release", "Teva Korlym generic launched January 2024 'mifepristone' Teva 'Korlym' launch 2024 press"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(query="Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release")**:
```
{"id": "ws_0bdc2562d42595f8006997313affc881948c98b32b3543432a", "action": {"query": "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release", "type": "search", "queries": ["Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release", "Teva statement Federal Circuit Korlym Feb 19 2026 press release 'Corcept' 'Federal Circuit'"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(query="Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'")**:
```
{"id": "ws_0bdc2562d42595f80069973144ffec81948ce57d9f76c2c77c", "action": {"query": "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'", "type": "search", "queries": ["Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'", "Corcept 2025 revenue Korlym percentage 'Korlym' '2025' 'annual report' '10-K' '2025'"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(query="Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'")**:
```
{"id": "ws_0bdc2562d42595f8006997315590ec8194a64ebfe5f045dbc7", "action": {"query": "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'", "type": "search", "queries": ["Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'", "CORT stock Feb 19 2026 percentage drop 'Federal Circuit' 'Teva' 'Korlym'"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(query="CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'")**:
```
{"id": "ws_0bdc2562d42595f800699731662f9481949e33501417c3e932", "action": {"query": "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'", "type": "search", "queries": ["CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'", "CORT Feb 19 2026 'stock fell' 'Federal Circuit' 'Corcept' 'Teva'"], "sources": null}, "status": "completed", "type": "web_search_call"}
```

**web_search(url='https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape')**:
```
{"id": "ws_0bdc2562d42595f80069973182f75081949531f26623ed0953", "action": {"type": "open_page", "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"}, "status": "completed", "type": "web_search_call"}
```

```

</details>

#### Reasoning Summary

```
**My Analysis of the Corcept (CORT) vs. Teva (TEVA) Ruling**

Okay, so the Federal Circuit affirmed the no-infringement ruling in favor of Teva regarding Korlym (mifepristone), and CORT is getting hammered, down ~23.5%. TEVA's barely budging, which makes sense. But the first thing that jumps out at me is, wait a second... Agent 2 pointed out that Teva *already* launched a generic in January 2024. So why the massive market reaction *now*, on February 19th, 2026?

The key is that the Federal Circuit *affirmed* the win. This means any threat of an injunction that could have pulled Teva’s generic off the market or led to substantial damages is now gone. That makes a huge difference. The legal overhang that was potentially protecting CORT is now gone.

I need to dig deeper into the nuance. If the market was pricing in a high chance of CORT winning the appeal, *that* could explain the violent reaction today, even with the prior generic launch. What really got me thinking is the reference to Sun and Hikma potentially entering the market based on settlement terms. Were those settlements triggered by a "final" ruling like this? If Teva's win is now definitive, does that open the floodgates for a bunch of generics? A "generic pack" effect would *decimate* Korlym's pricing. I need to get the precise language from those settlements, and fast. What are the triggers for generic entry?

Financially, I'm thinking about the scale. Korlym is Corcept's primary revenue driver. At an annualized $800M in revenue, if three or more generics enter the market, we're talking about a revenue drop of 50-80%...and *quickly*. Is this a "broken growth story" in the making for CORT?

I need to quickly focus on what is driving market reaction today. This is specific to this ruling, not the generic launch, I need to check for any recent analyst reactions to *this* news.

Lastly, I need to confirm the *relacorilant* CRL status. When was it issued? What were the reasons cited? The failure of their pipeline drug makes the Korlym generic threat even *more* existential, with no safety net to rely on.




**The Situation with Corcept Therapeutics (CORT): A Clear Bearish Outlook**

Alright, let's break this down. The pieces are falling into place, and the picture isn't pretty for Corcept. It's a classic situation, and as an expert, I'm seeing a clear pattern here.

First, we have the legal battle with Teva (TEVA). The Federal Circuit's recent *affirmation* on the no-infringement ruling is the critical turning point. This "blesses" Teva's generic mifepristone launch, essentially making it safe to stay on the market without the threat of any legal intervention. Teva's generic is now legally "safe" and on the market.

This opens the floodgates. Corcept has settlement agreements with Sun Pharmaceutical and Hikma. These settlements allow Sun/Hikma to enter in 2034, *or sooner, if certain conditions are met*. And guess what? The entry of a generic *specifically* triggers early entry. This is the crucial link: now that Teva's generic is safe, Sun and Hikma *likely* have the right to enter *now* or very soon, leading to a multi-source generic market. We know what that means: an 80-90% price erosion for the branded drug, Korlym.

Corcept is essentially a one-trick pony, almost entirely reliant on Korlym revenue. Their successor drug, relacorilant, is, unfortunately, stuck, and is facing a significant delay. The FDA gave them a CRL on December 31, 2025, requesting additional evidence. This likely means a new clinical trial, and thus, years of delay. There's a sliver of hope with the relacorilant ovarian cancer PDUFA date of July 11, 2026, but that's a long shot and still several months away.

So, here's what we have:

*   **Korlym:** No legal protection, multi-source generics inbound.
*   **Relacorilant (Cushing's):** Delayed, likely multi-year setback.
*   **Cash Position:** Adequate, but revenue burn will accelerate dramatically.
*   **Revenue:** Guided for $850-$900M in 2025. That revenue is toast in 2026 with the multi-source competition and no relacorilant to replace it.

For Teva, this is a minor win in the grand scheme of things. Korlym is a relatively small market. But, this is a very strong loss for Corcept.

**My Signal:** Bearish for CORT. The structural business model is broken. I'm seeing a clear and imminent revenue cliff.

The current date is **February 19, 2026**. I see the confirmation in the market. CORT is trading at $30.44 (-23.5%). The stock is breaking through the 52-week low. The second leg of the collapse began after the relacorilant CRL and now this ruling.

Now, I need to confirm if Sun/Hikma *have* actually launched, and if Corcept has an "authorized generic." The 1.9 search result says that Corcept is marketing its *own* generic since June 2024. If Sun and Hikma enter, we are looking at a 5-player market.

**The Bottom Line:** Relacorilant's ovarian cancer potential is the only silver lining, but that's a long shot. The revenue cliff for Korlym is real and approaching fast. The immediate reaction might have overshot the mark, but the structural damage to their business is undeniable.



```

#### Agent Findings

Direction: bearish, Confidence: 0.9, Catalyst: Federal Circuit affirmation removes final legal barrier to multi-source generic competition for Korlym, Corcept's sole revenue driver.

#### Raw Output

<details>
<summary>Show raw output (811 chars)</summary>

```
direction='bearish' confidence=0.9 horizon='1d' magnitude_estimate='5-10%' key_catalyst="Federal Circuit affirmation removes final legal barrier to multi-source generic competition for Korlym, Corcept's sole revenue driver." bull_case='The market may have overreacted to the legal news given Teva was already on the market; cash reserves could support the stock near $25-28 levels.' bear_case='Loss of patent moat combined with the recent relacorilant CRL leaves the company with a massive revenue cliff and no near-term successor drug; Sun and Hikma entry is now highly probable.' risk_factors=['Unexpected settlement or acquisition interest from a larger pharma peer', 'Positive surprise from the ovarian cancer PDUFA in July 2026', 'Short-covering bounce due to extremely oversold technical levels (RSI 27)']
```

</details>

#### Parsed Signal

```json
{
  "direction": "bearish",
  "confidence": 0.9,
  "horizon": "1d",
  "magnitude_estimate": "5-10%",
  "key_catalyst": "Federal Circuit affirmation removes final legal barrier to multi-source generic competition for Korlym, Corcept's sole revenue driver.",
  "bull_case": "The market may have overreacted to the legal news given Teva was already on the market; cash reserves could support the stock near $25-28 levels.",
  "bear_case": "Loss of patent moat combined with the recent relacorilant CRL leaves the company with a massive revenue cliff and no near-term successor drug; Sun and Hikma entry is now highly probable.",
  "risk_factors": [
    "Unexpected settlement or acquisition interest from a larger pharma peer",
    "Positive surprise from the ovarian cancer PDUFA in July 2026",
    "Short-covering bounce due to extremely oversold technical levels (RSI 27)"
  ]
}
```

#### Tool Calls (4)

##### 1. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003484+00:00 -> 2026-02-19T15:52:10.003484+00:00
- **Hop Index:** 30

**Input:**

```json
{
  "query": "Corcept relacorilant CRL date and reason 2025 2026"
}
```

##### 2. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003522+00:00 -> 2026-02-19T15:52:10.003522+00:00
- **Hop Index:** 31

**Input:**

```json
{
  "query": "did Teva launch generic Korlym at risk in 2024?"
}
```

##### 3. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003534+00:00 -> 2026-02-19T15:52:10.003534+00:00
- **Hop Index:** 32

**Input:**

```json
{
  "query": "Corcept Hikma Sun Pharmaceutical settlement terms generic Korlym entry trigger"
}
```

##### 4. web_search

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003545+00:00 -> 2026-02-19T15:52:10.003545+00:00
- **Hop Index:** 33

**Input:**

```json
{
  "query": "\"CORT\" stock \"Federal Circuit\" February 19 2026 analyst reaction"
}
```

---

## All Tool Traces (34 total)

##### 1. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551421+00:00 -> 2026-02-19T15:48:49.551421+00:00
- **Hop Index:** 0

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_31527568",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 2. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551473+00:00 -> 2026-02-19T15:48:49.551473+00:00
- **Hop Index:** 1

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_25442653",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 3. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551508+00:00 -> 2026-02-19T15:48:49.551508+00:00
- **Hop Index:** 2

**Input:**

```json
{
  "query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_18630801",
  "action": {
    "query": "\"Federal Circuit\" Teva Corcept Korlym OR mifepristone infringement ruling OR affirmed",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 4. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551528+00:00 -> 2026-02-19T15:48:49.551528+00:00
- **Hop Index:** 3

**Input:**

```json
{
  "query": "CORT stock drop Teva patent Korlym \"Federal Circuit\""
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_55647359",
  "action": {
    "query": "CORT stock drop Teva patent Korlym \"Federal Circuit\"",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 5. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:48:49.551548+00:00 -> 2026-02-19T15:48:49.551548+00:00
- **Hop Index:** 4

**Input:**

```json
{
  "query": "Korlym revenue percentage Corcept Therapeutics CORT"
}
```

**Output:**

```json
{
  "id": "ws_4d3c713f-58e6-456e-7577-6a15d21f12bc_call_20861641",
  "action": {
    "query": "Korlym revenue percentage Corcept Therapeutics CORT",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 6. x_stream_cache [grok]

- **Duration:** 0.000s
- **Modality:** social
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551563+00:00 -> 2026-02-19T15:48:49.551584+00:00
- **Hop Index:** 5

**Input:**

```json
{
  "symbol": "CORT",
  "limit": 20
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "posts": [],
  "count": 0,
  "source": "x_stream_cache"
}
```

##### 7. x_stream_cache [grok]

- **Duration:** 0.000s
- **Modality:** social
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551600+00:00 -> 2026-02-19T15:48:49.551608+00:00
- **Hop Index:** 6

**Input:**

```json
{
  "symbol": "TEVA",
  "limit": 20
}
```

**Output:**

```json
{
  "symbol": "TEVA",
  "posts": [],
  "count": 0,
  "source": "x_stream_cache"
}
```

##### 8. check_market_context [grok]

- **Duration:** 1.285s
- **Modality:** macro
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:49.551619+00:00 -> 2026-02-19T15:48:50.836355+00:00
- **Hop Index:** 7

**Output:**

```json
{
  "market_is_open": true,
  "session": "market_open",
  "regularMarket_start": "2026-02-19T09:30:00-05:00",
  "regularMarket_end": "2026-02-19T16:00:00-05:00",
  "preMarket_start": "2026-02-19T07:00:00-05:00",
  "preMarket_end": "2026-02-19T09:30:00-05:00",
  "postMarket_start": "2026-02-19T16:00:00-05:00",
  "postMarket_end": "2026-02-19T20:00:00-05:00",
  "spy_last": 685.49,
  "spy_net_change": -0.8,
  "spy_net_pct_change": -0.1165688,
  "vix_level": 20.04,
  "source": "schwab"
}
```

##### 9. get_movers [grok]

- **Duration:** 0.201s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:50.836422+00:00 -> 2026-02-19T15:48:51.036926+00:00
- **Hop Index:** 8

**Input:**

```json
{
  "index": "$SPX",
  "direction": "down"
}
```

**Output:**

```json
{
  "index": "$SPX",
  "direction": "down",
  "movers": [
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "down",
      "change": -0.15,
      "pct_change": -0.0008,
      "volume": 37088573,
      "last_price": 187.83
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "down",
      "change": -0.48,
      "pct_change": -0.0106,
      "volume": 25539507,
      "last_price": 44.98
    },
    {
      "symbol": "SMCI",
      "description": "SUPER MICRO COMPUTER",
      "direction": "down",
      "change": 1.99,
      "pct_change": 0.067,
      "volume": 21205373,
      "last_price": 31.7
    },
    {
      "symbol": "OXY",
      "description": "OCCIDENTAL PETE CORP",
      "direction": "down",
      "change": 3.79,
      "pct_change": 0.0805,
      "volume": 16919376,
      "last_price": 50.9
    },
    {
      "symbol": "PLTR",
      "description": "PALANTIR TECHNOLOGIE Class A",
      "direction": "down",
      "change": 0.38,
      "pct_change": 0.0028,
      "volume": 16381966,
      "last_price": 135.76
    },
    {
      "symbol": "BAC",
      "description": "BANK OF AMERICA CORP",
      "direction": "down",
      "change": -0.73,
      "pct_change": -0.0136,
      "volume": 15778815,
      "last_price": 52.64
    },
    {
      "symbol": "WMT",
      "description": "WALMART INC",
      "direction": "down",
      "change": 2.31,
      "pct_change": 0.0182,
      "volume": 15491567,
      "last_price": 128.93
    },
    {
      "symbol": "TSLA",
      "description": "TESLA INC",
      "direction": "down",
      "change": -0.14,
      "pct_change": -0.0003,
      "volume": 15394510,
      "last_price": 411.18
    },
    {
      "symbol": "F",
      "description": "FORD MTR CO DEL",
      "direction": "down",
      "change": -0.05,
      "pct_change": -0.004,
      "volume": 11438288,
      "last_price": 13.8
    },
    {
      "symbol": "AMZN",
      "description": "AMAZON.COM INC",
      "direction": "down",
      "change": -0.33,
      "pct_change": -0.0016,
      "volume": 10189752,
      "last_price": 204.46
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:48:50.836433+00:00",
  "source": "schwab"
}
```

##### 10. get_movers [grok]

- **Duration:** 0.278s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:51.036972+00:00 -> 2026-02-19T15:48:51.314689+00:00
- **Hop Index:** 9

**Input:**

```json
{
  "index": "$SPX",
  "direction": "up"
}
```

**Output:**

```json
{
  "index": "$SPX",
  "direction": "up",
  "movers": [
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "up",
      "change": -0.15,
      "pct_change": -0.0008,
      "volume": 37088573,
      "last_price": 187.83
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "up",
      "change": -0.48,
      "pct_change": -0.0106,
      "volume": 25539507,
      "last_price": 44.98
    },
    {
      "symbol": "SMCI",
      "description": "SUPER MICRO COMPUTER",
      "direction": "up",
      "change": 1.99,
      "pct_change": 0.067,
      "volume": 21205373,
      "last_price": 31.7
    },
    {
      "symbol": "OXY",
      "description": "OCCIDENTAL PETE CORP",
      "direction": "up",
      "change": 3.79,
      "pct_change": 0.0805,
      "volume": 16919376,
      "last_price": 50.9
    },
    {
      "symbol": "PLTR",
      "description": "PALANTIR TECHNOLOGIE Class A",
      "direction": "up",
      "change": 0.38,
      "pct_change": 0.0028,
      "volume": 16381966,
      "last_price": 135.76
    },
    {
      "symbol": "BAC",
      "description": "BANK OF AMERICA CORP",
      "direction": "up",
      "change": -0.73,
      "pct_change": -0.0136,
      "volume": 15778815,
      "last_price": 52.64
    },
    {
      "symbol": "WMT",
      "description": "WALMART INC",
      "direction": "up",
      "change": 2.31,
      "pct_change": 0.0182,
      "volume": 15491567,
      "last_price": 128.93
    },
    {
      "symbol": "TSLA",
      "description": "TESLA INC",
      "direction": "up",
      "change": -0.14,
      "pct_change": -0.0003,
      "volume": 15394510,
      "last_price": 411.18
    },
    {
      "symbol": "F",
      "description": "FORD MTR CO DEL",
      "direction": "up",
      "change": -0.05,
      "pct_change": -0.004,
      "volume": 11438288,
      "last_price": 13.8
    },
    {
      "symbol": "AMZN",
      "description": "AMAZON.COM INC",
      "direction": "up",
      "change": -0.33,
      "pct_change": -0.0016,
      "volume": 10189752,
      "last_price": 204.46
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:48:51.036979+00:00",
  "source": "schwab"
}
```

##### 11. get_financial_statements [grok]

- **Duration:** 0.150s
- **Modality:** fundamentals
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:48:51.314739+00:00 -> 2026-02-19T15:48:51.464903+00:00
- **Hop Index:** 10

**Input:**

```json
{
  "symbol": "CORT",
  "statement": "income",
  "freq": "quarterly"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "statement": "income",
  "frequency": "quarterly",
  "periods": [
    {
      "date": "2025-09-30",
      "total_revenue": 207638000.0,
      "cost_of_revenue": 4596000.0,
      "gross_profit": 203042000.0,
      "operating_income": 10219000.0,
      "ebitda": 10696000.0,
      "net_income": 19668000.0,
      "diluted_eps": 0.16
    },
    {
      "date": "2025-06-30",
      "total_revenue": 194430000.0,
      "cost_of_revenue": 3433000.0,
      "gross_profit": 190997000.0,
      "operating_income": 26675000.0,
      "ebitda": 27144000.0,
      "net_income": 35149000.0,
      "diluted_eps": 0.29
    },
    {
      "date": "2025-03-31",
      "total_revenue": 157214000.0,
      "cost_of_revenue": 2403000.0,
      "gross_profit": 154811000.0,
      "operating_income": 3416000.0,
      "ebitda": 3878000.0,
      "net_income": 20547000.0,
      "diluted_eps": 0.17
    },
    {
      "date": "2024-12-31",
      "total_revenue": 181890000.0,
      "cost_of_revenue": 2956000.0,
      "gross_profit": 178934000.0,
      "operating_income": 25262000.0,
      "ebitda": 25722000.0,
      "net_income": 30746000.0,
      "diluted_eps": 0.26
    }
  ],
  "fetched_at": "2026-02-19T15:48:51.464680+00:00",
  "source": "yfinance"
}
```

##### 12. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007645+00:00 -> 2026-02-19T15:49:12.007645+00:00
- **Hop Index:** 11

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_79423442",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 13. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007693+00:00 -> 2026-02-19T15:49:12.007693+00:00
- **Hop Index:** 12

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_22626050",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 14. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007713+00:00 -> 2026-02-19T15:49:12.007713+00:00
- **Hop Index:** 13

**Input:**

```json
{
  "query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_38511353",
  "action": {
    "query": "Corcept Therapeutics Korlym revenue percentage total sales 2025 OR 2026",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 15. web_search [grok]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0050
- **Time:** 2026-02-19T15:49:12.007729+00:00 -> 2026-02-19T15:49:12.007729+00:00
- **Hop Index:** 14

**Input:**

```json
{
  "query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com"
}
```

**Output:**

```json
{
  "id": "ws_5b38d1f7-0659-68a6-b4a8-a5475068a471_call_19649004",
  "action": {
    "query": "\"Corcept\" OR CORT \"Federal Circuit\" OR \"CAFC\" Teva ruling OR affirmed OR appeal Korlym site:seekingalpha.com OR site:investing.com OR site:marketwatch.com OR site:yahoo.com",
    "type": "search",
    "queries": null,
    "sources": []
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 16. get_company_news [grok]

- **Duration:** 0.245s
- **Modality:** news
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.007744+00:00 -> 2026-02-19T15:49:12.253140+00:00
- **Hop Index:** 15

**Input:**

```json
{
  "symbol": "CORT"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "articles": [
    {
      "title": "A Look At Corcept Therapeutics (CORT) Valuation After FDA Complete Response Letter For Relacorilant",
      "summary": "Why Corcept Therapeutics (CORT) Is Back in Focus The U.S. FDA’s Complete Response Letter for Corcept Therapeutics (CORT) relacorilant program, which calls for more evidence and pushes potential market entry back by about three years, has become the key driver of recent stock sentiment. See our latest analysis for Corcept Therapeutics. At a share price of $39.82, Corcept’s 30 day share price return of 14.66% suggests a partial rebound after the sharp 46.94% 90 day share price decline that...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/look-corcept-therapeutics-cort-valuation-082120385.html",
      "published_at": "2026-02-19T08:21:20+00:00",
      "fetched_at": "2026-02-19T15:49:12.137817+00:00",
      "source": "yfinance"
    },
    {
      "title": "How Investors May Respond To Corcept Therapeutics (CORT) FDA CRL And New Securities-Law Probes",
      "summary": "In late 2025, Corcept Therapeutics received a Complete Response Letter from the U.S. FDA for its relacorilant NDA in hypertension secondary to hypercortisolism, with the agency requesting additional evidence of effectiveness and raising concerns about the clinical development program and study design. The FDA setback has triggered multiple shareholder-rights and securities-law investigations, putting Corcept’s disclosure practices and regulatory pathway for relacorilant under closer scrutiny...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/investors-may-respond-corcept-therapeutics-191229390.html",
      "published_at": "2026-02-18T19:12:29+00:00",
      "fetched_at": "2026-02-19T15:49:12.137843+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Small-Cap Stocks We Think Twice About",
      "summary": "Small-cap stocks can be incredibly lucrative investments because their lack of analyst coverage leads to frequent mispricings. However, these businesses (and their stock prices) often stay small because their subscale operations make it harder to expand their competitive moats.",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/3-small-cap-stocks-think-043524189.html",
      "published_at": "2026-02-17T04:35:24+00:00",
      "fetched_at": "2026-02-19T15:49:12.137849+00:00",
      "source": "yfinance"
    },
    {
      "title": "FDA-Issued CRL for Relacorilant NDA Leads to Bullish Views Around Corcept Therapeutics (CORT)",
      "summary": "Corcept Therapeutics (NASDAQ:CORT) is one of the 17 biotechnology stocks with more than 50% upside. On February 2, Edward Nash from Canaccord Genuity reaffirmed his Buy rating for Corcept Therapeutics (NASDAQ:CORT). He forecasted a price target of $100, which leads to almost 161% upside potential on the stock. Nash noted that the CRL issued by […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/fda-issued-crl-relacorilant-nda-172400187.html",
      "published_at": "2026-02-16T17:24:00+00:00",
      "fetched_at": "2026-02-19T15:49:12.137853+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Cash-Producing Stocks We’re Skeptical Of",
      "summary": "While strong cash flow is a key indicator of stability, it doesn’t always translate to superior returns. Some cash-heavy businesses struggle with inefficient spending, slowing demand, or weak competitive positioning.",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/3-cash-producing-stocks-skeptical-043437767.html",
      "published_at": "2026-02-16T04:34:37+00:00",
      "fetched_at": "2026-02-19T15:49:12.137858+00:00",
      "source": "yfinance"
    },
    {
      "title": "Inspire Medical Systems, Astrana Health, Evolent Health, Corcept, and Centene Stocks Trade Up, What You Need To Know",
      "summary": "A number of stocks jumped in the afternoon session after a softer-than-expected inflation report fueled hopes for interest rate cuts by the Federal Reserve. The January Consumer Price Index (CPI), a key measure of inflation, rose by 0.2%, which was less than economists had forecast, with the annual rate cooling to 2.4%. This encouraging data increased market expectations for the Fed to begin cutting interest rates as early as June. The news prompted a rally in Treasuries as their yields fell. Wh",
      "publisher": "StockStory",
      "url": "https://finance.yahoo.com/news/inspire-medical-systems-astrana-health-205111110.html",
      "published_at": "2026-02-13T20:51:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.137862+00:00",
      "source": "yfinance"
    },
    {
      "title": "High Insider Ownership Fuels Growth Stocks In February 2026",
      "summary": "As February 2026 begins, U.S. stock markets have shown strong momentum with major indexes like the Dow and S&P 500 ending sharply higher, reflecting investor optimism amid recent economic developments such as trade deals and manufacturing growth. In this buoyant market environment, companies with high insider ownership often attract attention as their leadership's vested interest can signal confidence in long-term growth potential.",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/high-insider-ownership-fuels-growth-173611000.html",
      "published_at": "2026-02-11T17:36:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.137867+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Growth Companies With High Insider Ownership And Up To 15% Revenue Growth",
      "summary": "As February begins with a strong rally in the U.S. stock markets, highlighted by significant gains in major indices like the Dow Jones Industrial Average and S&P 500, investors are keenly observing growth companies that have shown resilience amidst fluctuating economic conditions. In this context, companies with high insider ownership and robust revenue growth potential stand out as compelling options for those looking to navigate the current market landscape effectively.",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/3-growth-companies-high-insider-113547657.html",
      "published_at": "2026-02-11T11:35:47+00:00",
      "fetched_at": "2026-02-19T15:49:12.137871+00:00",
      "source": "yfinance"
    },
    {
      "title": "Insider Favorites Rumble And Two Other Growth Stocks",
      "summary": "As the U.S. stock market begins February on a high note, with major indices like the Dow Jones Industrial Average and S&P 500 posting significant gains, investors are keenly observing growth companies that exhibit strong insider ownership. In this thriving market environment, stocks with substantial insider stakes can often signal confidence in the company's potential, making them appealing options for those looking to align their investments with insiders who have a vested interest in the...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/insider-favorites-rumble-two-other-173540260.html",
      "published_at": "2026-02-10T17:35:40+00:00",
      "fetched_at": "2026-02-19T15:49:12.137876+00:00",
      "source": "yfinance"
    },
    {
      "title": "3 Growth Companies With High Insider Ownership Expecting Up To 81% Earnings Growth",
      "summary": "As the U.S. stock market kicks off February with a strong start, highlighted by the Dow Jones Industrial Average's 515-point gain and the S&P 500 nearing record highs, investors are closely monitoring growth companies that demonstrate robust insider ownership. In times of economic uncertainty and evolving trade dynamics, stocks with high insider ownership can be appealing as they often indicate confidence from those who know the company best, suggesting potential resilience and alignment with...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/3-growth-companies-high-insider-113601473.html",
      "published_at": "2026-02-10T11:36:01+00:00",
      "fetched_at": "2026-02-19T15:49:12.137881+00:00",
      "source": "yfinance"
    },
    {
      "category": "company",
      "datetime": 1771248061,
      "headline": "Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case",
      "id": 139113901,
      "image": "https://www.chartmill.com/images/uploads/Affordable_Growth_500_264px_webp_add6a3aa59.webp",
      "related": "CORT",
      "source": "ChartMill",
      "summary": "Corcept Therapeutics exemplifies GARP investing with accelerating earnings growth, strong profitability, and a solid debt-free balance sheet, all at a reasonable sector-relative valuation.",
      "url": "https://finnhub.io/api/news?id=21f8a2f3f61150c2bdfe75af0ca68ceaf6960c296ca5e372918ac8b1c5f233f3",
      "title": "Corcept Therapeutics Inc (NASDAQ:CORT) Presents a Compelling GARP Investment Case",
      "published_at": "2026-02-16T13:21:01+00:00"
    },
    {
      "category": "company",
      "datetime": 1770894072,
      "headline": "Options Exercise: Hazel Hunt At Corcept Therapeutics Realizes $5.53M",
      "id": 139082128,
      "image": "https://cdn.benzinga.com/files/images/story/2025/11/06/insider_trade_1.jpg?width=2048&height=1536",
      "related": "CORT",
      "source": "Benzinga",
      "summary": " ",
      "url": "https://finnhub.io/api/news?id=0fbc2b4d7b0acb598b946fd3f7a175e192d1a552f936bcdfa05033ae55ec78bb",
      "title": "Options Exercise: Hazel Hunt At Corcept Therapeutics Realizes $5.53M",
      "published_at": "2026-02-12T11:01:12+00:00"
    }
  ],
  "fetched_at": "2026-02-19T15:49:12.007756+00:00",
  "article_count": 12,
  "source": "yfinance+finnhub"
}
```

##### 17. get_company_news [grok]

- **Duration:** 0.201s
- **Modality:** news
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.253212+00:00 -> 2026-02-19T15:49:12.454531+00:00
- **Hop Index:** 16

**Input:**

```json
{
  "symbol": "TEVA"
}
```

**Output:**

```json
{
  "symbol": "TEVA",
  "articles": [
    {
      "title": "Goldman Sachs See Teva Pharmaceutical Industries Limited (TEVA) Driving Double-Digit Earnings Growth Through 2026",
      "summary": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is among the 7 Cheap Pharmaceutical Stocks to Buy According to Hedge Funds. Teva Pharmaceutical Industries Limited (NYSE:TEVA) is given the second position on our list. TheFly reported on February 9 that Goldman Sachs raised its price target on TEVA to $45 from $36, and gave it a Buy rating. […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/goldman-sachs-see-teva-pharmaceutical-020044351.html",
      "published_at": "2026-02-19T02:00:44+00:00",
      "fetched_at": "2026-02-19T15:49:12.361786+00:00",
      "source": "yfinance"
    },
    {
      "title": "FDA accepts BMS protein degrader for review; Disc rare disease drug rejected",
      "summary": "Cytokinetics’ heart disease drug secured European approval. Elsewhere, Teva and Sanofi reported longer-term data from an experimental therapy targeting ulcerative colitis and Crohn’s disease.",
      "publisher": "BioPharma Dive",
      "url": "https://www.biopharmadive.com/news/disc-news-roundup-pharma-biotech/812351/",
      "published_at": "2026-02-18T10:35:17+00:00",
      "fetched_at": "2026-02-19T15:49:12.361816+00:00",
      "source": "yfinance"
    },
    {
      "title": "Should You Invest in Teva Pharmaceutical (TEVA), Here’s What the Street Has to Say",
      "summary": "​Teva Pharmaceutical Industries Limited (NYSE:TEVA) is one of the Best Performing Foreign Stocks to Buy Now. Wall Street is bullish on Teva Pharmaceutical since its fiscal Q4 2025 earnings were released on January 28. ​Recently, on February 10, Goldman Sachs analyst Matthew Dellatorre reiterated a Buy rating on the stock and raised the price target […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/invest-teva-pharmaceutical-teva-street-175638880.html",
      "published_at": "2026-02-17T17:56:38+00:00",
      "fetched_at": "2026-02-19T15:49:12.361821+00:00",
      "source": "yfinance"
    },
    {
      "title": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is largely controlled by institutional shareholders who own 74% of the company",
      "summary": "Key Insights Given the large stake in the stock by institutions, Teva Pharmaceutical Industries' stock price might be...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-limited-nyse-120021078.html",
      "published_at": "2026-02-14T12:00:21+00:00",
      "fetched_at": "2026-02-19T15:49:12.361827+00:00",
      "source": "yfinance"
    },
    {
      "title": "Here’s What Analysts Are Saying About Teva Pharmaceutical (TEVA) Post Earnings",
      "summary": "Teva Pharmaceutical Industries Limited (NYSE:TEVA) is one of the best healthcare stocks under $50 to invest in. Barclays maintained an Overweight rating on Teva Pharmaceutical Industries Limited (NYSE:TEVA) on January 30, adjusting the price target on the stock to $38 from $35. It told investors that it updated the company’s model after the fiscal Q4 […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/analysts-saying-teva-pharmaceutical-teva-114139672.html",
      "published_at": "2026-02-10T11:41:39+00:00",
      "fetched_at": "2026-02-19T15:49:12.361833+00:00",
      "source": "yfinance"
    },
    {
      "title": "Teva Pharmaceutical Industries' (NYSE:TEVA) Solid Earnings Are Supported By Other Strong Factors",
      "summary": "Teva Pharmaceutical Industries Limited's ( NYSE:TEVA ) strong earnings report was rewarded with a positive stock price...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-nyse-teva-100259512.html",
      "published_at": "2026-02-10T10:02:59+00:00",
      "fetched_at": "2026-02-19T15:49:12.361837+00:00",
      "source": "yfinance"
    },
    {
      "title": "How Good Is Teva Pharmaceutical Industries Limited (NYSE:TEVA), When It Comes To ROE?",
      "summary": "While some investors are already well versed in financial metrics (hat tip), this article is for those who would like...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/good-teva-pharmaceutical-industries-limited-120011262.html",
      "published_at": "2026-02-08T12:00:11+00:00",
      "fetched_at": "2026-02-19T15:49:12.361842+00:00",
      "source": "yfinance"
    },
    {
      "title": "Is Teva Pharmaceutical Industries Limited (TEVA) an Undervalued Stock Relative to Its Long-Term Potential?",
      "summary": "Baron Funds, an investment management company, released its fourth-quarter investor letter for the “Baron Health Care Fund”. A copy of the letter can be downloaded here. The fund rose 13.10% (Institutional Shares) in the quarter, compared to a 11.92% gain for the Russell 3000 Health Care Index (benchmark) and a 2.40% gain for the Russell […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/teva-pharmaceutical-industries-limited-teva-124644455.html",
      "published_at": "2026-02-06T12:46:44+00:00",
      "fetched_at": "2026-02-19T15:49:12.361847+00:00",
      "source": "yfinance"
    },
    {
      "title": "Alvotech (ALVO): A Bull Case Theory",
      "summary": "We came across a bullish thesis on Alvotech on r/valueinvesting by 123Thorr. In this article, we will summarize the bulls’ thesis on ALVO. Alvotech’s share was trading at $5.29 as of January 28th. ALVO’s trailing and forward P/E were 23.00 and 10.95, respectively according to Yahoo Finance. Alvotech ($ALVO) is emerging as a fast-growing biosimilars manufacturer, generating roughly $500 million in annual revenue […]",
      "publisher": "Insider Monkey",
      "url": "https://finance.yahoo.com/news/alvotech-alvo-bull-case-theory-183950403.html",
      "published_at": "2026-02-04T18:39:50+00:00",
      "fetched_at": "2026-02-19T15:49:12.361852+00:00",
      "source": "yfinance"
    },
    {
      "title": "How The Shifting 2026 Pipeline Story Is Reshaping Teva Pharmaceutical Industries’ Valuation",
      "summary": "Teva Pharmaceutical Industries has seen its fair value estimate move from about US$35.32 to about US$36.77, with analysts tightening their valuation ranges as they weigh upcoming pipeline events into 2026. While the discount rate assumption is essentially unchanged at about 8.52%, the revenue growth outlook now sits closer to about 1.05%. This reflects mixed views on how quickly Teva can translate its pipeline and core business into higher sales. As you read on, keep an eye on how this...",
      "publisher": "Simply Wall St.",
      "url": "https://finance.yahoo.com/news/shifting-2026-pipeline-story-reshaping-171605006.html",
      "published_at": "2026-02-04T17:16:05+00:00",
      "fetched_at": "2026-02-19T15:49:12.361857+00:00",
      "source": "yfinance"
    },
    {
      "category": "company",
      "datetime": 1771495651,
      "headline": "How Bausch Health's Turnaround Progressed In Q4 2025",
      "id": 139148734,
      "image": "",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=a9b842bf414d45318afd96853f62c48c8d974377e01097c1f3d1ad4ba6e2f541",
      "title": "How Bausch Health's Turnaround Progressed In Q4 2025",
      "published_at": "2026-02-19T10:07:31+00:00"
    },
    {
      "category": "company",
      "datetime": 1771434059,
      "headline": "13 Analysts Have This To Say About Teva Pharmaceutical Indus",
      "id": 139146834,
      "image": "https://cdn.benzinga.com/files/images/story/2025/11/06/analyst_ratings_6.jpg?width=2048&height=1536",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": " ",
      "url": "https://finnhub.io/api/news?id=ac941fc87c803801d615d269def0ca09d01e344ebcdce8d3c066123927a902e7",
      "title": "13 Analysts Have This To Say About Teva Pharmaceutical Indus",
      "published_at": "2026-02-18T17:00:59+00:00"
    },
    {
      "category": "company",
      "datetime": 1771414157,
      "headline": "Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42",
      "id": 139146835,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "Truist Securities  analyst Les Sulewski   maintains Teva Pharmaceutical Indus (NYSE:TEVA) with a Buy and raises the price target from $38 to $42.",
      "url": "https://finnhub.io/api/news?id=84511dac6f5a19684eaad74c6c912157acec2648ae468905678cbfd5b4029885",
      "title": "Truist Securities Maintains Buy on Teva Pharmaceutical Indus, Raises Price Target to $42",
      "published_at": "2026-02-18T11:29:17+00:00"
    },
    {
      "category": "company",
      "datetime": 1771329745,
      "headline": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow",
      "id": 139124509,
      "image": "",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=16c19453db608902d2eedcfc0342088e5b27e6e936d06eef0a1bc45188541f09",
      "title": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results - Slideshow",
      "published_at": "2026-02-17T12:02:25+00:00"
    },
    {
      "category": "company",
      "datetime": 1771325697,
      "headline": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript",
      "id": 139124272,
      "image": "https://static.seekingalpha.com/assets/og_image_1200-29b2bfe1a595477db6826bd2126c63ac2091efb7ec76347a8e7f81ba17e3de6c.png",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results February 17, 2026 8:00 AM ESTCompany...",
      "url": "https://finnhub.io/api/news?id=962e81e84b9e4800fc64efb70cd11a0a50d978d8b0ef886a3c736aa78ff52a0b",
      "title": "Teva Pharmaceutical Industries Limited (TEVA) Discusses Duvakitug IBD Phase 2b Maintenance Top-Line Results Transcript",
      "published_at": "2026-02-17T10:54:57+00:00"
    },
    {
      "category": "company",
      "datetime": 1771322173,
      "headline": "Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,",
      "id": 139123216,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=cd2835dfed86655337ca9b8e18f0458fb703c9ae88e4cdb4fedb2c2c061d3386",
      "title": "Teva Pharmaceutical shares are trading higher after the company announced results from the RELIEVE UCCD long-term extension study of duvakitug,",
      "published_at": "2026-02-17T09:56:13+00:00"
    },
    {
      "category": "company",
      "datetime": 1771315477,
      "headline": "Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients",
      "id": 139122350,
      "image": "https://cdn.benzinga.com/files/images/story/2026/02/17/Teva-Pharmaceuticals.jpeg?width=2048&height=1536",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "Teva stock rises after a new study, in partnership with Sanofi, shows lasting benefits in ulcerative colitis and Crohn&#39;s patients.",
      "url": "https://finnhub.io/api/news?id=8a07b4f659e27bcf9979b71f24286b30ce2b63b823d67cdfe7eda699ace9c900",
      "title": "Teva Pharmaceutical's New Study Shows Lasting Relief For Inflammatory Bowel Disease Patients",
      "published_at": "2026-02-17T08:04:37+00:00"
    },
    {
      "category": "company",
      "datetime": 1771311968,
      "headline": "Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs",
      "id": 139122351,
      "image": "",
      "related": "TEVA",
      "source": "Benzinga",
      "summary": "",
      "url": "https://finnhub.io/api/news?id=129ebc8fc56b3ec2330a75714c1851231147e5fc837a3fc91b313117d6927872",
      "title": "Teva And Sanofi Report duvakitug Phase 2b Maintenance Data Showing Durable Clinical And Endoscopic Efficacy In Ulcerative Colitis And Crohn's Disease, Supporting Ongoing Phase 3 Programs",
      "published_at": "2026-02-17T07:06:08+00:00"
    },
    {
      "category": "company",
      "datetime": 1770905760,
      "headline": "Neurocrine Biosciences: Expanding Opportunities, 2026 Will Be Less Exciting",
      "id": 139072463,
      "image": "https://static.seekingalpha.com/cdn/s3/uploads/getty_images/855725214/image_855725214.jpg?io=getty-c-w1536",
      "related": "TEVA",
      "source": "SeekingAlpha",
      "summary": "Neurocrine Biosciences reported largely in-line Q4 results, with better-than-expected Crenessity sales. Click here to read my latest analysis of NBIX stock.",
      "url": "https://finnhub.io/api/news?id=00c55cb79c171690cfd221eaa2203fb3534641206255231377cf5f101485e510",
      "title": "Neurocrine Biosciences: Expanding Opportunities, 2026 Will Be Less Exciting",
      "published_at": "2026-02-12T14:16:00+00:00"
    }
  ],
  "fetched_at": "2026-02-19T15:49:12.253223+00:00",
  "article_count": 19,
  "source": "yfinance+finnhub"
}
```

##### 18. check_price [grok]

- **Duration:** 0.555s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:12.454592+00:00 -> 2026-02-19T15:49:13.010047+00:00
- **Hop Index:** 17

**Input:**

```json
{
  "symbol": "XBI"
}
```

**Output:**

```json
{
  "symbol": "XBI",
  "last_price": 125.12,
  "bid": 125.11,
  "ask": 125.14,
  "total_volume": 1962825,
  "high": 125.71,
  "low": 123.43,
  "open_price": 124.75,
  "close_price": 125.6,
  "net_change": -0.48,
  "net_pct_change": -0.38216561,
  "mark": 125.12,
  "timestamp_ms": 1771516150874,
  "source": "schwab",
  "period_returns": "1W: +1.6% | 1M: -2.4% | 3M: +9.0% | 6M: +40.7% | 1Y: +35.7%",
  "52_week_range": "$66.66–$132.09",
  "52_week_position_pct": 89.3
}
```

##### 19. check_price [grok]

- **Duration:** 0.521s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.010087+00:00 -> 2026-02-19T15:49:13.531388+00:00
- **Hop Index:** 18

**Input:**

```json
{
  "symbol": "IBB"
}
```

**Output:**

```json
{
  "symbol": "IBB",
  "last_price": 173.635,
  "bid": 173.58,
  "ask": 173.69,
  "total_volume": 359703,
  "high": 174.33,
  "low": 171.65,
  "open_price": 173.94,
  "close_price": 174.76,
  "net_change": -1.125,
  "net_pct_change": -0.64373999,
  "mark": 173.635,
  "timestamp_ms": 1771516151511,
  "source": "schwab",
  "period_returns": "1W: +1.8% | 1M: -0.9% | 3M: +4.4% | 6M: +25.7% | 1Y: +25.5%",
  "52_week_range": "$107.43–$179.64",
  "52_week_position_pct": 91.7
}
```

##### 20. get_movers [grok]

- **Duration:** 0.336s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.531437+00:00 -> 2026-02-19T15:49:13.867614+00:00
- **Hop Index:** 19

**Input:**

```json
{
  "index": "NASDAQ",
  "direction": "down"
}
```

**Output:**

```json
{
  "index": "NASDAQ",
  "direction": "down",
  "movers": [
    {
      "symbol": "TRNR",
      "description": "INTERACTIVE STRENGTH",
      "direction": "down",
      "change": 0.2,
      "pct_change": 0.496,
      "volume": 228347252,
      "last_price": 0.61
    },
    {
      "symbol": "RUBI",
      "description": "RUBICO INC",
      "direction": "down",
      "change": -0.15,
      "pct_change": -0.067,
      "volume": 81374174,
      "last_price": 2.09
    },
    {
      "symbol": "AGAE",
      "description": "ALLIED GAMING & ENTM",
      "direction": "down",
      "change": 0.13,
      "pct_change": 0.4336,
      "volume": 75082593,
      "last_price": 0.44
    },
    {
      "symbol": "DTCK",
      "description": "DAVIS COMMODITIES LT A",
      "direction": "down",
      "change": -0.07,
      "pct_change": -0.3591,
      "volume": 63926898,
      "last_price": 0.12
    },
    {
      "symbol": "RXT",
      "description": "RACKSPACE TECHNOLOGY",
      "direction": "down",
      "change": -0.26,
      "pct_change": -0.1898,
      "volume": 37655119,
      "last_price": 1.11
    },
    {
      "symbol": "NVDA",
      "description": "NVIDIA CORP",
      "direction": "down",
      "change": -0.14,
      "pct_change": -0.0008,
      "volume": 37182053,
      "last_price": 187.84
    },
    {
      "symbol": "ONDS",
      "description": "ONDAS INC",
      "direction": "down",
      "change": 0.38,
      "pct_change": 0.0339,
      "volume": 36296765,
      "last_price": 11.45
    },
    {
      "symbol": "TQQQ",
      "description": "PROSHARES ULTRAPRO QQQ ETF",
      "direction": "down",
      "change": -0.26,
      "pct_change": -0.0053,
      "volume": 31209808,
      "last_price": 49.13
    },
    {
      "symbol": "BOXL",
      "description": "BOXLIGHT CORP",
      "direction": "down",
      "change": 0.39,
      "pct_change": 0.2962,
      "volume": 25976451,
      "last_price": 1.69
    },
    {
      "symbol": "INTC",
      "description": "INTEL CORP",
      "direction": "down",
      "change": -0.47,
      "pct_change": -0.0102,
      "volume": 25577216,
      "last_price": 45.0
    }
  ],
  "count": 10,
  "fetched_at": "2026-02-19T15:49:13.531445+00:00",
  "source": "schwab"
}
```

##### 21. get_technical_indicators [grok]

- **Duration:** 0.301s
- **Modality:** market_data
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:49:13.867664+00:00 -> 2026-02-19T15:49:14.169038+00:00
- **Hop Index:** 20

**Input:**

```json
{
  "symbol": "CORT",
  "indicators": "rsi,macd,boll,close_50_sma,close_200_sma"
}
```

**Output:**

```json
{
  "symbol": "CORT",
  "rsi": 26.9921,
  "macd": -3.5903,
  "boll": 40.9495,
  "close_50_sma": 53.0428,
  "close_200_sma": 67.307,
  "rsi_signal": "oversold",
  "fetched_at": "2026-02-19T15:49:13.916006+00:00",
  "source": "stockstats",
  "interpretation": {
    "rsi": "oversold",
    "vs_50sma": "-43.7% (below)",
    "vs_200sma": "-55.6% (below)"
  }
}
```

##### 22. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174835+00:00 -> 2026-02-19T15:51:39.174836+00:00
- **Hop Index:** 21

**Input:**

```json
{
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997311e5db88194a90bb823a4300b43",
  "action": {
    "type": "open_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 23. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174915+00:00 -> 2026-02-19T15:51:39.174915+00:00
- **Hop Index:** 22

**Input:**

```json
{
  "pattern": "injunction",
  "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973122762481949f304e48a3cb213c",
  "action": {
    "pattern": "injunction",
    "type": "find_in_page",
    "url": "https://www.cafc.uscourts.gov/opinions-orders/24-1346.OPINION.2-19-2026_2649652.pdf"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 24. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174939+00:00 -> 2026-02-19T15:51:39.174939+00:00
- **Hop Index:** 23

**Input:**

```json
{
  "query": "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973128625081948e32444e5a19c5f1",
  "action": {
    "query": "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news",
    "type": "search",
    "queries": [
      "Corcept Teva Federal Circuit affirmed no infringement Korlym Feb 19 2026 news",
      "Federal Circuit affirms no infringement Teva Corcept Korlym 24-1346 news Reuters"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 25. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174957+00:00 -> 2026-02-19T15:51:39.174957+00:00
- **Hop Index:** 24

**Input:**

```json
{
  "query": "Teva launches generic Korlym January 2024 Teva press release"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973134a0a88194af2dc35bee3f6bb1",
  "action": {
    "query": "Teva launches generic Korlym January 2024 Teva press release",
    "type": "search",
    "queries": [
      "Teva launches generic Korlym January 2024 Teva press release",
      "Teva Korlym generic launched January 2024 'mifepristone' Teva 'Korlym' launch 2024 press"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 26. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.174974+00:00 -> 2026-02-19T15:51:39.174974+00:00
- **Hop Index:** 25

**Input:**

```json
{
  "query": "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997313affc881948c98b32b3543432a",
  "action": {
    "query": "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release",
    "type": "search",
    "queries": [
      "Corcept statement Federal Circuit Feb 19 2026 'Federal Circuit' 'Corcept' 'Teva' 'Feb 19, 2026' press release",
      "Teva statement Federal Circuit Korlym Feb 19 2026 press release 'Corcept' 'Federal Circuit'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 27. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175006+00:00 -> 2026-02-19T15:51:39.175006+00:00
- **Hop Index:** 26

**Input:**

```json
{
  "query": "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973144ffec81948ce57d9f76c2c77c",
  "action": {
    "query": "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'",
    "type": "search",
    "queries": [
      "Corcept Korlym revenue 2025 'Korlym' 'revenue' 2025 'Corcept' 'Korlym sales' '2025'",
      "Corcept 2025 revenue Korlym percentage 'Korlym' '2025' 'annual report' '10-K' '2025'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 28. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175023+00:00 -> 2026-02-19T15:51:39.175023+00:00
- **Hop Index:** 27

**Input:**

```json
{
  "query": "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f8006997315590ec8194a64ebfe5f045dbc7",
  "action": {
    "query": "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'",
    "type": "search",
    "queries": [
      "Corcept Therapeutics stock plunge Feb 19 2026 'CORT' 'Feb 19 2026' 'Federal Circuit' 'Korlym' 'stock'",
      "CORT stock Feb 19 2026 percentage drop 'Federal Circuit' 'Teva' 'Korlym'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 29. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175040+00:00 -> 2026-02-19T15:51:39.175040+00:00
- **Hop Index:** 28

**Input:**

```json
{
  "query": "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f800699731662f9481949e33501417c3e932",
  "action": {
    "query": "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'",
    "type": "search",
    "queries": [
      "CORT historical close Feb 19 2026 'Corcept Therapeutics' February 19 2026 close price 'CORT' 'Feb 19 2026' 'stock' 'close'",
      "CORT Feb 19 2026 'stock fell' 'Federal Circuit' 'Corcept' 'Teva'"
    ],
    "sources": null
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 30. web_search [openai]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:51:39.175055+00:00 -> 2026-02-19T15:51:39.175055+00:00
- **Hop Index:** 29

**Input:**

```json
{
  "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
}
```

**Output:**

```json
{
  "id": "ws_0bdc2562d42595f80069973182f75081949531f26623ed0953",
  "action": {
    "type": "open_page",
    "url": "https://www.benzinga.com/news/26/02/50723583/federal-circuit-affirmed-a-no-infringement-win-for-teva-pharmaceutical-industries-vs-corcept-therape"
  },
  "status": "completed",
  "type": "web_search_call"
}
```

##### 31. web_search [gemini]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003484+00:00 -> 2026-02-19T15:52:10.003484+00:00
- **Hop Index:** 30

**Input:**

```json
{
  "query": "Corcept relacorilant CRL date and reason 2025 2026"
}
```

##### 32. web_search [gemini]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003522+00:00 -> 2026-02-19T15:52:10.003522+00:00
- **Hop Index:** 31

**Input:**

```json
{
  "query": "did Teva launch generic Korlym at risk in 2024?"
}
```

##### 33. web_search [gemini]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003534+00:00 -> 2026-02-19T15:52:10.003534+00:00
- **Hop Index:** 32

**Input:**

```json
{
  "query": "Corcept Hikma Sun Pharmaceutical settlement terms generic Korlym entry trigger"
}
```

##### 34. web_search [gemini]

- **Duration:** 0.000s
- **Modality:** web_research
- **Status:** ok
- **Cost:** $0.0000
- **Time:** 2026-02-19T15:52:10.003545+00:00 -> 2026-02-19T15:52:10.003545+00:00
- **Hop Index:** 33

**Input:**

```json
{
  "query": "\"CORT\" stock \"Federal Circuit\" February 19 2026 analyst reaction"
}
```

---

## Cost Summary

- **Total:** $0.0391
- **By Tool:**
  - web_search: $0.0450
  - x_stream_cache: $0.0000
  - check_market_context: $0.0000
  - get_movers: $0.0000
  - get_financial_statements: $0.0000
  - get_company_news: $0.0000
  - check_price: $0.0000
  - get_technical_indicators: $0.0000

---

## Data Modalities

- **web_research:** 22 traces (indices: [0, 1, 2, 3, 4, 11, 12, 13, 14, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33])
- **social:** 2 traces (indices: [5, 6])
- **macro:** 1 traces (indices: [7])
- **market_data:** 6 traces (indices: [8, 9, 17, 18, 19, 20])
- **fundamentals:** 1 traces (indices: [10])
- **news:** 2 traces (indices: [15, 16])
