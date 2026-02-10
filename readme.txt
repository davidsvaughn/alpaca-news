start 
python alpaca/news_websocket.py

------------------------------------------------------

jq -r '{id, signal_strength} | [.id, .signal_strength] | @tsv' ./labels/*.jsonl | sort -k1 -n > signal.tsv

------------------------------------------------------

uv venv --python /usr/bin/python3.12
source .venv/bin/activate
uv sync

---------------------------------

GROK x_search: https://docs.x.ai/developers/tools/x-search

X DEVELOPER CONSOLE: 
https://console.x.com/accounts/2020768736576253952/apps
https://docs.x.com/x-api/posts/search-recent-posts
https://docs.x.com/x-api/stream/stream-filtered-posts

App: 2020768736576253952blammo3030

---- App-Only Authentication ----
BearerToken: AAAAAAAAAAAAAAAAAAAAAAvn7QEAAAAAvXK205swehBxN8Tg1UYheHxzmKo%3Dl0TY5ExfbbODbsE1OC5gnGg0jerzAZdij6ud5xJJSibNVOegMo

---- OAuth 1.0 Keys ----
Consumer Key: 3VZxKFkmGLidJJq317BqOYQ3D
Consumer Key Secret: BpfiTewvlllrRUkOOX6VsxwK51zXjSNTPP86vn31lAQqe7jtwK

Access Token: 118192929-wIYrqyVHijTFWTrKmJDTQ6PnypF6QE4ICaBK8NrY
Access Token Secret: Mre4oFDwA6bIVM74RzogJGpTdvs3Y91M4CcZFYWX3rM5E