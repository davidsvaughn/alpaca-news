# TradingAgents Research Notes

Running document of findings and insights from the [TradingAgents](TradingAgents/) repo
that inform our pipeline design.

---

## Architecture Overview

TradingAgents is a LangGraph-based multi-agent trading system with **4 phases**:

1. **Analysis** — 4 parallel analysts (Market, Fundamentals, News, Social Media)
2. **Investment Debate** — Bull vs Bear researchers debate, Research Manager judges
3. **Trading** — Trader agent synthesizes plan → BUY/HOLD/SELL
4. **Risk Management** — 3-way debate (Aggressive, Conservative, Neutral), Risk Manager judges

Each phase passes structured state forward. All debate agents see the full set of
4 analyst reports plus semantic-memory retrieval of 2 most similar past situations.

### Key Differences from Our Pipeline

| Aspect | TradingAgents | Our Pipeline |
|--------|---------------|--------------|
| Orchestration | LangGraph state machine | Sequential agent rounds (Grok→OpenAI→Gemini) |
| Analyst roles | Dedicated specialist agents | Single multi-tool agent per round |
| Debate | Explicit bull/bear + risk debate cycles | Agents see prior findings, no formal debate |
| Memory | Semantic memory of past decisions | Not yet implemented |
| Data fetching | Agents call tools on-demand | Pre-fetched + on-demand tools |

---

## Financial Data: What TradingAgents Provides to Agents

### 1. Price History (OHLCV)

- **Tool**: `get_stock_data(symbol, start_date, end_date)`
- **Source**: yfinance (primary), Alpha Vantage (fallback)
- **Format**: CSV with header metadata + OHLCV rows
- **Timeframe**: Agent-specified (no hard default — analyst chooses range)
- **Cache**: 15 years of daily data cached locally per symbol

**Key insight**: Agents choose their own lookback period. The data is NOT
pre-sliced to a single timeframe — the analyst decides what range matters.

### 2. Technical Indicators (13 indicators)

- **Tool**: `get_indicators(symbol, indicator, curr_date, look_back_days=30)`
- **Source**: `stockstats` library computed over yfinance OHLCV
- **Default lookback**: 30 days of indicator values (not just current value)
- **Available indicators**:
  - Trend: `close_50_sma`, `close_200_sma`, `close_10_ema`
  - Momentum: `macd`, `macds`, `macdh`, `rsi`
  - Volatility: `boll`, `boll_ub`, `boll_lb`, `atr`
  - Volume: `vwma`, `mfi`
- **Format**: Date-by-date values + description of what the indicator measures

