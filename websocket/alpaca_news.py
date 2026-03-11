'''
Run this file to start a websocket connection to Alpaca's news data stream.
It will save incoming news articles to `data/news/incoming/alpaca` by default.

uv run python -u websocket/alpaca_news.py

Make sure to set your Alpaca API keys in a `.env` file with the following content:
    ALPACA_API_KEY=your_api_key
    ALPACA_SECRET_KEY=your_secret_key
Optional:
    ALPACA_NEWS_DIR=data/news/incoming/alpaca

'''



import json, pathlib, html, os, datetime as dt
from dotenv import load_dotenv
from alpaca.data.live import NewsDataStream
from alpaca.data.models.news import News

# create .env file (if it doesn't exist) with following params:
#     ALPACA_API_KEY=your_api_key
#     ALPACA_SECRET_KEY=your_secret_key

load_dotenv()
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

BASE_DIR = pathlib.Path(
    os.getenv("ALPACA_NEWS_DIR")
    or os.getenv("ALPACA_OUTPUT_DIR")
    or "data/news/incoming/alpaca"
)
# create output directory if it doesn't exist
BASE_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _iso_filename(ts: dt.datetime) -> str:
    """Return something like 2025-07-04T14-15-40Z (safe for most filesystems)."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

def _article_path(article: News) -> pathlib.Path:
    return BASE_DIR / f"{_iso_filename(article.created_at)}_{article.id}.json"

def _article_to_dict(article: News) -> dict:
    """
    Convert the Pydantic model into pure-Python types that `json.dump` likes.
    Also unescape HTML entities in the text fields for readability.
    """
    try:
        data = article.model_dump()

        # datetime → ISO-strings
        data["created_at"] = article.created_at.isoformat()
        data["updated_at"] = article.updated_at.isoformat()

        # unescape headline / summary / content
        for fld in ("headline", "summary", "content"):
            if fld in data and isinstance(data[fld], str):
                data[fld] = html.unescape(data[fld])

        return data
    except Exception as e:
        print(f"Error converting article {article.id}: {e}")
        # return raw data if conversion fails
        return {
            "id": article.id,
            "created_at": article.created_at.isoformat(),
            "updated_at": article.updated_at.isoformat(),
            "headline": str(article.headline),
            "summary": str(article.summary),
            "content": str(article.content),
        }

# -----------------------------------------------------------------------------
# websocket callback
# -----------------------------------------------------------------------------
async def news_data_handler(article: News):
    try:
        path = _article_path(article)
        with path.open("w", encoding="utf-8") as f:
            json.dump(_article_to_dict(article), f, ensure_ascii=False, indent=2)
        print("saved", path.name)
    except Exception as e:
        print(f"Error saving article:\n{article}:\n\nERROR:\n{e}\n")
        # Optionally, you could log this error to a file or monitoring system

# -----------------------------------------------------------------------------
# run the stream
# -----------------------------------------------------------------------------
def main():
    stream = NewsDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    stream.subscribe_news(news_data_handler, "*")     # "*" = all stocks
    stream.run()

if __name__ == "__main__":
    main()
