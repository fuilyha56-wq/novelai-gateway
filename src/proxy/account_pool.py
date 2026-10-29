"""NovelAI 多账号池：平滑加权轮询、失败冷却与运行时统计。"""

from __future__ import annotations

import json
import os
import time
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any


def normalize_secret(value: str) -> str:
    """清理凭据首尾空白和外层引号。"""
    return value.strip().strip("'\"")


def mask_secret(value: str) -> str:
    """只显示凭据前缀和末尾少量字符。"""
    secret = normalize_secret(value)
    if not secret:
        return ""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:8]}...{secret[-4:]}"


def parse_accounts(value: str) -> list[dict[str, Any]]:
    """解析 JSON 账号数组，兼容字符串数组和单字符串。"""
    raw = value.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [item for item in raw.replace(";", ",").replace("\n", ",").split(",") if item.strip()]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    accounts: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if isinstance(item, str):
            secret = normalize_secret(item)
            account = {"id": f"account-{index}", "name": f"账号 {index}", "key": secret}
        elif isinstance(item, dict):
            secret = item.get("key", item.get("token", ""))
            if not isinstance(secret, str):
                continue
            account = dict(item)
            account["key"] = normalize_secret(secret)
        else:
            continue
        if not account["key"]:
            continue
        account.setdefault("id", f"account-{index}")
        account.setdefault("name", f"账号 {index}")
        try:
            account["weight"] = max(1, min(100, int(account.get("weight", 1))))
        except (TypeError, ValueError):
            account["weight"] = 1
        account["enabled"] = bool(account.get("enabled", True))
        # 账号级计费开关：allow_v5 控制是否承接 V5 请求，
        # allow_anlas 控制是否允许该账号消耗 Anlas（超出 Opus 免费额度）。
        account["allow_v5"] = bool(account.get("allow_v5", True))
        account["allow_anlas"] = bool(account.get("allow_anlas", True))
        account["v5_auto_disabled"] = bool(account.get("v5_auto_disabled", False))
        accounts.append(account)
    return accounts


def serialize_accounts(accounts: list[dict[str, Any]]) -> str:
    """序列化账号配置，只保留持久化字段。"""
    result = []
    for index, item in enumerate(accounts, start=1):
        key = normalize_secret(str(item.get("key", item.get("token", ""))))
        if not key:
            continue
        result.append({
            "id": str(item.get("id", f"account-{index}")),
            "name": str(item.get("name", f"账号 {index}")),
            "key": key,
            "weight": max(1, min(100, int(item.get("weight", 1)))),
            "enabled": bool(item.get("enabled", True)),
            "allow_v5": bool(item.get("allow_v5", True)),
            "allow_anlas": bool(item.get("allow_anlas", True)),
            "v5_auto_disabled": bool(item.get("v5_auto_disabled", False)),
        })
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def load_accounts_file(path: Path) -> list[dict[str, Any]]:
    """从 JSON 文件读取并规范化多账号配置。"""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"账号配置必须是 JSON 数组: {path}")
    return parse_accounts(json.dumps(payload, ensure_ascii=False))


def save_accounts_file(path: Path, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """原子写入规范化后的多账号配置并返回保存内容。"""
    normalized = parse_accounts(serialize_accounts(accounts))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary_path, 0o640)
    except OSError:
        pass
    temporary_path.replace(path)
    return normalized


@dataclass
class AccountState:
    """账号持久化信息和运行时状态。"""

    id: str
    name: str
    key: str
    weight: int = 1
    enabled: bool = True
    allow_v5: bool = True
    allow_anlas: bool = True
    v5_auto_disabled: bool = False
    current_weight: float = 0
    status: str = "ready"
    last_error: str = ""
    cooldown_until: float = 0
    success_count: int = 0
    failure_count: int = 0
    last_used_at: float = 0

    def public(self) -> dict[str, Any]:
        """返回脱敏账号状态。"""
        return {
            "id": self.id,
            "name": self.name,
            "masked_key": mask_secret(self.key),
            "weight": self.weight,
            "enabled": self.enabled,
            "allow_v5": self.allow_v5,
            "allow_anlas": self.allow_anlas,
            "v5_auto_disabled": self.v5_auto_disabled,
            "status": self.status if self.enabled else "disabled",
            "last_error": self.last_error,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_used_at": self.last_used_at,
        }


