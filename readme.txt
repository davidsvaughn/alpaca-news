start 
python alpaca/news_websocket.py

# Kill existing
pkill -f "trader.main"

# launch
uv run python -m trader.main

# Relaunch
BACKFILL_ON_START=false nohup uv run python -m trader.main > /tmp/alpaca-dashboard.log 2>&1 &

------------------------------------------------------

jq -r '{id, signal_strength} | [.id, .signal_strength] | @tsv' ./labels/*.jsonl | sort -k1 -n > signal.tsv

------------------------------------------------------

uv venv --python /usr/bin/python3.12
source .venv/bin/activate
uv sync

---------------------------------------------------------------------------------------------------
CLAUDE CODE

MEMORY.md:
/home/david/.claude/projects/-home-david-code-davidsvaughn-alpaca-news/memory/MEMORY.md


subl .claude/settings.json
subl ~/.claude/CLAUDE.md

 # install context7 mcp globally
claude mcp add --transport stdio --scope user context7 -- npx -y @upstash/context7-mcp --api-key API_KEY

# install playwright-cli skill
https://github.com/microsoft/playwright-cli

SHOW COSTS

[ https://claude.ai/share/0b3b1977-bbec-412d-a43b-b81935fa82e8 ]

# View daily costs
npx ccusage@latest daily

# View monthly aggregated costs
npx ccusage@latest monthly

# Filter by date range
npx ccusage@latest daily --since 2025-02-08 --until 2025-02-11

# Show per-model cost breakdown
npx ccusage@latest daily --breakdown


The tool is lightweight and doesn't require installation - just run it with npx. 
It analyzes your local usage data (stored in ~/.claude/projects/) to give you 
comprehensive cost breakdowns by day, month, or session.

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


-----------------
Grok
https://console.x.ai/team/f21957a0-6031-4cdb-9d81-f4a4cb416058/usage

X
https://console.x.com/accounts/2020768736576253952/usage

Gemini
https://console.cloud.google.com/billing/01CED9-5E8FC2-E10728/payment?project=opportune-geode-473113-s9

OpenAI
https://platform.openai.com/settings/organization/usage
