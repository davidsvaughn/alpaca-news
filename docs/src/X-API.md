# X API v2 Data Source (x_stream)

> **Status**: Active (Basic tier, $200/mo)
> **Package**: None (raw HTTP via `requests` / `httpx`)
> **Client**: [`trader/xapi/`](trader/xapi/) (client, stream, rules, usage)
> **Service**: [`trader/online/x_stream_service.py`](trader/online/x_stream_service.py)
> **Env vars**: `X_BEARER_TOKEN`, `X_API_BASE_URL`
> **Demo**: [`demo/x_api_demo.py`](demo/x_api_demo.py) — `uv run python demo/x_api_demo.py` or `--rules` / `--burst`

---

## Overview

The X API v2 provides access to the **filtered stream** endpoint — a real-time WebSocket-like feed of tweets matching custom rules. We use this to capture social media sentiment around specific stocks immediately after a news event drops. This is **distinct from xAI/Grok's `x_search`** (see [X-AI.md](X-AI.md)) — this is the raw Twitter/X data feed.

Our approach is **burst-first**: short, time-bounded streaming sessions triggered by high-confidence news events. We never run a persistent stream.

---

## What We Currently Pull

### Filtered Stream (`GET /2/tweets/search/stream`)

**Our implementation**: `trader/xapi/stream.py` + `trader/online/x_stream_service.py`

Real-time tweets matching our filter rules. Each tweet includes:

| Field | Path | Description |
|-------|------|-------------|
| `id` | `data.id` | Tweet ID |
| `text` | `data.text` | Tweet text |
| `author_id` | `data.author_id` | Author user ID |
| `username` | `includes.users[0].username` | Author @handle |
| `created_at` | `data.created_at` | ISO timestamp |
| `lang` | `data.lang` | Language code |
| `retweet_count` | `data.public_metrics.retweet_count` | Retweets |
| `reply_count` | `data.public_metrics.reply_count` | Replies |
| `like_count` | `data.public_metrics.like_count` | Likes |
| `matching_rules` | `matching_rules[*].tag` | Which symbol rule matched |

**Request params**:
```
tweet_fields=created_at,author_id,lang,public_metrics
expansions=author_id
user_fields=username
```

### Stream Rules (`POST /2/tweets/search/stream/rules`)

Rules define what tweets appear in the filtered stream. We build rules dynamically per burst:

