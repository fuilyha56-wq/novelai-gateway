"""并发门控多账号分档行为测试。"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from proxy.account_pool import account_pool
from proxy.queue import ConcurrencyGate, account_generation_gate


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
    account_pool._async_locks.clear()


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


def test_same_account_generation_is_serialized():
    _configure_accounts(2)

    async def scenario():
        inside = 0
        peak = 0

        async def worker():
            nonlocal inside, peak
            request = type("Request", (), {"state": type("State", (), {"gateway_account_id": "acc-1"})()})()
            async with account_generation_gate(request):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.02)
                inside -= 1

        await asyncio.gather(worker(), worker())
        return peak

    assert asyncio.run(scenario()) == 1


def test_different_accounts_can_generate_in_parallel():
    _configure_accounts(2)

    async def scenario():
        inside = 0
        peak = 0

        async def worker(account_id):
            nonlocal inside, peak
            request = type("Request", (), {"state": type("State", (), {"gateway_account_id": account_id})()})()
            async with account_generation_gate(request):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.02)
                inside -= 1

        await asyncio.gather(worker("acc-1"), worker("acc-2"))
        return peak

    assert asyncio.run(scenario()) == 2


def _request(need_v5=False, need_anlas=False):
    return SimpleNamespace(
        state=SimpleNamespace(gateway_need_v5=need_v5, gateway_need_anlas=need_anlas),
        url=SimpleNamespace(path="/v1/images/generations"),
    )


def test_weighted_pool_uses_every_idle_account_before_queueing():
    account_pool.configure(
        '[{"id":"a","key":"a","weight":100},'
        '{"id":"b","key":"b","weight":1},'
        '{"id":"c","key":"c","weight":1}]'
    )
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=0, cooldown_max=0)

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        selected = []

        async def worker():
            request = _request()
            async with gate.for_request(request):
                selected.append(request.state.gateway_account_id)
                if len(selected) == 3:
                    ready.set()
                await release.wait()

        tasks = [asyncio.create_task(worker()) for _ in range(3)]
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.5)
            assert set(selected) == {"a", "b", "c"}
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert not any(a["busy"] for a in account_pool.public())

    asyncio.run(scenario())


def test_v5_waiter_does_not_block_an_idle_legacy_account():
    account_pool.configure(
        '[{"id":"v5","key":"v5","allow_v5":true},'
        '{"id":"legacy","key":"legacy","allow_v5":false}]'
    )
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=0, cooldown_max=0)

    async def scenario():
        async with gate.for_request(_request(need_v5=True)):
            async def v5_waiter():
                async with gate.for_request(_request(need_v5=True)):
                    pass
            waiter = asyncio.create_task(v5_waiter())
            await asyncio.sleep(0)
            legacy = _request()
            async with gate.for_request(legacy):
                assert legacy.state.gateway_account_id == "legacy"
                assert not waiter.done()
        await asyncio.wait_for(waiter, timeout=0.5)
        assert gate._active == 0 and gate._waiting == 0

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leak_account_reservations():
    _configure_accounts(1)
    gate = ConcurrencyGate(1, timeout=1, cooldown_min=0, cooldown_max=0)

    async def scenario():
        async with gate.for_request(_request()):
            async def waiter():
                async with gate.for_request(_request()):
                    pass
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with gate.for_request(_request()):
            assert gate._active == 1
        assert gate._active == 0 and gate._waiting == 0
        assert not account_pool.public()[0]["busy"]

    asyncio.run(scenario())


def test_duplicate_tokens_share_one_slot_and_one_generation_lock():
    account_pool.configure('[{"id":"a","key":"same"},{"id":"b","key":"same"}]')
    assert account_pool.enabled_count() == 1
    assert account_pool.get_async_lock("a") is account_pool.get_async_lock("b")
    lease = account_pool.reserve()
    assert lease is not None
    assert account_pool.reserve() is None
    account_pool.release(lease[1])
    assert account_pool.reserve() is not None
    account_pool.release(lease[1])
