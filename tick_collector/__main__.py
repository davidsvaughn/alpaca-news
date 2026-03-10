"""Entry point: python -m tick_collector"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

from .collector import TickCollector
from .config import CollectorConfig
from .portfolio import PortfolioSync

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet down noisy libs
logging.getLogger("schwabdev").setLevel(logging.WARNING)
logging.getLogger("asyncpg").setLevel(logging.WARNING)


def main() -> None:
    config = CollectorConfig.from_env()

    # Preview portfolio symbols
    ps = PortfolioSync(config.trader_db_path, config.portfolio_cooloff_min)
    ps.sync()
    active = sorted(ps.active_symbols)

    print("Tick Collector")
    print(f"  Portfolio symbols: {len(active)}")
    print(f"  Trader DB:         {config.trader_db_path}")
    print(f"  DSN:               {config.dsn}")
    print(f"  Flush:             every {config.flush_interval_sec}s")
    print(f"  Portfolio sync:    every {config.portfolio_sync_interval_sec}s")
    print(f"  Cool-off:          {config.portfolio_cooloff_min} min")
    if active:
        preview = ', '.join(active[:10])
        print(f"  Symbols:           {preview}{'...' if len(active) > 10 else ''}")
    print()

    if not active:
        print("ERROR: No portfolio holdings found in trader.db")
        sys.exit(1)

    collector = TickCollector(config)
    asyncio.run(collector.run())


main()
