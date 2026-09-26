"""Account-pool tests: strategies, cooldown, persistence, session renewal."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api.account import Account
from loomy2api.pool import AccountPool, PoolError
from tests.support import make_config


class TestPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def test_load_and_snapshot(self):
        self._write([
            {"name": "a", "loginid": "13800000001", "password": "p",
             "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.accounts], ["a", "b"])
        snap = pool.snapshot()
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["usable"], 2)
        self.assertNotIn('"password":', json.dumps(snap))   # secrets stay out

    def test_balance_strategy_prefers_richest(self):
        self._write([
            {"name": "poor", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "rich", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("poor").available = 10
        pool.get("rich").available = 900
        self.assertEqual(pool.acquire().name, "rich")

    def test_round_robin_strategy(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "round_robin"
        pool = AccountPool(self.cfg)
        self.assertEqual([pool.acquire().name for _ in range(4)], ["a", "b", "a", "b"])

    def test_lru_strategy(self):
        self._write([
            {"name": "old", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "new", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        self.cfg["strategy"] = "lru"
        pool = AccountPool(self.cfg)
        pool.get("old").last_used = 100
        pool.get("new").last_used = 200
        self.assertEqual(pool.acquire().name, "old")

    def test_cooldown_skips_account_and_recovers(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.report_failure(pool.get("a"), 401, "invalid session")
        self.assertEqual(pool.acquire().name, "b")
        self.assertEqual(pool.usable()[0].name, "b")
        # a 401 clears the session, so it needs credentials to come back
        self.assertFalse(pool.get("a").session_valid)

    def test_exhausted_account_is_skipped(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 14 * 86400},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        pool.get("a").available = 0
        self.assertEqual(pool.acquire().name, "b")

    def test_acquire_raises_when_pool_empty(self):
        self._write([])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.acquire()

    def test_disabled_account_ignored(self):
        self._write([
            {"name": "a", "session": "s1", "expireAt": int(time.time()) + 86400,
             "enabled": False},
            {"name": "b", "session": "s2", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.usable()], ["b"])

    def test_add_remove_persists(self):
        self._write([])
        pool = AccountPool(self.cfg)
        pool.add_account("x", loginid="13800000009", password="pw")
        pool.flush()
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"][0]["name"], "x")
        self.assertTrue(pool.remove_account("x"))
        self.assertFalse(pool.remove_account("x"))

    def test_duplicate_add_rejected(self):
        self._write([{"name": "x", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError):
            pool.add_account("x")

    def test_report_success_decrements_quota(self):
        self._write([{"name": "a", "session": "s", "expireAt": int(time.time()) + 9e4}])
        pool = AccountPool(self.cfg)
        acc = pool.get("a")
        acc.available = 100
        pool.report_success(acc, {"points_consumed": 3})
        self.assertEqual(acc.points_used, 3)
        self.assertEqual(acc.available, 97)
        self.assertEqual(acc.requests, 1)


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_expiring_session_triggers_relogin(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "stale", "expireAt": int(time.time()) + 3600},   # < 3 days
        ])
        pool = AccountPool(self.cfg)

        calls = []

        class FakeClient:
            def fill(self, account):
                calls.append(account.name)
                account.session = "fresh"
                account.expire_at = int(time.time()) + 14 * 86400
                return account

        pool.client = FakeClient()
        pool.acquire()
        self.assertEqual(calls, ["a"])
        self.assertEqual(pool.get("a").session, "fresh")

    def test_no_credentials_raises_actionable_error(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [{"name": "a"}])
        pool = AccountPool(self.cfg)
        with self.assertRaises(PoolError) as ctx:
            pool.acquire()
        self.assertIn("loginid/password", str(ctx.exception))

    def test_tick_refreshes_quota(self):
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session):
                return {"balance": 5, "daily_balance": 6, "available": 11}

        pool.gateway = FakeGateway()
        pool.tick()
        acc = pool.get("a")
        self.assertEqual((acc.balance, acc.daily_balance, acc.available), (5, 6, 11))

    def test_tick_clears_session_on_401(self):
        from loomy2api.upstream import UpstreamError
        from tests.support import write_accounts

        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s", "expireAt": int(time.time()) + 14 * 86400},
        ])
        pool = AccountPool(self.cfg)

        class FakeGateway:
            def quota(self, session):
                raise UpstreamError("nope", 401)

        pool.gateway = FakeGateway()
        pool.tick()
        self.assertFalse(pool.get("a").session_valid)


if __name__ == "__main__":
    unittest.main()
