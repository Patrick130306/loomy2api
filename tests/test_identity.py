"""Identity (per-account device fingerprint) tests."""

from __future__ import annotations

import unittest

from loomy2api import constants as C
from loomy2api.account import Account, AccountClient, new_identity
from tests.support import make_config


class TestNewIdentity(unittest.TestCase):
    def test_per_account_mode_is_distinct(self):
        first, second = new_identity("per_account"), new_identity("per_account")
        self.assertNotEqual(first["devid"], second["devid"])
        self.assertNotEqual(first["campus_device_id"], second["campus_device_id"])
        self.assertTrue(first["devid"].startswith("web-"))
        self.assertTrue(first["campus_device_id"].startswith(C.CAMPUS_DEVICE_ID_PREFIX))

    def test_client_mode_mirrors_the_client(self):
        ident = new_identity("client")
        self.assertEqual(ident["devid"], "web")
        self.assertEqual(ident["ua"], C.CLIENT_UA)
        self.assertEqual(ident["campus_device_id"], "")

    def test_identity_has_no_traceid(self):
        """traceid must stay per-request, exactly like the real client."""
        self.assertNotIn("traceid", new_identity())
        self.assertNotIn("traceid", new_identity("client"))


class TestRequestEnvelope(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.tmp.name), "http://127.0.0.1:1/api/v1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_carries_identity(self):
        client = AccountClient(self.cfg)
        ident = new_identity("per_account")
        base = client._base(ident)
        self.assertEqual(base["devid"], ident["devid"])
        self.assertEqual(base["ua"], C.CLIENT_UA)
        self.assertEqual(base["modelid"], "Web")
        self.assertEqual(base["appid"], "GM3LOOMY")

    def test_traceid_is_regenerated_per_call(self):
        client = AccountClient(self.cfg)
        ident = new_identity("per_account")
        self.assertNotEqual(client._base(ident)["traceid"],
                            client._base(ident)["traceid"])

    def test_client_mode_config_is_honoured(self):
        self.cfg["identity_mode"] = "client"
        client = AccountClient(self.cfg)
        self.assertEqual(client._base()["devid"], "web")


class TestIdentityBinding(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.tmp.name), "http://127.0.0.1:1/api/v1")
        self.client = AccountClient(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_identity_is_stable(self):
        acc = Account(name="a")
        first = self.client.ensure_identity(acc)
        second = self.client.ensure_identity(acc)
        self.assertEqual(first["devid"], second["devid"])
        self.assertIs(first, second)

    def test_rebind_identity_changes_devid(self):
        acc = Account(name="a")
        before = self.client.ensure_identity(acc)["devid"]
        after = self.client.rebind_identity(acc)["devid"]
        self.assertNotEqual(before, after)

    def test_identity_survives_serialisation(self):
        acc = Account(name="a", loginid="13800000000", password="pw")
        self.client.ensure_identity(acc)
        restored = Account.from_dict(acc.to_dict())
        self.assertEqual(restored.identity["devid"], acc.identity["devid"])
        self.assertEqual(restored.identity_view()["devid"], acc.identity["devid"])

    def test_identity_view_masks_long_campus_id(self):
        acc = Account(name="a")
        self.client.ensure_identity(acc)
        view = acc.identity_view()
        self.assertTrue(view["bound"])
        self.assertLessEqual(len(view["campus_device_id"]), 33)
        self.assertTrue(view["campus_device_id"].endswith("…"))

    def test_empty_identity_view(self):
        view = Account(name="a").identity_view()
        self.assertFalse(view["bound"])
        self.assertEqual(view["devid"], "")


if __name__ == "__main__":
    unittest.main()