**Key insight**: They provide a **time series** of indicator values (30 days
of RSI readings, not just today's RSI). This lets the LLM see trends and
divergences, not just a snapshot.

### 3. Fundamentals (comprehensive)

- **Tools**: `get_fundamentals()`, `get_balance_sheet()`, `get_cashflow()`, `get_income_statement()`
- **Source**: yfinance or Alpha Vantage
- **Financial statements**: Quarterly by default, multiple periods shown
- **Key fields** (27+):
  - Profile: Name, Sector, Industry, Market Cap
  - Valuation: P/E (TTM & forward), PEG, Price-to-Book, EPS
  - Performance: Revenue, Gross Profit, EBITDA, Net Income, FCF
  - Margins: Profit, Operating, ROE, ROA
  - Strength: Debt-to-Equity, Current Ratio, Beta, Dividend Yield
  - Reference: 52-Week High/Low, 50/200-Day Averages

**Key insight**: Full financial statements (not just summary ratios). Multiple
quarters of data to see trends.

### 4. News & Sentiment

- **Tools**: `get_news()`, `get_global_news(curr_date, look_back_days=7, limit=5)`, `get_insider_transactions()`
- **News**: Company-specific + macroeconomic (separate tools)
- **Global news default**: 7 days lookback, 5 articles
- **Insider transactions**: Latest only (no historical range)

### 5. What We Provide vs What They Provide

| Data Type | TradingAgents | Our Pipeline | Gap |
|-----------|---------------|--------------|-----|
| **Current price** | Via `get_stock_data` | Pre-fetched quote | ~Same |
| **Price history** | Agent-chosen range, OHLCV CSV | `get_price_history` tool (agent can call) | Similar capability |
| **Technical indicators** | 30-day time series per indicator | Current snapshot only (single value) | **Major gap** |
| **Fundamentals** | Full financial statements (multi-quarter) | Summary ratios only (P/E, EPS, Beta, etc.) | **Major gap** |
| **Balance sheet** | Yes (quarterly) | No | **Missing** |
| **Cash flow** | Yes (quarterly) | No | **Missing** |
| **Income statement** | Yes (quarterly) | No | **Missing** |
| **News** | Company + macro (separate) | Company news + FinnHub | Adequate |
| **Insider activity** | Latest transactions | Yes (yfinance) | Same |
| **Options** | No | Yes (Schwab ATM IV, put/call) | We're ahead |
| **Volume analysis** | No | Yes (regime detection, spike detection) | We're ahead |
| **Market context** | No | Yes (SPY, VIX, session info) | We're ahead |
| **Analyst ratings** | No | Yes (FinnHub) | We're ahead |
| **Earnings data** | No | Yes (FinnHub surprises + calendar) | We're ahead |
| **Social media** | Dedicated analyst (via news API) | x_search + x_stream_cache | We're ahead |
| **Semantic memory** | Yes (past decisions + outcomes) | Not yet | **Missing** |

---

## Key Takeaways for Our Enhancement

### What We Should Add

1. **Technical indicator time series** — Instead of just "RSI: 45.5", provide
   the last 30 days of RSI values so agents can see momentum shifts, divergence,
   and trend reversals. Same for MACD, Bollinger bands position, etc.

2. **Financial statements** — Add `get_balance_sheet()`, `get_cashflow()`,
   `get_income_statement()` tools (yfinance provides these for free). Even
   summary/highlight format would be valuable.

3. **Multi-period fundamentals** — Show not just current P/E but the trend
   (last 4 quarters of revenue, margins, etc.)

4. **Pre-fetched price chart summary** — Include a condensed price history
   in the pre-fetch (e.g. "1-week: +3.2%, 1-month: -8.1%, 3-month: +15.4%,
   52-week: -42.3%") so agents have trend context without needing tool calls.

### What We Already Do Better

- Real-time streaming data (Schwab)
- Options market activity (IV, put/call ratios)
- Volume regime detection
- Broad market context (SPY, VIX)
- Earnings calendar + surprises
- Analyst recommendation trends
- X/Twitter real-time stream + search

### Debate/Memory (Future Consideration)

TradingAgents uses explicit bull/bear debate cycles and BM25-based memory of
past decisions (see detailed sections below). The debate pattern is heavyweight
but the BM25 memory is cheap and could be adopted independently. Our sequential
pipeline with accumulating context achieves some of the debate effect (later
agents can challenge/refine earlier findings).

---

## Agent Prompt Patterns Worth Noting

### Analyst Instructions
- Market analyst: "Select up to **8 indicators** that provide complementary
  insights without redundancy" — forces the LLM to think about which indicators
  matter for the current situation rather than dumping all of them.

- Fundamentals analyst: "Write a comprehensive report of the company's
  fundamental information such as financial documents, company profile, basic
  company financials, and company financial history"

### Debate Patterns
- Bull/Bear researchers get all 4 reports + debate history + 2 similar past memories
- Research Manager: "Avoid defaulting to Hold simply because both sides have
  valid points; commit to a stance"
- Risk Manager: "Learn from past mistakes" with explicit past_memory injection

### Output Enforcement
- All agents share: "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**"
- "Do not simply state the trends are mixed, provide detailed and fine-grained
  analysis and insights"

---

## Backtesting & Exit Strategies

**TradingAgents has neither.** The system is a single-point-in-time decision
engine — `ta.propagate("NVDA", "2024-05-10")` produces one BUY/SELL/HOLD
signal. There is no:

- Backtesting engine or historical simulation loop
- Exit strategy logic (no stop-loss, take-profit, trailing stops, time-based exits)
- Position tracking or portfolio state
- P&L calculation or performance metrics
- Position sizing algorithms
- Order execution or broker integration

**Design intent**: TradingAgents is a research framework for studying LLM
decision-making quality, not a production trading system. Outcomes are evaluated
after the fact via the reflection system (see below), not simulated in advance.

**Implication for us**: All backtesting, exit logic, and position management
must be built from scratch. Nothing to borrow here.

---

## Memory System: BM25 Implementation

The doc above mentions "semantic memory" — here are the implementation details.

**File**: `tradingagents/agents/utils/memory.py`

Each agent type has its own memory bank:
- `bull_memory`, `bear_memory`, `trader_memory`, `invest_judge_memory`, `risk_manager_memory`

**How it works**:
1. Each memory entry is a `(situation, recommendation)` tuple — the market
   context at decision time paired with the agent's recommendation
2. Retrieval uses **BM25 lexical similarity** (via `rank_bm25` library) — no
   embeddings, no vector DB, no API calls
3. At decision time, the current situation is tokenized and matched against
   stored situations; the top-k most similar past memories are injected into
   the agent's prompt
4. Default: 2 most similar past situations retrieved per agent

**Why BM25 over embeddings?**
- Zero cost (no API calls, no GPU)
- Works offline
- Fast enough for real-time use
- Surprisingly effective for structured financial text where key terms
  (ticker, sector, indicator names) carry strong signal

**Potential adaptation**: We could use BM25 memory to store past backtest
scenarios and retrieve similar ones when evaluating new signals — lightweight
alternative to a full embedding pipeline.

---

## Post-Trade Reflection System

**File**: `tradingagents/graph/reflection.py`

After a trade executes and the actual P&L is known, you call:
```python
ta.reflect_and_remember(returns_losses=1000)  # e.g. +$1000
```

**What happens**:
1. The reflector LLM receives the original decision context + actual outcome
2. It analyzes contributing factors:
   - Which technical indicators were predictive vs misleading
   - Whether news/sentiment signals were correctly weighted
   - Whether fundamentals justified the decision
   - What the agent should do differently next time
3. Generated lessons are stored as new entries in each agent's BM25 memory
4. Future decisions retrieve these lessons when facing similar situations

**Key insight**: This creates a feedback loop — decisions improve over time
as the memory bank accumulates lessons from real outcomes. But it requires
**actual trade execution and P&L tracking** to feed the loop, which they
leave to the user.

**Potential adaptation**: After backtests complete, we could run a reflection
step that analyzes which exit strategy would have been optimal and why,
storing insights for future signal evaluation.

---

## Configuration Reference

```python
# TradingAgents default_config.py
DEFAULT_CONFIG = {
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.2",        # Research Manager & Risk Judge
    "quick_think_llm": "gpt-5-mini",    # All other agents
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "data_vendors": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    },
}
```

---

## File Reference (TradingAgents/)

- `tradingagents/agents/utils/core_stock_tools.py` — OHLCV price data tool
- `tradingagents/agents/utils/technical_indicators_tools.py` — 13 indicator tools
- `tradingagents/agents/utils/fundamental_data_tools.py` — Fundamentals + financial statements
- `tradingagents/agents/utils/news_data_tools.py` — News + insider transactions
- `tradingagents/agents/analysts/` — 4 specialist analyst agents
- `tradingagents/agents/researchers/` — Bull/Bear debate agents
- `tradingagents/agents/managers/` — Research Manager + Risk Manager (judges)
- `tradingagents/agents/trader/trader.py` — Final trading agent
- `tradingagents/dataflows/` — Data vendor abstraction (yfinance + Alpha Vantage)
- `tradingagents/graph/trading_graph.py` — LangGraph orchestration
- `tradingagents/default_config.py` — Configuration defaults
