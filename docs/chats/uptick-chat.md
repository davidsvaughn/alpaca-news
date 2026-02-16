Q:

what is tick by tick trade data, for stocks? I want to compute uptick/downtick volume.  using this API: "https://tylerebowers.github.io/Schwabdev/"

---

A:

### What “tick-by-tick” would mean here

For uptick/downtick volume, you ideally need **trade prints** (“time & sales”): each executed trade with at least `(timestamp, price, size)`.

### What Schwabdev gives you

Schwabdev is a wrapper around Schwab’s REST + **streaming** interfaces. In practice, for equities most people start with the streaming **Level One Equities** feed, e.g. `streamer.level_one_equities(...)`. ([community.wolfram.com][1])

**Important:** Level One Equities is primarily a **quote/summary update feed** (bid/ask/last, etc.). Even if it includes “last trade price” and “last trade size”, it may **not** be a guaranteed “one message per trade print” feed. That means you can miss trades (multiple executions can occur between updates) and your “tick-by-tick” reconstruction can be imperfect.

### Does Schwab’s streaming API have true Time & Sales?

* Some community discussion suggests **time & sales streaming may be absent / unclear** in Schwab’s API (at least compared to old TDA streaming). ([Reddit][2])
* Meanwhile, `schwab-py`’s streaming docs claim the Schwab streaming API provides “time of sale data”. ([schwab-py.readthedocs.io][3])
  Net: this is a point of real-world ambiguity; you should not assume you’re getting true prints unless you can identify an explicit “time & sales / timesale” service and verify the message schema.

### If you only have Level One updates, what you can do

You can compute **an approximation** of uptick/downtick volume using *changes in the reported last trade price* and the accompanying size field (if provided), but it won’t be perfect.

Algorithm (tick test):

1. Maintain:

   * `prev_price`
   * `last_nonzero_dir` in {+1, -1}
2. For each incoming event with `price` and `size`:

   * If `price > prev_price`: classify as **uptick**, add `size` to `uptick_volume`, set `last_nonzero_dir=+1`
   * If `price < prev_price`: classify as **downtick**, add to `downtick_volume`, set `last_nonzero_dir=-1`
   * If `price == prev_price`: classify using `last_nonzero_dir` (tick-rule for “zero ticks”), and add `size` accordingly
3. Update `prev_price = price`

Caveats:

* If the feed aggregates multiple trades into one update, “size” might not represent a single print.
* If size is “last size” (not “accumulated size since last update”), you can’t reliably compute volume without additional fields.

### What to check next (quick, concrete)

In Schwabdev’s streaming message payloads for Level One Equities, confirm you have:

* last trade **price**
* last trade **size** (and whether it’s incremental vs just “last print size”)
* a reliable **timestamp/sequence**

If you paste one sample streaming message (one JSON blob), I can tell you immediately whether you can compute a defensible uptick/downtick volume from it, or whether you’re missing the necessary “print-level” fields.

[1]: https://community.wolfram.com/groups/-/m/t/3250713?utm_source=chatgpt.com "Real-time financial data analysis with Python/Wolfram ..."
[2]: https://www.reddit.com/r/Schwab/comments/1ebwz47/schwab_developer_api_time_and_sales_streaming/?utm_source=chatgpt.com "Schwab Developer API Time and Sales Streaming"
[3]: https://schwab-py.readthedocs.io/en/latest/streaming.html "Streaming Client — schwab-py  documentation"

---

Q:

check the the "Schwab Streamer API": `docs/refs/SchwabStreamerAPI.pdf`

---

A:

I read `docs/refs/SchwabStreamerAPI.pdf` and found this...

That PDF is enough to tell the key point: the Schwab Streamer “Market Data Production” contract documents **Level One quote/trade-summary services** (e.g., `LEVELONE_EQUITIES`) with fields like **Last Price**, **Last Size**, and **Trade Time**, plus book/chart/screener services—but it does **not** describe a dedicated **Time & Sales / Timesale / one-message-per-executed-trade** service in the way you’d need for true tick-by-tick prints. 

### What that means for “tick-by-tick” and uptick/downtick volume

* **True tick-by-tick uptick/downtick volume** requires **every trade print** with `(timestamp, price, size)` emitted as a distinct event.
* The streamer contract you uploaded is consistent with getting **updates that include the latest trade fields** (last price/last size/trade time) as part of Level One, but Level One is typically **not guaranteed to emit every print**. It can be throttled/aggregated.

So you likely cannot compute *true* uptick/downtick volume from Schwab streamer alone unless there’s an additional undocumented/other service in the API that emits actual prints.

### Best-available approximation from `LEVELONE_EQUITIES`

If your stream messages include:

* `lastPrice`
* `totalVolume`
* `tradeTime` (or similar)

