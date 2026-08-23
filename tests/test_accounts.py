"""多账号轮询、解析和脱敏测试。"""

import unittest

from src.proxy.account_pool import AccountPool, mask_secret, parse_accounts, serialize_accounts


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

    def test_serialize_preserves_secret_and_management_fields(self) -> None:
        encoded = serialize_accounts([{"id": "a", "name": "主账号", "key": "key-a", "weight": 2, "enabled": False}])
        self.assertEqual(parse_accounts(encoded)[0]["enabled"], False)
        self.assertEqual(parse_accounts(encoded)[0]["weight"], 2)


if __name__ == "__main__":
    unittest.main()
