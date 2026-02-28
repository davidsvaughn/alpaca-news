'''
Run this file to start a websocket connection to InsightSentry's news data stream.
It will save incoming news articles to the `output/insight_sentry` directory in JSON format.

uv run python websocket/insight_sentry_news.py

Make sure to set your InsightSentry API key in a `.env` file with the following content:
    INSIGHT_SENTRY_API_KEY=your_api_key

'''

import asyncio
import hashlib
import json
import pathlib
import os
import datetime as dt
from dotenv import load_dotenv
import websockets

load_dotenv()
INSIGHT_SENTRY_API_KEY = os.getenv("INSIGHT_SENTRY_API_KEY")

BASE_DIR = pathlib.Path("output/insight_sentry")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _iso_filename(unix_ts: int) -> str:
    """Convert unix timestamp to filesystem-safe ISO string."""
    return dt.datetime.fromtimestamp(unix_ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

def _article_id(title: str) -> str:
    """Short hash of the title as a unique-ish ID (like Alpaca's numeric article ID)."""
    return hashlib.sha256(title.encode()).hexdigest()[:8]

# -----------------------------------------------------------------------------
# websocket
# -----------------------------------------------------------------------------
async def connect_and_listen():
    uri = "wss://realtime.insightsentry.com/newsfeed"
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                await websocket.send(json.dumps({"api_key": INSIGHT_SENTRY_API_KEY}))
                print(f"Connected to {uri}")

                async for message in websocket:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    # skip heartbeat messages
                    if "title" not in data:
                        continue

                    # filename: {published_at_iso}_{title_hash}.json  (like Alpaca's {timestamp}_{id}.json)
                    ts = data.get("published_at", int(dt.datetime.now(dt.timezone.utc).timestamp()))
                    aid = _article_id(data["title"])
                    filename = f"{_iso_filename(ts)}_{aid}.json"
                    path = BASE_DIR / filename
                    with path.open("w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    print("saved", path.name)

        except websockets.exceptions.ConnectionClosed:
            print("Connection closed, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(2)

# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(connect_and_listen())
