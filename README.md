# fin-text
tools for processing financial text (i.e. news)

# setup
```
git clone https://github.com/davidsvaughn/alpaca-news.git
cd alpaca-news
virtualenv -p python3.10 .venv && source .venv/bin/activate
pip install -U pip alpaca-py python-dotenv
```

# alpaca
- sign up for free tier Alpaca trading account [here](https://alpaca.markets/)
- copy `.env.example` to `.env` and fill in your Alpaca API key and secret
- run `python alpaca/news_websocket.py` to start saving alpaca newsfeed articles to json files