Then compute **delta-volume per update** and assign it using tick-test direction:

1. Keep `prev_last_price`, `prev_total_volume`, and `last_nonzero_dir`.
2. For each update:

   * `dv = max(0, totalVolume - prev_total_volume)`  (shares since last update)
   * Direction:

     * if `lastPrice > prev_last_price`: `uptick += dv`, `last_nonzero_dir=+1`
     * if `lastPrice < prev_last_price`: `downtick += dv`, `last_nonzero_dir=-1`
     * if equal: allocate `dv` using `last_nonzero_dir`
3. Update previous values.

**Why this is only a proxy:** multiple trades at different prices can occur between updates; you’ll dump all intervening volume into whichever direction the *lastPrice* moved.

### What would make it “good enough”

If your JSON blob shows a service that clearly emits **one event per trade** (often named like `TIMESALE_EQUITY` / `TIME_SALE` / `SALE` / “time of sale”), with explicit per-trade fields, then you can do proper tick-test on each print. If it’s only `LEVELONE_EQUITIES`, you’re in approximation land.

Paste:

* one subscription request you’re using, and
* 5–10 consecutive messages for a single symbol

and I’ll tell you definitively whether you’re seeing prints or just Level One updates, and which fields to use for `price`, `size`, and `timestamp`.

---

Q:

approximation might be good enough....

---

A:

You can make a decent proxy from Schwab’s **LEVELONE_EQUITIES** stream if (and only if) you have a monotonically increasing volume field like **`totalVolume`** (or equivalent) in each update. Using **`lastSize`** alone is usually worse because it’s just “size of the last reported trade,” not “volume since last update.”

## Best proxy: allocate *volume deltas* by tick direction

Maintain per symbol:

* `prev_last` (previous last trade price)
* `prev_total_vol` (previous cumulative volume)
* `last_nonzero_dir` in `{+1,-1}`

For each incoming update (for that symbol):

1. Extract:

   * `last = lastPrice`
   * `tv = totalVolume` (cumulative shares for the day/session)
   * `t = tradeTime` or message timestamp (optional, for bucketing)

2. Compute incremental volume since last update:

   * `dv = tv - prev_total_vol`
   * if `dv < 0`: set `dv = 0` and reset `prev_total_vol = tv` (handles resets / bad ordering)

3. Determine direction:

   * if `last > prev_last`: **uptick** → `up += dv`, set `last_nonzero_dir=+1`
   * if `last < prev_last`: **downtick** → `down += dv`, set `last_nonzero_dir=-1`
   * if `last == prev_last`: **zero tick** → allocate `dv` using `last_nonzero_dir` (if unknown yet, put in `unknown` bucket)

4. Update:

   * `prev_last = last`
   * `prev_total_vol = tv`

This gives you a proxy for “uptick/downtick volume” that behaves like cumulative delta, but with a known limitation: **all the volume between updates is assigned to the direction of the final last-price move**.

## Practical details that matter

### 1) Bucket by time window (recommended)

Rather than one running total forever, compute per bar window (e.g., 1s, 5s, 1m):

* `up_vol[bar] += dv` / `down_vol[bar] += dv`

This makes it usable as a feature (imbalance, delta, etc.) and reduces sensitivity to occasional weird updates.

### 2) Filter “bad” prints/updates if you have flags

If Schwab streamer includes trade/quote condition fields, you generally want to exclude auctions/corrections or at least keep them separate. If you don’t have flags, accept that the proxy is noisier at open/close.

### 3) Handle out-of-order messages

If timestamps/sequence aren’t guaranteed in order, you can:

* ignore updates where `tradeTime < prev_tradeTime` (per symbol), or
* keep a small reorder buffer (more complexity)

For a “good enough” proxy, the simplest safe move is: **ignore negative `dv` and ignore clearly backward times**.

### 4) Start-of-day reset

Cumulative volume resets daily. When you detect `tv` dropping a lot (or session changes), reset state for that symbol.

## If you *don’t* have totalVolume

Fallback options are weaker:

* **Use `lastSize` as dv**: allocate `lastSize` by tick direction. This is often misleading because you’ll miss prints and some updates won’t correspond 1:1 with trades.
* **Use bid/ask direction (quote test)** if you have bid/ask: classify trade as buy if last near ask, sell if last near bid. But you need actual prints; with only “last” updates it’s still approximate.

## Minimal output features you can compute from the proxy

Per bar:

* `up_vol`, `down_vol`
* `delta = up_vol - down_vol`
* `imbalance = delta / (up_vol + down_vol + 1e-9)`
* `delta_change` (first difference)
* rolling z-scores of delta/imbalance

If you paste one example streamer message (even a single JSON update), I can point to the exact field names to use for `lastPrice` and `totalVolume` and whether you’ll need symbol-specific parsing.