**Short tickers** (< 5 chars, e.g. "PARA"):
```
$PARA context:166.* -is:retweet lang:en
```
Uses `context:166.*` (X's ML-classified "Stocks" domain) to avoid false matches (e.g., Spanish word "para").

**Short tickers with company name**:
```
($PARA OR "Paramount Global") -is:retweet lang:en
```

**Long tickers** (>= 5 chars, e.g. "GOOGL"):
```
(GOOGL OR $GOOGL) -is:retweet lang:en
```

**Limits**: Max 3 symbols per burst.

### Usage Tracking (`GET /2/usage/tweets`)

Polls API usage every 5 minutes (configurable). Reports 7-day rolling tweet consumption vs. tier cap.

---

## Burst Lifecycle

### Triggering Conditions

A burst starts when ALL conditions are met:
1. `X_STREAM_ENABLED=true`
2. `X_STREAM_MODE=burst`
3. Triage confidence >= 0.75 (configurable)
4. News is fresh (not stale/backfilled)
5. Market hours check passes (if `X_STREAM_MARKET_HOURS_ONLY=true`)
6. Daily burst limit not exceeded
7. Daily post limit not exceeded

### Execution Flow

```
1. Build rules for symbols (max 3)
2. Add rules via API
3. Open stream connection (WebSocket-like SSE)
4. Buffer tweets (fail-closed — nothing published yet)
5. After N tweets (default 5), run quality check
   ├── PASS: Flush buffer, continue streaming + publishing
   ├── FAIL + revised rules: Delete rules, retry with new rules (up to 3x)
   └── FAIL + no revisions: Discard buffer, end burst
6. Stream continues until TTL expires (default 5 min)
7. Delete rules (cleanup)
8. Publish burst_end event
```

### Quality Gate (`stream_quality.py`)

LLM-based noise filter that evaluates the first N tweets:

```python
check_stream_quality(
    posts=first_5_tweets,
    headline="News headline",
    symbols=["PARA"],
    current_rules=[...],
    model="gemini-3-flash",
)
```

Returns `QualityVerdict`:
- `relevant`: bool — are tweets about the right topic?
- `confidence`: 0.0–1.0
- `reasoning`: explanation
- `revised_rule_values`: optional improved rules for retry

**Design principle**: Fail-closed. If the quality check fails or errors, no tweets are published.

### Caching

Tweets that pass quality are stored in an in-memory cache:
```python
self._cache: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=500))
```
- Per-symbol deques (max 500 per symbol)
- Thread-safe via `threading.Lock`
- Also persisted to JSONL files: `data/x/stream/{YYYY-MM-DD}.jsonl`

### Tool Access

Agents access cached tweets via the `x_stream_cache` tool:
```python
x_stream_cache(symbol="PARA", limit=20)
# Returns: {"symbol": "PARA", "posts": [...], "count": 15, "source": "x_stream_cache"}
```
This tool is **free** — it reads from the in-memory cache, no API call.

---

## Pricing

### Tier Comparison

| Tier | Price | Read Volume | Filtered Stream | Full-Archive Search |
|------|------:|-------------|:-:|:-:|
| **Free** | $0/mo | ~0 (write-only) | No | No |
| **Basic** | $200/mo | 15,000 tweets/mo | **No** | No |
| **Pro** | $5,000/mo | ~1M tweets/mo | **Yes** | Yes |
| **Enterprise** | $42,000+/mo | 50M+ tweets/mo | Yes | Yes |

Source: [X API Pricing](https://twitterapi.io/blog/twitter-api-pricing-2025)

### Important: Filtered Stream Access

**Filtered stream requires the Pro tier ($5,000/mo).** The Basic tier ($200/mo) only provides search/recent and user timeline endpoints.

However, there are reports of Basic tier users having filtered stream access in certain configurations. Our implementation works with whatever tier provides the endpoint.

### Our Usage Budget

With conservative guards:
- Max 1,000 tweets/day
- Max 10 bursts/day
- 5-minute burst TTL
- Quality gate discards irrelevant tweets

Monthly consumption is well within Basic tier limits when using search endpoints, but filtered stream is technically a Pro feature.

### Pay-Per-Use Pilot

X announced a pay-per-use pricing pilot (credit-based) in December 2025. Currently in closed beta with a $500 starting voucher. This could be a more cost-effective option for our burst-only usage pattern.

---

## Configuration

```bash
# .env
X_BEARER_TOKEN=<your-bearer-token>
X_API_BASE_URL=https://api.x.com

# Burst settings
X_STREAM_ENABLED=false                     # Master enable/disable
X_STREAM_MODE=burst                        # Only "burst" or "off"
X_MAX_POSTS_PER_DAY=1000                   # Daily tweet cap
X_MAX_BURSTS_PER_DAY=10                    # Daily burst cap
X_BURST_TTL_MINUTES=5                      # Burst duration
X_USAGE_POLL_INTERVAL_S=300                # Usage check frequency

# Quality gate
X_STREAM_QUALITY_CHECK_ENABLED=true
X_STREAM_QUALITY_CHECK_AFTER=5             # Evaluate after N tweets
X_STREAM_QUALITY_CHECK_MODEL=gemini-3-flash
X_STREAM_QUALITY_MAX_RETRIES=3
X_STREAM_MARKET_HOURS_ONLY=true
X_MIN_TRIAGE_CONFIDENCE_FOR_BURST=0.75
```

---

## API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/2/tweets/search/stream` | GET | Filtered stream (real-time tweets) |
| `/2/tweets/search/stream/rules` | GET | List current rules |
| `/2/tweets/search/stream/rules` | POST | Add/delete rules |
| `/2/usage/tweets` | GET | Usage tracking (7-90 day window) |

### Connection Details

- **Auth**: OAuth 2.0 Bearer token (app-only, read-only)
- **Base URL**: `https://api.x.com` (configurable)
- **Timeouts**: Connect 3.05s, Read 90s
- **Reconnection**: Exponential backoff 1s → 2s → 4s → ... → 60s max

---

## Event Bus Integration

All stream activity is published via EventBus:

| Event | Trigger |
|-------|---------|
| `x_stream_configured` | Service initialized |
| `x_burst_start` | Burst started with rules |
| `x_burst_quality` | Quality check verdict |
| `x_post` | Individual tweet published |
| `x_burst_end` | Burst finished |
| `x_usage` | Usage snapshot from API |
| `x_rules_purged` | Stale rules cleaned on shutdown |

---

## What Else X API Offers (Not Currently Used)

| Endpoint | Description | Tier |
|----------|-------------|------|
| `search/recent` | Search tweets (last 7 days) | Basic |
| `search/all` | Full-archive search | Pro |
| `users/:id/tweets` | User timeline | Basic |
| `tweets/:id` | Single tweet lookup | Basic |
| `tweets/counts/recent` | Tweet volume counts | Basic |
| `users/:id/followers` | Follower list | Basic |
| `lists/:id/tweets` | List timeline | Basic |
| Spaces | Twitter Spaces data | Pro |

### Potential Additions

- **`search/recent`** — could supplement filtered stream on Basic tier (poll every 30s)
- **Tweet counts** — volume analysis for mentions of a ticker over time
- **User lookup** — verify credibility of tweet authors (verified, follower count)
- **Full-archive search** — historical sentiment analysis (Pro tier only)

---

## Key Files

| File | Purpose |
|------|---------|
| [`trader/xapi/client.py`](trader/xapi/client.py) | Low-level HTTP client for X API |
| [`trader/xapi/stream.py`](trader/xapi/stream.py) | Filtered stream consumer with backoff |
| [`trader/xapi/rules.py`](trader/xapi/rules.py) | Stream rule CRUD operations |
| [`trader/xapi/usage.py`](trader/xapi/usage.py) | API usage polling |
| [`trader/online/x_stream_service.py`](trader/online/x_stream_service.py) | `XStreamService` — burst orchestration + caching |
| [`trader/online/stream_quality.py`](trader/online/stream_quality.py) | LLM-based noise filtering |
| [`trader/online/tool_core.py`](trader/online/tool_core.py) | `x_stream_cache` tool |
| [`tests/test_x_stream_quality.py`](tests/test_x_stream_quality.py) | Quality gate tests with real noise data |

---

## References

- [X API v2 Documentation](https://developer.x.com/en/docs/x-api)
- [Filtered Stream](https://developer.x.com/en/docs/x-api/tweets/filtered-stream/introduction)
- [X API Pricing](https://twitterapi.io/blog/twitter-api-pricing-2025)
- [Pay-Per-Use Pilot](https://devcommunity.x.com/t/announcing-the-x-api-pay-per-use-pricing-pilot/250253)
