"""Desktop-client session import tests (no real client required)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from loomy2api.pool import AccountPool
from tests.support import make_config


class TestClientImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.client_root = self.dir / "client"
        (self.client_root / "abc123").mkdir(parents=True)
        self.session_file = (self.client_root / "abc123" / "userData"
                             / "auth-session.json")
        self.cfg = make_config(self.dir, "http://127.0.0.1:1/api/v1",
                              client_root=str(self.client_root),
                              sessions_from_client=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _client_session(self, session="client-session", userid="u1"):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps({
            "session": session, "userid": userid, "phone": "13800006207",
            "updatedAt": int(time.time() * 1000),
        }), encoding="utf-8")

    def _accounts(self, accounts):
        (self.dir / "accounts.json").write_text(
            json.dumps({"accounts": accounts}), encoding="utf-8")

    def test_import_creates_pool_entry(self):
        self._client_session()
        self._accounts([])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.accounts], ["desktop-6207"])
        imported = pool.accounts[0]
        self.assertEqual(imported.source, "client")
        self.assertFalse(imported.persist)
        self.assertTrue(imported.session_valid)
        self.assertAlmostEqual(imported.days_left, 14, delta=0.1)

    def test_imported_account_is_not_persisted(self):
        self._client_session()
        self._accounts([])
        pool = AccountPool(self.cfg)
        pool.save(force=True)
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"], [])

    def test_same_user_is_not_duplicated(self):
        """Password account + its client session = one pool entry, not two."""
        self._client_session(userid="same-user")
        self._accounts([{
            "name": "main", "loginid": "13800006207", "password": "pw",
            "session": "own-session", "userid": "same-user",
            "expireAt": int(time.time()) + 14 * 86400,
        }])
        pool = AccountPool(self.cfg)
        self.assertEqual([a.name for a in pool.accounts], ["main"])

    def test_different_user_is_added(self):
        self._client_session(userid="other-user")
        self._accounts([{
            "name": "main", "session": "own-session", "userid": "same-user",
            "expireAt": int(time.time()) + 14 * 86400,
        }])
        pool = AccountPool(self.cfg)
        self.assertEqual(sorted(a.name for a in pool.accounts),
                         ["desktop-6207", "main"])

    def test_import_can_be_disabled(self):
        self._client_session()
        self._accounts([])
        self.cfg["sessions_from_client"] = False
        pool = AccountPool(self.cfg)
        self.assertEqual(pool.accounts, [])

    def test_broken_client_file_is_ignored(self):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text("{not json", encoding="utf-8")
        self._accounts([])
        pool = AccountPool(self.cfg)
        self.assertEqual(pool.accounts, [])


if __name__ == "__main__":
    unittest.main()
