start 
python alpaca/news_websocket.py

------------------------------------------------------

jq -r '{id, signal_strength} | [.id, .signal_strength] | @tsv' ./labels/*.jsonl | sort -k1 -n > signal.tsv

------------------------------------------------------

uv venv --python /usr/bin/python3.12
source .venv/bin/activate
uv sync