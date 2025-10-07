## Task

You are an expert financial-news tagger. Classify a stock-related article strictly by the “News Type Schema (0–7)” below.  

### Rules:

* **Exactly one label** per article.
* Be conservative: if content is mixed, prefer the **most specific event-type** (e.g., “Corporate Event” beats “Stock Movement Explanation” if the article itself is the announcement).
* **No prose** in the output; **JSONL only** (one JSON object per line).

## Schema (0–7):

* **0 – Background / Informational (“Fluff”)**: evergreen/retrospective/educational; not event-based.  
* **1 – Analyst Action**: brokerage rating/price-target/coverage changes.  
* **2 – Corporate Event / Announcement**: company-led actions: offerings, leadership changes, M&A, partnerships, product launches, closures, expansions, contracts, regulatory submissions/approvals, trial starts/primary results.  
* **3 – Financial / Earnings Report**: revenue/EPS/guidance/results.  
* **4 – Market Activity / Sentiment Data**: options/short-interest/insider trades/RSI/volume anomalies.  
* **5 – Stock Movement Explanation (WIIM)**: post-hoc move explanation. Often single-symbol.  
* **6 – Macro / Market Recap**: index/sector roundups.  
* **7 – Technical / Chart Analysis**: chart-driven commentary.  

### Tie-breakers:

* If headline is “shares trading higher/lower after X,” label **5** unless the article is primarily the **announcement itself** (then **2/3**).
* Clinical trial topline results or FDA decisions → **2** (corporate event).
* A tiny PT tweak still counts as **1**.
* Market-wide daily wrap → **6**.
* Backtests/long-term hypothetical returns → **0**.

## Output format:

Output (JSONL; one object per line; no extra text):

```json
{{"id": <int>, "type": <int 0–7>}}
```

Return **only** valid JSONL.

---  

Articles is in JSON format below:

```json
{article}
```