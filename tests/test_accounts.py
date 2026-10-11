"""多账号轮询、解析和脱敏测试。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.proxy.account_pool import (
    AccountPool,
    load_accounts_file,
    mask_secret,
    parse_accounts,
    save_accounts_file,
    serialize_accounts,
)


class AccountPoolTests(unittest.TestCase):
    """验证账号池核心行为。"""

    def test_legacy_string_array_is_compatible(self) -> None:
        accounts = parse_accounts('["key-a", "key-b"]')
        self.assertEqual([item["key"] for item in accounts], ["key-a", "key-b"])
        self.assertEqual([item["weight"] for item in accounts], [1, 1])

    def test_mask_secret_never_returns_full_secret(self) -> None:
        secret = "pst-1234567890-secret"
        masked = mask_secret(secret)
        self.assertNotEqual(masked, secret)
        self.assertTrue(masked.startswith("pst-1234"))
        self.assertTrue(masked.endswith("cret"))

    def test_weighted_selection_skips_disabled_account(self) -> None:
        pool = AccountPool()
        pool.configure(
            '[{"id":"heavy","name":"Heavy","key":"key-heavy","weight":3},'
            '{"id":"off","name":"Off","key":"key-off","weight":100,"enabled":false}]'
        )
        self.assertEqual([pool.choose()[0] for _ in range(8)], ["heavy"] * 8)

    def test_smooth_weighted_selection_distribution(self) -> None:
        pool = AccountPool()
        pool.configure(
            '[{"id":"a","key":"key-a","weight":3},'
            '{"id":"b","key":"key-b","weight":1}]'
        )
        selected = [pool.choose()[0] for _ in range(8)]
        self.assertEqual(selected.count("a"), 6)
        self.assertEqual(selected.count("b"), 2)

    def test_failure_cooldown_and_reset(self) -> None:
        pool = AccountPool()
        pool.configure('[{"id":"a","key":"key-a"},{"id":"b","key":"key-b"}]')
        pool.failure("a", "bad token", cooldown_seconds=60)
        self.assertEqual(pool.choose()[0], "b")
        self.assertTrue(pool.reset("a"))
        self.assertEqual(pool.public()[0]["status"], "ready")

    def test_all_cooling_accounts_allow_recovery_probe(self) -> None:
        pool = AccountPool()
        pool.configure('[{"id":"a","key":"key-a"},{"id":"b","key":"key-b"}]')
        pool.failure("a", "network down", cooldown_seconds=30)
        pool.failure("b", "network down", cooldown_seconds=60)

        self.assertEqual(pool.choose()[0], "a")

    def test_serialize_preserves_secret_and_management_fields(self) -> None:
        encoded = serialize_accounts([{"id": "a", "name": "主账号", "key": "key-a", "weight": 2, "enabled": False}])
        self.assertEqual(parse_accounts(encoded)[0]["enabled"], False)
        self.assertEqual(parse_accounts(encoded)[0]["weight"], 2)

    def test_accounts_file_round_trip_preserves_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            accounts = [{"id": "a", "name": "主账号", "key": "key-a", "weight": 3, "enabled": True}]

            saved = save_accounts_file(path, accounts)

            self.assertEqual(load_accounts_file(path), saved)
            self.assertEqual(saved[0]["weight"], 3)
            self.assertEqual(saved[0]["key"], "key-a")

    def test_missing_accounts_file_returns_empty_list(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"

            self.assertEqual(load_accounts_file(path), [])

    def test_choose_filters_by_capability_without_fallback(self) -> None:
        pool = AccountPool()
        pool.configure(
            '[{"id":"free","name":"免费","key":"key-free","allow_v5":false,"allow_anlas":false},'
            '{"id":"v5","name":"V5","key":"key-v5","allow_v5":true,"allow_anlas":false}]'
        )
        self.assertEqual(pool.choose(need_v5=True, need_anlas=False)[0], "v5")
        with self.assertRaises(RuntimeError):
            pool.choose(need_v5=True, need_anlas=True)

    def test_v5_quota_disable_is_persisted_and_can_be_manually_reenabled(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            pool = AccountPool(persistence_path=path)
            pool.configure('[{"id":"a","key":"key-a"}]')

            self.assertTrue(pool.disable_v5_for_quota("a", "V5 免费额度已耗尽"))

            saved = load_accounts_file(path)[0]
            self.assertFalse(saved["allow_v5"])
            self.assertTrue(saved["v5_auto_disabled"])
            self.assertFalse(pool.public()[0]["allow_v5"])
            self.assertEqual(pool.public()[0]["status"], "v5_quota_exhausted")

            self.assertTrue(pool.set_flag("a", "allow_v5", True))
            self.assertFalse(pool.public()[0]["v5_auto_disabled"])

    def test_auto_disabled_account_leaves_v5_rotation_until_reenabled(self) -> None:
        pool = AccountPool(persistence_path=None)
        pool.configure('[{"id":"a","key":"key-a"},{"id":"b","key":"key-b"}]')
        pool.disable_v5_for_quota("a", "V5 免费额度已耗尽")

        self.assertEqual(pool.choose(need_v5=True)[0], "b")
        self.assertTrue(pool.restore_v5_after_quota("a"))
        self.assertTrue(pool.public()[0]["allow_v5"])


if __name__ == "__main__":
    unittest.main()
