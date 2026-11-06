"""
全局并发门控。

每个启用的上游凭据可同时运行一个请求并容纳一个等待请求。
运行位和等待位都按请求所需的 V5 / Anlas 能力匹配。
"""

import asyncio
import logging
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .account_pool import account_pool
from .config import settings

logger = logging.getLogger("gateway")


def uses_managed_account_pool() -> bool:
    # 已配置但全部停用的账号池仍由账号池管理，不能切换到直连容量。
    return account_pool.has_accounts() or bool(settings.shared_api_keys.strip())


@dataclass(eq=False, slots=True)
class _Waiter:
    request: Request | None
    managed: bool
    requirements: tuple[bool, bool, str | None]
    queued_at: float
    deadline: float
    acquired: bool = False
    error: HTTPException | None = None


def _assign_waiting_slot(
    waiter: _Waiter, eligible: dict[_Waiter, tuple[str, ...]], owners: dict[str, _Waiter],
) -> bool:
    """Find a free compatible waiting slot, moving flexible requests if needed."""
    search = deque([waiter])
    previous: dict[_Waiter, tuple[_Waiter, str] | None] = {waiter: None}
    while search:
        current = search.popleft()
        for credential in eligible[current]:
            owner = owners.get(credential)
            if owner is None:
                while True:
                    owners[credential] = current
                    parent = previous[current]
                    if parent is None:
                        return True
                    current, credential = parent
            elif owner not in previous:
                previous[owner] = (current, credential)
                search.append(owner)
    return False


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
    ):
        self._base_max = max(1, max_concurrent)
        self._timeout = timeout
        self._cooldown_min = cooldown_min
        self._cooldown_max = cooldown_max
        self._cond = asyncio.Condition()
        self._active = 0
        self._waiting: list[_Waiter] = []
        self._next_capacity_check = 0.0
        # 冷却从上一个请求完成时开始计时。这样当前响应不会在 __aexit__
        # 里额外等待，只有下一个重请求会按保护间隔启动。
        self._next_ready_time = 0.0

    def _cooldown_enabled(self) -> bool:
        """仅单账号时启用全局请求间冷却；多账号靠轮询分摊上游压力。"""
        return account_pool.enabled_count() <= 1

    def for_request(self, request):
        """排队后预留符合 V5/Anlas 能力的空闲账号，再开始执行。"""
        return _RequestConcurrencyGate(self, request)

    async def __aenter__(self):
        await self._enter()
        return self

    def _try_acquire(self, waiter: _Waiter) -> None:
        request = waiter.request
        if waiter.managed:
            try:
                lease = account_pool.reserve(*waiter.requirements)
            except RuntimeError as exc:
                waiter.error = HTTPException(status_code=409, detail=str(exc))
                return
            if lease is None:
                return
            assert request is not None
            request.state.gateway_account_id, request.state.gateway_auth_token = lease
            request.state.gateway_account_lease = lease
        elif self._active >= self._base_max:
            return

        self._active += 1
        waiter.acquired = True
        if request is not None:
            started = time.monotonic()
            request.state.gateway_queue_seconds = started - waiter.queued_at
            request.state.gateway_slot_started = started

    def _limit_waiters(self, waiting: list[_Waiter]) -> list[_Waiter]:
        admitted = []
        direct = 0
        owners: dict[str, _Waiter] = {}
        eligible: dict[_Waiter, tuple[str, ...]] = {}
        credentials: dict[tuple[bool, bool, str | None], tuple[str, ...]] = {}
        for waiter in waiting:
            if waiter.managed:
                requirements = waiter.requirements
                if requirements not in credentials:
                    credentials[requirements] = account_pool.eligible_credentials(*requirements)
                eligible[waiter] = credentials[requirements]
                if not eligible[waiter]:
                    waiter.error = HTTPException(status_code=409, detail="没有满足权限的 NovelAI 账号")
                    continue
                available = _assign_waiting_slot(waiter, eligible, owners)
            else:
                available = direct < self._base_max
                if available:
                    direct += 1
            if available:
                admitted.append(waiter)
                continue
            logger.warning("⚠️ 请求所需的账号等待位已满，快速返回 429")
            waiter.error = HTTPException(status_code=429, detail="重负载请求正在排队，请稍后重试")
        return admitted

    def _dispatch(self) -> None:
        previous_count = len(self._waiting)
        if not previous_count:
            return
        waiting = []
        now = time.monotonic()
        self._next_capacity_check = now + 1.0
        for waiter in self._waiting:
            if now >= waiter.deadline:
                logger.warning("⚠️ 排队超时 (%s 等待中)", previous_count)
                waiter.error = HTTPException(status_code=504, detail="排队超时")
                continue
            self._try_acquire(waiter)
            if not waiter.acquired and waiter.error is None:
                waiting.append(waiter)
        self._waiting = self._limit_waiters(waiting)
        if len(self._waiting) != previous_count:
            self._cond.notify_all()

    async def _enter(self, request=None):
        queued_at = time.monotonic()
        state = getattr(request, "state", None)
        waiter = _Waiter(
            request=request,
            managed=request is not None and uses_managed_account_pool(),
            requirements=(
                bool(getattr(state, "gateway_need_v5", False)),
                bool(getattr(state, "gateway_need_anlas", False)),
                getattr(state, "gateway_account_id", None),
            ),
            queued_at=queued_at,
            deadline=queued_at + self._timeout,
        )
        try:
            async with self._cond:
                # 先分配已在等待的兼容请求，新增请求仍可使用不冲突的空闲账号。
                self._dispatch()
                self._try_acquire(waiter)
                if not waiter.acquired and waiter.error is None:
                    self._waiting = self._limit_waiters([*self._waiting, waiter])
                try:
                    while not waiter.acquired and waiter.error is None:
                        remaining = min(waiter.deadline, self._next_capacity_check) - time.monotonic()
                        if remaining <= 0:
                            self._dispatch()
                            continue
                        try:
                            # 账号设置和旧账号锁释放未必经过此门控，定时刷新容量。
                            async with asyncio.timeout(remaining):
                                await self._cond.wait()
                        except TimeoutError:
                            pass
                finally:
                    if waiter in self._waiting:
                        self._waiting.remove(waiter)
                if waiter.error is not None:
                    raise waiter.error

            if self._cooldown_enabled():
                delay = max(0.0, self._next_ready_time - time.monotonic())
                if delay:
                    logger.debug("⏱️ 新请求等待冷却 %.1fs", delay)
                    await asyncio.sleep(delay)
        except BaseException:
            if waiter.acquired:
                await self._exit(request)
            raise

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._exit()
        return False

    async def _exit(self, request=None):
        # 先释放当前请求，再记录下一次可启动时间。当前请求不会被冷却拖住，
        # 但下一个请求仍会遵守原来的随机保护间隔（仅单账号模式）。
        async with self._cond:
            if request is not None:
                lease = getattr(request.state, "gateway_account_lease", None)
                if lease is not None:
                    account_pool.release(lease[1])
                    request.state.gateway_account_lease = None
                started = getattr(request.state, "gateway_slot_started", time.monotonic())
                logger.info(
                    "Gateway request path=%s account=%s need_v5=%s need_anlas=%s queue=%.3fs active=%.3fs",
                    getattr(getattr(request, "url", None), "path", ""),
                    getattr(request.state, "gateway_account_id", "direct"),
                    bool(getattr(request.state, "gateway_need_v5", False)),
                    bool(getattr(request.state, "gateway_need_anlas", False)),
                    getattr(request.state, "gateway_queue_seconds", 0.0),
                    time.monotonic() - started,
                )
            if self._cooldown_enabled() and self._cooldown_max > 0:
                gap = random.uniform(self._cooldown_min, self._cooldown_max)
                self._next_ready_time = max(
                    self._next_ready_time,
                    time.monotonic() + max(0.0, gap),
                )
            self._active -= 1
            self._dispatch()


class _RequestConcurrencyGate:
    def __init__(self, gate: ConcurrencyGate, request):
        self._gate = gate
        self._request = request

    async def __aenter__(self):
        await self._gate._enter(self._request)
        return self._gate

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._gate._exit(self._request)
        return False


gate = ConcurrencyGate(
    max_concurrent=settings.max_concurrent,
    timeout=settings.queue_timeout,
    cooldown_min=settings.cooldown_min,
    cooldown_max=settings.cooldown_max,
)


class AccountGenerationGate:
    """按请求已选中的 NovelAI 账号串行化上游生成。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


async def acquire_account_gate(request):
    """等待当前请求绑定账号的上游锁。"""
    account_id = getattr(request.state, "gateway_account_id", None)
    if not account_id:
        return None
    lock = account_pool.get_async_lock(account_id)
    if lock is None:
        return None
    await lock.acquire()
    return lock


@asynccontextmanager
async def account_generation_gate(request):
    """在当前请求绑定的账号上串行执行一次上游生成。"""
    lock = await acquire_account_gate(request)
    try:
        yield
    finally:
        if lock is not None and lock.locked():
            lock.release()
