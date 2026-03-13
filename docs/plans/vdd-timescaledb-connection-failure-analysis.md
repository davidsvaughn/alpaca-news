# VDD TimescaleDB Connection Failure Analysis

Date: 2026-03-13

## Summary

The trader app terminal output points to a failure in the tick-based VDD path, specifically `asyncpg` connections being closed while queries are still running.

The most likely root cause is that the VDD helper uses one module-global `asyncpg` pool across multiple threads and event loops. When one caller decides the pool is unhealthy and closes it, another caller can still be using a connection from that same pool, producing:

```text
asyncpg.exceptions.ConnectionDoesNotExistError: connection was closed in the middle of operation
```

## Symptoms Seen In Logs

- Repeated `VDD: pool connections dead, reconnecting...`
- Immediate reconnect success after each warning
- Repeated `Future exception was never retrieved`
- Repeated `ConnectionDoesNotExistError('connection was closed in the middle of operation')`
- Live monitor warning:
  `VDD tick check failed for AAPL, falling back to bar-based`

These lines indicate the app is not fully down, but the tick-based exit signal path is unstable and repeatedly failing over to bar-based logic.

## Code Paths Involved

### Shared VDD pool

The VDD module stores a single process-global pool:

- [tick_collector/vdd.py](/home/david/code/davidsvaughn/alpaca-news/tick_collector/vdd.py#L421)

It is created and reused by:

- [tick_collector/vdd.py](/home/david/code/davidsvaughn/alpaca-news/tick_collector/vdd.py#L424)

The reconnect logic closes and recreates the global pool after a failed health check:

- [tick_collector/vdd.py](/home/david/code/davidsvaughn/alpaca-news/tick_collector/vdd.py#L431)
- [tick_collector/vdd.py](/home/david/code/davidsvaughn/alpaca-news/tick_collector/vdd.py#L438)
- [tick_collector/vdd.py](/home/david/code/davidsvaughn/alpaca-news/tick_collector/vdd.py#L446)

### Live exit monitor thread

The live monitor runs in its own daemon thread:

- [trader/online/orchestrator.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/orchestrator.py#L1476)
- [trader/online/orchestrator.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/orchestrator.py#L1481)

Inside that thread, `LiveExitMonitor` creates or reuses a private event loop and calls the VDD helper synchronously via `run_until_complete(...)`:

- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L168)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L177)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L182)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L193)

### Live portfolio manager path

`LivePortfolioManager` also creates or reuses its own private event loop and calls the same VDD helper when fetching ranking features:

- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L886)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L895)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L900)
- [trader/online/live_monitor.py](/home/david/code/davidsvaughn/alpaca-news/trader/online/live_monitor.py#L903)

## Why This Looks Like A Pool Ownership Bug

The relevant shape of the system is:

1. VDD DB access uses a module-global `_pool`.
2. Multiple code paths call that pool from different threads and event loops.
3. A failed health check in one caller causes `get_pool()` to close the shared pool.
4. Another caller may still be mid-query on a connection from that same pool.
5. That in-flight query fails with `ConnectionDoesNotExistError`.

That sequence matches the observed logs:

1. A VDD query is in progress.
2. Another caller runs the health check, decides the pool is dead, and closes it.
3. The in-flight query reports `connection was closed in the middle of operation`.
4. The reconnect path creates a fresh pool.
5. The cycle repeats under load.

## Alternate Possibility

The TimescaleDB container may also have restarted or dropped connections around `11:13:25`, which could trigger the first failure. Even if that happened, the current shared-pool reconnect logic is still unsafe because it lets one caller close a pool another caller may still be using.

This should be verified with container logs:

```bash
docker compose logs timescaledb
```

## Notes On Other Log Lines

These lines do not appear to be the primary issue:

- `GET /api/... 200 OK`
  Normal FastAPI/Uvicorn access logs.
- `AlpacaBroker initialized ...`
  Likely unrelated broker initialization noise.
- `Skipping stale holding update ... status changed concurrently`
  This looks like an expected race guard, not the DB failure root cause.
- `VDD tick check failed ..., falling back to bar-based`
  This is a downstream effect of the DB/pool failure, not the original cause.

## Most Likely Root Cause

One shared `asyncpg.Pool` is being accessed and force-recycled across multiple threads/event loops in the trader process.

## Recommended Fix

Replace the module-global pool with a loop-local or thread-local pool, so each event loop owns its own `asyncpg` pool. Reconnect logic should only replace the pool associated with the current loop, not a process-global singleton.

Secondary improvements:

- Catch and await failing tasks so `Future exception was never retrieved` stops appearing.
- Consider a narrower reconnect strategy that retries the current query once after recreating the local pool.
- Log loop/thread identity during pool creation and reconnect events for easier diagnosis.

## Practical Impact

Current impact:

- Tick-based VDD exit checks are unreliable.
- The live monitor falls back to bar-based logic more often than intended.
- Terminal noise is high because each failure triggers both reconnect and uncaught-future logging.

Likely non-impact:

- The web API itself is still serving requests.
- Core live trading logic continues running, but with degraded signal quality whenever tick-based VDD is expected.

## Implementation Notes (2026-03-13)

Best way to proceed:

### Use loop-local pool ownership

The safe ownership model is loop-local, not merely thread-local.

`asyncpg` pools are bound to the event loop that created them. In this codebase, the VDD callers create and cache private loops on their instances, so a single thread can still end up creating more than one loop over time. A thread-local pool would still allow reuse across different loops in the same thread, which is the wrong abstraction boundary.

The pool registry should therefore be keyed by `asyncio.get_running_loop()`.

### Locks are not the fix

An `asyncio.Lock` only serializes work inside one loop. A plain threading lock only serializes access to the registry. Neither solves the original failure mode by itself, which is one owner closing a pool another owner is actively using.

Locks are acceptable around registry bookkeeping, but not as the primary fix.

### Keep the health check after ownership is fixed

The `SELECT 1` health check in `get_pool()` is reasonable once the pool is loop-local. At that point, closing and recreating a dead pool only affects the current loop's pool rather than a process-global singleton.

### `Future exception was never retrieved` should be reduced, not assumed eliminated

The cross-owner `pool.close()` behavior is a strong explanation for the uncaught-future noise in the terminal dump. Moving to loop-local ownership should reduce that noise substantially. It may not eliminate every case, because genuine database disconnects can still happen.

### `close_pool()` should close the current loop's pool

The existing `close_pool()` implementation assumes one module-global pool. After the refactor, it should only close the pool associated with the current running loop. A separate helper can close all registered pools if test or shutdown code ever needs that behavior.
