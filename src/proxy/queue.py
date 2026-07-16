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

from .config import settings

logger = logging.getLogger("gateway")


class ConcurrencyGate:
    """
    异步并发门控，用法：

        async with gate:
            await do_heavy_work()
    """

    def __init__(self, max_concurrent: int, timeout: int,
                 cooldown_min: float, cooldown_max: float):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._timeout = timeout
        self._cooldown_min = cooldown_min
        self._cooldown_max = cooldown_max
        self._waiting = 0
        self._last_release_time = 0.0

    async def __aenter__(self):
        async with self._lock:
            self._waiting += 1
            waiting = self._waiting

        logger.info(f"⏳ 排队中... (当前等待: {waiting})")

        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._waiting -= 1
                remaining = self._waiting
            logger.warning(f"⚠️ 排队超时 (剩余等待: {remaining})")
            raise

        async with self._lock:
            self._waiting -= 1
            remaining = self._waiting

        logger.info(f"🔒 获取锁，开始处理 (剩余等待: {remaining})")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 冷却
        if self._cooldown_max > 0:
            delay = random.uniform(self._cooldown_min, self._cooldown_max)
            logger.info(f"❄️ 冷却 {delay:.1f}s")
            await asyncio.sleep(delay)

        # 原子性地检查时间间隔并释放
        async with self._lock:
            if self._last_release_time > 0:
                elapsed = time.time() - self._last_release_time
                if elapsed < self._cooldown_min:
                    extra = self._cooldown_min - elapsed
                    logger.info(f"⏱️ 额外保护等待 {extra:.1f}s")
                    await asyncio.sleep(extra)

            self._last_release_time = time.time()
            self._sem.release()
            waiting = self._waiting

        logger.info(f"🔓 释放锁 (当前等待: {waiting})")
        return False


gate = ConcurrencyGate(
    max_concurrent=settings.max_concurrent,
    timeout=settings.queue_timeout,
    cooldown_min=settings.cooldown_min,
    cooldown_max=settings.cooldown_max,
)
