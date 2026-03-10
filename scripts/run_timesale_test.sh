#!/usr/bin/env bash
# Run the TIMESALE_EQUITY test with env vars loaded.
# Usage:
#   ./scripts/run_timesale_test.sh                  # 5 min default
#   ./scripts/run_timesale_test.sh --duration 600   # 10 min
#   ./scripts/run_timesale_test.sh -v               # verbose (every trade)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a && source .env 2>/dev/null && set +a
exec uv run python scripts/test_timesale.py "$@"
