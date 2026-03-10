"""Entry point: python -m tick_collector"""

import asyncio
import logging
import sys

from dotenv import load_dotenv

from .collector import TickCollector
from .config import CollectorConfig

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

    if not config.symbols:
        print("ERROR: No symbols configured. Set TICK_SYMBOLS or edit symbols.txt")
        sys.exit(1)

    print(f"Tick Collector")
    print(f"  Symbols:  {len(config.symbols)} ({', '.join(config.symbols[:5])}{'...' if len(config.symbols) > 5 else ''})")
    print(f"  DSN:      {config.dsn}")
    print(f"  Flush:    every {config.flush_interval_sec}s, batch size {config.flush_batch_size}")
    print()

    collector = TickCollector(config)
    asyncio.run(collector.run())


main()
