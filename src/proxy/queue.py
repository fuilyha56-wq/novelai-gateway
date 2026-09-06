"""
全局并发门控。

通过异步信号量实现重负载请求的排队机制，
确保同一时间只有有限数量的请求通过，避免触发 NovelAI 的 429 限制。
每次释放锁后会执行随机冷却，进一步降低触发频率限制的风险。
"""

import asyncio
import logging
import random
import time

from fastapi import HTTPException

from .config import settings

logger = logging.getLogger("gateway")


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
        max_waiters: int = 8,
    ):
        self._max_concurrent = max(1, max_concurrent)
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._lock = asyncio.Lock()
        self._timeout = timeout
        self._cooldown_min = cooldown_min
        self._cooldown_max = cooldown_max
        self._max_waiters = max(0, max_waiters)
        self._waiting = 0
        # admitted = active + waiting，用于让 QUEUE_MAX_WAITERS=0 仍允许
        # 一个空闲槽位立即执行，同时拒绝后续排队请求。
        self._admitted = 0
        # 冷却从上一个请求完成时开始计时。这样当前响应不会在 __aexit__
        # 里额外等待，只有下一个重请求会按保护间隔启动。
        self._next_ready_time = 0.0

    async def __aenter__(self):
        async with self._lock:
            if self._admitted >= self._max_concurrent + self._max_waiters:
                logger.warning("⚠️ 重负载请求排队已满，快速返回 429")
                raise HTTPException(
                    status_code=429,
                    detail="重负载请求正在排队，请稍后重试",
                )
            self._admitted += 1
            self._waiting += 1
            waiting = self._waiting

        if waiting:
            logger.debug("⏳ 排队中... (当前等待: %s)", waiting)

        acquired = False
        removed_from_waiters = False
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
            acquired = True

            async with self._lock:
                self._waiting -= 1
                removed_from_waiters = True
                remaining = self._waiting
                delay = max(0.0, self._next_ready_time - time.monotonic())
        except asyncio.TimeoutError:
            if not removed_from_waiters:
                async with self._lock:
                    self._waiting -= 1
                    self._admitted -= 1
                    remaining = self._waiting
            if acquired:
                self._sem.release()
            logger.warning("⚠️ 排队超时 (剩余等待: %s)", remaining)
            raise
        except BaseException:
            if not removed_from_waiters:
                async with self._lock:
                    self._waiting -= 1
                    self._admitted -= 1
            if acquired:
                self._sem.release()
            raise

        # 不在锁内 sleep，避免阻塞其他请求读取队列状态。
        if delay:
            logger.debug("⏱️ 新请求等待冷却 %.1fs", delay)
            try:
                await asyncio.sleep(delay)
            except BaseException:
                async with self._lock:
                    self._admitted -= 1
                    self._sem.release()
                raise

        logger.debug("🔒 获取锁，开始处理 (剩余等待: %s)", remaining)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 先释放当前请求，再记录下一次可启动时间。当前请求不会被冷却拖住，
        # 但下一个请求仍会遵守原来的随机保护间隔。
        async with self._lock:
            delay = random.uniform(self._cooldown_min, self._cooldown_max)
            self._next_ready_time = max(
                self._next_ready_time,
                time.monotonic() + max(0.0, delay),
            )
            self._admitted -= 1
            self._sem.release()
            waiting = self._waiting

        logger.debug("🔓 释放锁，下一请求冷却 %.1fs (当前等待: %s)", delay, waiting)
        return False


gate = ConcurrencyGate(
    max_concurrent=settings.max_concurrent,
    timeout=settings.queue_timeout,
    cooldown_min=settings.cooldown_min,
    cooldown_max=settings.cooldown_max,
    max_waiters=settings.queue_max_waiters,
)
