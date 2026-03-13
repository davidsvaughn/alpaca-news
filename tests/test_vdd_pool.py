import asyncio

from tick_collector import vdd


class _FakeConnection:
    async def fetchval(self, _query: str) -> int:
        return 1


class _AcquireContext:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        if self._pool.closed:
            raise RuntimeError("pool is closed")
        return _FakeConnection()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self)

    async def close(self) -> None:
        self.closed = True


def _run_in_loop(loop: asyncio.AbstractEventLoop, coro):
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)


def test_get_pool_is_loop_local(monkeypatch):
    created_pools: list[_FakePool] = []

    async def fake_create_pool(*args, **kwargs) -> _FakePool:
        pool = _FakePool(f"pool-{len(created_pools) + 1}")
        created_pools.append(pool)
        return pool

    monkeypatch.setattr(vdd.asyncpg, "create_pool", fake_create_pool)

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        pool1 = _run_in_loop(loop1, vdd.get_pool())
        pool2 = _run_in_loop(loop2, vdd.get_pool())

        assert pool1 is not None
        assert pool2 is not None
        assert pool1 is not pool2
        assert len(created_pools) == 2

        _run_in_loop(loop1, vdd.close_pool())
        _run_in_loop(loop2, vdd.close_pool())
    finally:
        loop1.close()
        loop2.close()
        asyncio.run(vdd.close_all_pools())


def test_close_pool_only_closes_current_loop_pool(monkeypatch):
    created_pools: list[_FakePool] = []

    async def fake_create_pool(*args, **kwargs) -> _FakePool:
        pool = _FakePool(f"pool-{len(created_pools) + 1}")
        created_pools.append(pool)
        return pool

    monkeypatch.setattr(vdd.asyncpg, "create_pool", fake_create_pool)

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        pool1 = _run_in_loop(loop1, vdd.get_pool())
        pool2 = _run_in_loop(loop2, vdd.get_pool())

        _run_in_loop(loop1, vdd.close_pool())

        assert pool1.closed is True
        assert pool2.closed is False

        _run_in_loop(loop2, vdd.close_pool())
    finally:
        loop1.close()
        loop2.close()
        asyncio.run(vdd.close_all_pools())
