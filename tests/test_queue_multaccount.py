"""并发门控多账号分档行为测试。"""

import asyncio
import time

import pytest
from fastapi import HTTPException

from proxy.account_pool import account_pool
from proxy.queue import ConcurrencyGate


def _configure_accounts(count: int) -> None:
    account_pool.configure(
        "[" + ",".join(
            f'{{"id":"acc-{i}","name":"账号{i}","key":"pst-key-{i}"}}'
            for i in range(1, count + 1)
        ) + "]"
    )


@pytest.fixture(autouse=True)
def _reset_pool():
    yield
    account_pool.configure("[]")


def test_capacity_scales_with_enabled_accounts():
    _configure_accounts(3)
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=0.5, cooldown_max=1.0)
    assert gate._capacity() == 3

    account_pool.set_enabled("acc-2", False)
    assert gate._capacity() == 2

    _configure_accounts(0)
    assert gate._capacity() == 1  # 落回 MAX_CONCURRENT


def test_multi_account_no_global_cooldown():
    _configure_accounts(3)
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=5, cooldown_max=5)
    assert not gate._cooldown_enabled()

    _configure_accounts(1)
    assert gate._cooldown_enabled()


def test_concurrent_requests_fill_all_account_slots():
    _configure_accounts(3)
    gate = ConcurrencyGate(1, timeout=5, cooldown_min=5, cooldown_max=5)

    async def scenario():
        inside = 0
        peak = 0

        async def worker():
            nonlocal inside, peak
            async with gate:
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.05)
                inside -= 1

        await asyncio.gather(*(worker() for _ in range(3)))
        return peak

    peak = asyncio.run(scenario())
    assert peak == 3  # 三个账号三个并发槽位，冷却不阻塞


def test_overflow_beyond_accounts_queues_then_times_out():
    _configure_accounts(2)
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=5, cooldown_max=5)

    async def scenario():
        inside = 0
        all_in = asyncio.Event()

        async def holder():
            nonlocal inside
            async with gate:
                inside += 1
                if inside == 2:
                    all_in.set()
                await asyncio.sleep(1.5)

        async def waiter():
            await all_in.wait()
            async with gate:
                pass

        return await asyncio.gather(
            holder(), holder(), waiter(), return_exceptions=True
        )

    results = asyncio.run(scenario())
    assert results[0] is None and results[1] is None  # 两个账号槽位正常
    assert isinstance(results[2], asyncio.TimeoutError)  # 第三个排队超时


def test_waiter_overflow_fast_429():
    _configure_accounts(1)
    gate = ConcurrencyGate(1, timeout=10, cooldown_min=0, cooldown_max=0, max_waiters=0)

    async def scenario():
        started = asyncio.Event()

        async def holder():
            async with gate:
                started.set()
                await asyncio.sleep(0.1)

        async def overflow():
            await started.wait()
            async with gate:
                pass

        return await asyncio.gather(holder(), overflow(), return_exceptions=True)

    results = asyncio.run(scenario())
    assert results[0] is None
    assert isinstance(results[1], HTTPException)
    assert results[1].status_code == 429


def test_enabled_count_counts_only_enabled_with_keys():
    _configure_accounts(3)
    assert account_pool.enabled_count() == 3
    account_pool.set_enabled("acc-3", False)
    assert account_pool.enabled_count() == 2
    account_pool.set_enabled("acc-3", True)
    assert account_pool.enabled_count() == 3