class AccountPool:
    """线程安全的平滑加权账号池。"""

    @staticmethod
    def _unique_credentials(accounts: list[AccountState]) -> list[AccountState]:
        """重复配置的同一 NovelAI Token 只代表一个上游账号。"""
        seen: set[str] = set()
        unique: list[AccountState] = []
        for account in accounts:
            if account.key in seen:
                continue
            seen.add(account.key)
            unique.append(account)
        return unique

    def __init__(self, persistence_path: Path | None = Path("config/accounts.json")) -> None:
        self._lock = Lock()
        self._accounts: dict[str, AccountState] = {}
        self._source = ""
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._reserved: set[str] = set()
        self._persistence_path = persistence_path

    def configure(self, value: str) -> None:
        """从环境配置同步账号，保留已有运行时统计。"""
        accounts = parse_accounts(value)
        with self._lock:
            old = self._accounts
            self._accounts = {}
            for item in accounts:
                account_id = str(item["id"])
                previous = old.get(account_id)
                self._accounts[account_id] = AccountState(
                    id=account_id,
                    name=str(item["name"]),
                    key=str(item["key"]),
                    weight=int(item["weight"]),
                    enabled=bool(item["enabled"]),
                    allow_v5=bool(item.get("allow_v5", True)),
                    allow_anlas=bool(item.get("allow_anlas", True)),
                    v5_auto_disabled=bool(item.get("v5_auto_disabled", False)),
                    current_weight=previous.current_weight if previous else 0,
                    status=previous.status if previous else "ready",
                    last_error=previous.last_error if previous else "",
                    cooldown_until=previous.cooldown_until if previous else 0,
                    success_count=previous.success_count if previous else 0,
                    failure_count=previous.failure_count if previous else 0,
                    last_used_at=previous.last_used_at if previous else 0,
                )
                self._async_locks.setdefault(str(item["key"]), asyncio.Lock())
            self._source = value

    def choose(self, need_v5: bool = False, need_anlas: bool = False) -> tuple[str, str]:
        selected = self._choose(need_v5, need_anlas)
        assert selected is not None
        return selected

    def reserve(
        self, need_v5: bool = False, need_anlas: bool = False, account_id: str | None = None,
    ) -> tuple[str, str] | None:
        """原子预留一个符合能力的空闲账号；全部繁忙时交回队列等待。"""
        return self._choose(need_v5, need_anlas, reserve=True, account_id=account_id)

    def release(self, key: str) -> None:
        """按凭据释放槽位，兼容请求执行期间修改或删除账号配置。"""
        with self._lock:
            self._reserved.discard(key)

    def _choose(
        self, need_v5: bool, need_anlas: bool, *, reserve: bool = False,
        account_id: str | None = None,
    ) -> tuple[str, str] | None:
        """平滑加权轮询选择账号，返回账号 ID 和凭据。

        need_v5 / need_anlas：请求对账号计费能力的要求。启用对应开关的账号
        才进入候选池；没有符合条件的账号时直接失败，绝不回退到其他账号。
        """
        now = time.monotonic()
        with self._lock:
            enabled_accounts = [
                account for account in self._accounts.values()
                if account.enabled and account.key
            ]
            if not enabled_accounts:
                raise RuntimeError("没有可用的 NovelAI 账号")
            if need_v5 or need_anlas:
                enabled_accounts = [
                    account for account in enabled_accounts
                    if (not need_v5 or account.allow_v5) and (not need_anlas or account.allow_anlas)
                ]
                if not enabled_accounts:
                    required = []
                    if need_v5:
                        required.append("V5")
                    if need_anlas:
                        required.append("Anlas")
                    raise RuntimeError(f"没有满足权限的 NovelAI 账号：{' + '.join(required)}")
            enabled_accounts = self._unique_credentials(enabled_accounts)
            if account_id is not None:
                bound = self._accounts.get(account_id)
                enabled_accounts = [a for a in enabled_accounts if bound and a.key == bound.key]
                if not enabled_accounts:
                    raise RuntimeError("当前请求绑定的 NovelAI 账号已不可用")
            if reserve:
                enabled_accounts = [
                    a for a in enabled_accounts
                    if a.key not in self._reserved
                    and not (self._async_locks.get(a.key) and self._async_locks[a.key].locked())
                ]
                if not enabled_accounts:
                    return None
            candidates = [
                account for account in enabled_accounts
                if account.cooldown_until <= now
            ]
            if not candidates:
                candidates = [min(enabled_accounts, key=lambda account: account.cooldown_until)]
            total = sum(account.weight for account in candidates)
            selected: AccountState | None = None
            for account in candidates:
                account.current_weight += account.weight
                if selected is None or account.current_weight > selected.current_weight:
                    selected = account
            assert selected is not None
            selected.current_weight -= total
            selected.status = "active"
            selected.last_used_at = time.time()
            if reserve:
                self._reserved.add(selected.key)
            return selected.id, selected.key

    def success(self, account_id: str) -> None:
        """记录一次成功请求并清除错误状态。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if account:
                account.success_count += 1
                account.status = "v5_quota_exhausted" if account.v5_auto_disabled else "ready"
                if not account.v5_auto_disabled:
                    account.last_error = ""
                account.cooldown_until = 0

    def failure(self, account_id: str, error: str, cooldown_seconds: float = 30) -> None:
        """记录失败并进入短暂冷却。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if account:
                account.failure_count += 1
                account.status = "v5_quota_exhausted" if account.v5_auto_disabled else "cooldown"
                account.last_error = error[:300]
                account.cooldown_until = time.monotonic() + cooldown_seconds

    def reset(self, account_id: str) -> bool:
        """清除账号错误和冷却状态。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return False
            account.status = "v5_quota_exhausted" if account.v5_auto_disabled else "ready"
            if not account.v5_auto_disabled:
                account.last_error = ""
            account.cooldown_until = 0
            return True

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        """启用或禁用账号。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return False
            account.enabled = enabled
            account.status = "ready" if enabled else "disabled"
            return True

    def set_flag(self, account_id: str, flag: str, allowed: bool) -> bool:
        """更新账号计费开关（allow_v5 / allow_anlas），运行时立即生效。"""
        if flag not in ("allow_v5", "allow_anlas"):
            return False
        with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return False
            setattr(account, flag, bool(allowed))
            if flag == "allow_v5":
                account.v5_auto_disabled = False
                if account.enabled:
                    account.status = "ready"
            return True

    def disable_v5_for_quota(self, account_id: str, reason: str) -> bool:
        """因上游报告 V5 免费额度耗尽而关闭账号的 V5 能力。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account:
                return False
            account.allow_v5 = False
            account.v5_auto_disabled = True
            account.status = "v5_quota_exhausted" if account.enabled else "disabled"
            account.last_error = reason[:300]
            snapshot = [
                {
                    "id": item.id,
                    "name": item.name,
                    "key": item.key,
                    "weight": item.weight,
                    "enabled": item.enabled,
                    "allow_v5": item.allow_v5,
                    "allow_anlas": item.allow_anlas,
                    "v5_auto_disabled": item.v5_auto_disabled,
                }
                for item in self._accounts.values()
            ]
            persistence_path = self._persistence_path
        if persistence_path is not None:
            save_accounts_file(persistence_path, snapshot)
        return True

    def restore_v5_after_quota(self, account_id: str) -> bool:
        """上游报告额度恢复时，仅恢复本功能自动关闭的 V5 能力。"""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account or not account.v5_auto_disabled:
                return False
            account.allow_v5 = True
            account.v5_auto_disabled = False
            account.status = "ready" if account.enabled else "disabled"
            account.last_error = ""
            snapshot = [
                {
                    "id": item.id,
                    "name": item.name,
                    "key": item.key,
                    "weight": item.weight,
                    "enabled": item.enabled,
                    "allow_v5": item.allow_v5,
                    "allow_anlas": item.allow_anlas,
                    "v5_auto_disabled": item.v5_auto_disabled,
                }
                for item in self._accounts.values()
            ]
            persistence_path = self._persistence_path
        if persistence_path is not None:
            save_accounts_file(persistence_path, snapshot)
        return True

    def enabled_count(self) -> int:
        """当前启用且配置了凭据的账号数量，用于并发容量计算。"""
        with self._lock:
            return len(self._unique_credentials([
                account for account in self._accounts.values()
                if account.enabled and account.key
            ]))

    def eligible_count(self, need_v5: bool = False, need_anlas: bool = False) -> int:
        """返回满足当前请求能力要求的启用账号数。"""
        with self._lock:
            eligible = [
                account
                for account in self._accounts.values()
                if account.enabled
                and account.key
                and (not need_v5 or account.allow_v5)
                and (not need_anlas or account.allow_anlas)
            ]
            return len(self._unique_credentials(eligible))

    def capacity_summary(self) -> dict[str, int]:
        """返回账号池按请求能力划分的并发槽数。"""
        enabled = self.enabled_count()
        anlas = self.eligible_count(need_anlas=True)
        return {
            "total": enabled,
            # V4.5 has no separate account toggle; every enabled credential can serve it.
            "v45": enabled,
            "v5": self.eligible_count(need_v5=True),
            "anlas": anlas,
            "v45_and_anlas": anlas,
            "v5_and_anlas": self.eligible_count(need_v5=True, need_anlas=True),
        }

    def check_request(self, need_v5: bool = False, need_anlas: bool = False) -> tuple[bool, str]:
        """检查是否存在满足计费能力要求的启用账号。

        Returns:
            (是否通过, 未通过时的提示消息)
        """
        if not (need_v5 or need_anlas):
            return True, ""
        with self._lock:
            enabled = [a for a in self._accounts.values() if a.enabled and a.key]
        if not enabled:
            return True, ""
        capable = [
            a for a in enabled
            if (not need_v5 or a.allow_v5) and (not need_anlas or a.allow_anlas)
        ]
        if capable:
            return True, ""
        missing = []
        if need_v5 and need_anlas:
            missing.append("允许 V5 且允许消耗 Anlas")
        elif need_v5:
            missing.append("允许 V5 模型")
        else:
            missing.append("允许消耗 Anlas")
        return False, (
            f"当前没有任何启用的账号{' ' + '/'.join(missing) if missing else ''}；"
            f"请在管理控制台为至少一个账号开启对应能力后再试"
        )

    def public(self) -> list[dict[str, Any]]:
        """返回脱敏账号列表。"""
        with self._lock:
            credential_groups: dict[str, str] = {}
            for account in self._accounts.values():
                credential_groups.setdefault(account.key, account.id)
            return [
                {
                    **account.public(),
                    "credential_group": credential_groups[account.key],
                    "classification": self._classification(account),
                    "busy": account.key in self._reserved or bool(
                        self._async_locks.get(account.key) and self._async_locks[account.key].locked()
                    ),
                }
                for account in self._accounts.values()
            ]

    @staticmethod
    def _classification(account: AccountState) -> str:
        """根据账号当前能力归类；自动耗尽的 V5 能力不会误算为可用。"""
        if not account.enabled or not account.key:
            return "disabled"
        if account.allow_v5 and account.allow_anlas:
            return "v5-and-anlas"
        if account.allow_v5:
            return "v5-free"
        if account.allow_anlas:
            return "anlas-only"
        return "legacy-only"

    def get_secret(self, account_id: str) -> str:
        """读取内部账号凭据，仅供测试端点使用。"""
        with self._lock:
            account = self._accounts.get(account_id)
            return account.key if account else ""

    def get_permitted_secret(
        self, account_id: str, need_v5: bool = False, need_anlas: bool = False,
        expected_token: str | None = None,
    ) -> str:
        """Recheck a bound account before using its cached credential."""
        with self._lock:
            account = self._accounts.get(account_id)
            if not account or not account.enabled or not account.key:
                raise RuntimeError("当前请求绑定的 NovelAI 账号已不可用")
            if need_v5 and not account.allow_v5:
                raise RuntimeError("当前请求绑定的 NovelAI 账号不允许 V5 请求")
            if need_anlas and not account.allow_anlas:
                raise RuntimeError("当前请求绑定的 NovelAI 账号不允许消耗 Anlas，已阻止上游请求")
            if expected_token and expected_token != account.key:
                raise RuntimeError("当前请求绑定的 NovelAI 账号凭据已变更，请重试")
            return account.key

    def get_async_lock(self, account_id: str) -> asyncio.Lock | None:
        """返回账号级异步锁；未知账号返回 None。"""
        with self._lock:
            if account_id not in self._accounts:
                return None
            return self._async_locks.setdefault(self._accounts[account_id].key, asyncio.Lock())


account_pool = AccountPool()
