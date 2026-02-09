# fin-text
tools for processing financial text (i.e. news)

# setup
```
git clone https://github.com/davidsvaughn/alpaca-news.git
cd alpaca-news

# create a virtualenv + install dependencies from pyproject.toml
uv venv --python /usr/bin/python3.12
source .venv/bin/activate
uv sync
```

Optional dependency groups:

```bash
# deps for classify/ workflows
uv sync --group classify

# deps for ft/ fine-tuning workflows
uv sync --group ft
```

Run scripts inside the env:

```bash
uv run python alpaca/news_websocket.py
```

Add a dependency (example):

```bash
uv add schwabdev
```

# alpaca
- sign up for free tier Alpaca trading account [here](https://alpaca.markets/)
- copy `.env.example` to `.env` and fill in your Alpaca API key and secret
- run `python alpaca/news_websocket.py` to start saving alpaca newsfeed articles to json files