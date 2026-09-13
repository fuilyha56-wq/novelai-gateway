"""
全局并发门控。

容量随启用账号数动态扩展：并发重负载请求经账号池加权轮询分摊到
多个启用账号上（每个启用账号约一个并发槽位），只有全部账号繁忙时
才排队。单账号时保留请求间随机冷却，继续规避 NovelAI 的 429 限制；
多账号时相邻请求本就落在不同账号上，全局冷却关闭以换取并发吞吐。
"""

import asyncio
import logging
import random
import time

from fastapi import HTTPException

from .account_pool import account_pool
from .config import settings

logger = logging.getLogger("gateway")


def _account_slots() -> int:
    """启用账号可承载的并发槽数；账号池不可用时按 0 处理。"""
    try:
        return account_pool.enabled_count()
    except Exception:
        return 0


class ConcurrencyGate:
    """
    异步并发门控，用法：

        async with gate:
            await do_heavy_work()
    """

    def __init__(
        self,
        max_concurrent: int,
        timeout: int,
        cooldown_min: float,
        cooldown_max: float,
        max_waiters: int | None = None,
    ):
        self._base_max = max(1, max_concurrent)
        self._timeout = timeout
        self._cooldown_min = cooldown_min
        self._cooldown_max = cooldown_max
        if max_waiters is None:
            max_waiters = getattr(settings, "queue_max_waiters", 8)
        self._max_waiters = max(0, max_waiters)
        self._cond = asyncio.Condition()
        # admitted = active + waiting，用于满载时快速返回 429。
        self._active = 0
        self._waiting = 0
        # 冷却从上一个请求完成时开始计时。这样当前响应不会在 __aexit__
        # 里额外等待，只有下一个重请求会按保护间隔启动。
        self._next_ready_time = 0.0

    def _capacity(self) -> int:
        """并发容量 = max(MAX_CONCURRENT, 启用账号数)，至少为 1。"""
        return max(1, self._base_max, _account_slots())

    def _cooldown_enabled(self) -> bool:
        """仅单账号时启用全局请求间冷却；多账号靠轮询分摊上游压力。"""
        return _account_slots() <= 1

    async def __aenter__(self):
        async with self._cond:
            if self._active + self._waiting >= self._capacity() + self._max_waiters:
                logger.warning("⚠️ 重负载请求排队已满，快速返回 429")
                raise HTTPException(
                    status_code=429,
                    detail="重负载请求正在排队，请稍后重试",
                )
            self._waiting += 1
            # Python 3.11 的 Condition.wait_for 不支持 timeout，手动按
            # deadline 轮询；wait() 超时/被取消时都会重新持锁再抛出。
            deadline = asyncio.get_running_loop().time() + self._timeout
            acquired = False
            try:
                while True:
                    if self._active < self._capacity():
                        acquired = True
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
            except BaseException:
                # Condition.wait 被取消时会在持锁状态下抛出，这里仍持有锁。
                self._waiting -= 1
                raise
            self._waiting -= 1
            if not acquired:
                logger.warning("⚠️ 排队超时 (%s 等待中)", self._waiting)
                raise asyncio.TimeoutError("排队超时")
            self._active += 1

        # 不在锁内 sleep，避免阻塞其他请求读取队列状态。
        if self._cooldown_enabled():
            delay = max(0.0, self._next_ready_time - time.monotonic())
            if delay:
                logger.debug("⏱️ 新请求等待冷却 %.1fs", delay)
                try:
                    await asyncio.sleep(delay)
                except BaseException:
                    await self.__aexit__(None, None, None)
                    raise

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 先释放当前请求，再记录下一次可启动时间。当前请求不会被冷却拖住，
        # 但下一个请求仍会遵守原来的随机保护间隔（仅单账号模式）。
        async with self._cond:
            if self._cooldown_enabled() and self._cooldown_max > 0:
                gap = random.uniform(self._cooldown_min, self._cooldown_max)
                self._next_ready_time = max(
                    self._next_ready_time,
                    time.monotonic() + max(0.0, gap),
                )
            self._active -= 1
            self._cond.notify_all()
        return False


gate = ConcurrencyGate(
    max_concurrent=settings.max_concurrent,
    timeout=settings.queue_timeout,
    cooldown_min=settings.cooldown_min,
    cooldown_max=settings.cooldown_max,
)
