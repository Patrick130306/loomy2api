"""Web control panel tests — page, state, and every write endpoint."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from loomy2api.server import Gateway, Handler, Server
from tests.support import (FakeAccountClient, FakeUpstream, make_config,
                           write_accounts)


class PanelHarness:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = FakeUpstream()
        base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "loginid": "13800000001", "password": "pw",
             "session": "s1", "userid": "u1",
             "expireAt": int(time.time()) + 14 * 86400,
             "identity": {"devid": "web-aaaaaaaaaaaaaaaa", "ua": "UA",
                          "modelid": "Web", "version": "1.0.0",
                          "campus_device_id": "loomy-campus-fixed",
                          "created_at": 1700000000}},
        ])
        self.cfg = make_config(self.dir, base)
        self.gateway = Gateway(self.cfg)
        self.gateway.pool.client = FakeAccountClient()
        self.gateway.pool.bootstrap()
        self.gateway.refresh_models()
        handler = type("Bound", (Handler,), {"gateway": self.gateway})
        self.httpd = Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.stop()
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, response

    def get_json(self, path, headers=None):
        status, response = self.get(path, headers)
        return status, json.loads(response.read())

    def post(self, path, payload, headers=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=60)
        return response.status, json.loads(response.read())


class TestPanelPage(PanelHarness, unittest.TestCase):
    def test_page_is_served(self):
        status, response = self.get("/panel")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", body)
        self.assertIn("/api/panel/state", body)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_root_serves_the_page(self):
        status, response = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("loomy2api", response.read().decode("utf-8"))

    def test_health_advertises_the_panel(self):
        _s, payload = self.get_json("/health")
        self.assertTrue(payload["panel"].endswith("/panel"))


class TestPanelState(PanelHarness, unittest.TestCase):
    def test_state_lists_accounts_with_quota(self):
        _s, payload = self.get_json("/api/panel/state")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["totals"]["accounts"], 1)
        self.assertEqual(payload["totals"]["available"], 150)
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "a")
        self.assertEqual(account["available"], 150)
        self.assertEqual(account["balance"], 100)
        self.assertEqual(account["daily_balance"], 50)
        self.assertEqual(account["loginid_masked"], "138****0001")
        self.assertEqual(account["mode"], "password")
        self.assertNotIn('"password":', json.dumps(payload))

    def test_state_reports_identity(self):
        _s, payload = self.get_json("/api/panel/state")
        identity = payload["accounts"][0]["identity"]
        self.assertTrue(identity["bound"])
        self.assertEqual(identity["devid"], "web-aaaaaaaaaaaaaaaa")

    def test_state_reports_config(self):
        _s, payload = self.get_json("/api/panel/state")
        cfg = payload["config"]
        self.assertEqual(cfg["strategy"], "balance")
        self.assertEqual(cfg["identity_mode"], "per_account")
        self.assertFalse(cfg["auth_required"])

    def test_repeated_state_does_not_hit_upstream_again(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.assertEqual(before, after)          # quota cache is reused

    def test_refresh_flag_forces_upstream_call(self):
        self.get_json("/api/panel/state")
        before = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.get_json("/api/panel/state?refresh=1")
        after = len([c for c in self.upstream.calls if "points" in c["path"]])
        self.assertEqual(after, before + 1)


class TestPanelWrites(PanelHarness, unittest.TestCase):
    def test_add_account_with_password_logs_in_and_binds_identity(self):
        status, payload = self.post("/api/panel/accounts", {
            "name": "new", "loginid": "13900000002", "password": "pw", "login": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["logged_in"])
        self.assertTrue(payload["identity"]["devid"].startswith("web-"))
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        entry = [a for a in saved["accounts"] if a["name"] == "new"][0]
        self.assertEqual(entry["identity"]["devid"], payload["identity"]["devid"])
        self.assertIn("new", self.gateway.pool.client.fills)

    def test_add_account_with_session_only(self):
        _s, payload = self.post("/api/panel/accounts", {
            "name": "shared", "session": "borrowed-session"})
        self.assertTrue(payload["ok"])
        self.assertNotIn("logged_in", payload)
        acc = self.gateway.pool.get("shared")
        self.assertEqual(acc.session, "borrowed-session")
        self.assertTrue(acc.identity)          # identity bound at creation

    def test_add_without_credentials_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_duplicate_name_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts", {"name": "a", "session": "s"})
        self.assertEqual(ctx.exception.code, 400)

    def test_renew_refreshes_session_and_clears_cooldown(self):
        self.gateway.pool.get("a").cooldown(600, "test")
        _s, payload = self.post("/api/panel/accounts/renew", {"name": "a"})
        self.assertTrue(payload["ok"])
        acc = self.gateway.pool.get("a")
        self.assertFalse(acc.in_cooldown)
        self.assertAlmostEqual(acc.days_left, 14, delta=0.1)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_renew_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/renew", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_rebind_identity_changes_devid(self):
        before = self.gateway.pool.get("a").identity["devid"]
        _s, payload = self.post("/api/panel/accounts/identity",
                                {"name": "a", "regenerate": True})
        after = payload["identity"]["devid"]
        self.assertNotEqual(before, after)
        self.assertTrue(after.startswith("web-"))
        self.assertEqual(self.gateway.pool.client.rebinds, ["a"])

    def test_identity_inspect_does_not_regenerate(self):
        before = self.gateway.pool.get("a").identity["devid"]
        _s, payload = self.post("/api/panel/accounts/identity", {"name": "a"})
        self.assertEqual(payload["identity"]["devid"], before)

    def test_toggle_enabled(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "enabled": False})
        self.assertTrue(payload["ok"])
        self.assertFalse(self.gateway.pool.get("a").enabled)
        self.assertEqual(payload["state"]["totals"]["usable"], 0)

    def test_update_password_forces_new_login(self):
        _s, payload = self.post("/api/panel/accounts/update",
                                {"name": "a", "password": "new-pw", "login": True})
        acc = self.gateway.pool.get("a")
        self.assertEqual(acc.password, "new-pw")
        self.assertTrue(acc.session_valid)
        self.assertEqual(self.gateway.pool.client.fills, ["a"])

    def test_remove_account(self):
        _s, payload = self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertTrue(payload["ok"])
        self.assertIsNone(self.gateway.pool.get("a"))
        saved = json.loads((self.dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["accounts"], [])

    def test_remove_unknown_account_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_panel_endpoint_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/nonsense", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_panel_saves_socks5_proxy_without_leaking_the_password(self):
        cfg_path = self.dir / "config.json"
        cfg_path.write_text('{"strategy": "lru", "_comment": "keep"}\n', encoding="utf-8")
        status, payload = self.post("/api/panel/proxy", {
            "proxy": "socks5://alice:s3cret@10.1.1.1:1080",
        })
        self.assertEqual(status, 200)
        view = payload["proxy"]
        self.assertEqual(view["scheme"], "socks5")
        self.assertEqual(view["masked"], "socks5://alice:***@10.1.1.1:1080")
        self.assertNotIn("s3cret", json.dumps(payload))
        self.assertEqual(self.gateway.cfg["proxy"],
                         "socks5://alice:s3cret@10.1.1.1:1080")
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["_comment"], "keep")
        self.assertEqual(saved["strategy"], "lru")
        self.assertIn("s3cret", saved["proxy"])

        status, payload = self.post("/api/panel/proxy", {
            "scheme": "https", "host": "10.2.2.2", "port": 443, "username": "alice",
        })
        self.assertEqual(payload["proxy"]["scheme"], "https")
        self.assertTrue(payload["proxy"]["has_password"])
        self.assertNotIn("s3cret", json.dumps(payload))
        self.assertIn("s3cret", self.gateway.cfg["proxy"])

        self.post("/api/panel/proxy", {"scheme": "direct"})
        self.assertEqual(self.gateway.cfg["proxy"], "")
        _s, state = self.get_json("/api/panel/state")
        self.assertFalse(state["config"]["proxy"]["enabled"])

    def test_proxy_scheme_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/proxy", {"proxy": "file:///tmp/x"})
        self.assertEqual(ctx.exception.code, 400)

    def test_logs_endpoint(self):
        self.gateway.log("panel test line")
        _s, payload = self.get_json("/api/panel/logs?lines=20")
        self.assertTrue(payload["ok"])
        self.assertTrue(any("panel test line" in line for line in payload["lines"]))


class TestPanelAuth(PanelHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["panel-key"]

    def test_state_requires_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/api/panel/state")
        self.assertEqual(ctx.exception.code, 401)

    def test_state_with_key(self):
        _s, payload = self.get_json("/api/panel/state",
                                    headers={"Authorization": "Bearer panel-key"})
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["config"]["auth_required"])

    def test_proxy_requires_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/proxy", {"scheme": "direct"})
        self.assertEqual(ctx.exception.code, 401)

    def test_writes_require_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/accounts/remove", {"name": "a"})
        self.assertEqual(ctx.exception.code, 401)
        self.assertIsNotNone(self.gateway.pool.get("a"))

    def _login(self, key="panel-key", headers=None):
        request = urllib.request.Request(
            self.base + "/api/panel/login",
            data=json.dumps({"api_key": key}).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=30)
        set_cookie = response.headers.get("Set-Cookie") or ""
        response.read()
        return set_cookie

    def test_anonymous_page_is_login_only(self):
        status, response = self.get("/panel")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("登录", body)
        self.assertNotIn("添加账号", body)
        self.assertNotIn("/api/panel/state", body)
        self.assertNotIn("localStorage.setItem", body)

        status, response = self.get("/")
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("登录", body)
        self.assertNotIn("添加账号", body)

    def test_session_endpoint_is_anonymous_and_empty(self):
        _s, payload = self.get_json("/api/panel/session")
        self.assertEqual(payload, {
            "ok": True, "auth_required": True, "authenticated": False,
        })

    def test_login_sets_httponly_cookie_and_unlocks_panel(self):
        set_cookie = self._login()
        parts = [part.strip() for part in set_cookie.split(";")]
        self.assertTrue(parts[0].startswith("loomy_panel="))
        self.assertIn("HttpOnly", parts)
        self.assertIn("SameSite=Lax", parts)
        self.assertNotIn("Secure", parts)
        self.assertNotIn("panel-key", parts[0])
        cookie = set_cookie.split(";", 1)[0]

        status, response = self.get("/panel", headers={"Cookie": cookie})
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("添加账号", body)
        self.assertNotIn("localStorage.setItem", body)

        _s, payload = self.get_json("/api/panel/state", headers={"Cookie": cookie})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["accounts"][0]["name"], "a")

        status, payload = self.post(
            "/api/panel/accounts/remove", {"name": "a"},
            headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_login_cookie_is_secure_behind_https_proxy(self):
        set_cookie = self._login(headers={"X-Forwarded-Proto": "https"})
        parts = [part.strip() for part in set_cookie.split(";")]
        self.assertIn("Secure", parts)

    def test_panel_cookie_does_not_authorize_model_api(self):
        cookie = self._login().split(";", 1)[0]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/v1/models", headers={"Cookie": cookie})
        self.assertEqual(ctx.exception.code, 401)

    def test_wrong_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._login(key="nope")
        self.assertEqual(ctx.exception.code, 401)

    def test_login_locks_out_after_repeated_failures(self):
        codes = []
        for _ in range(8):
            try:
                self._login(key="nope")
                codes.append(200)
            except urllib.error.HTTPError as exc:
                codes.append(exc.code)
                exc.read()
        self.assertEqual(codes, [401] * 7 + [429])

    def test_logout_revokes_the_session(self):
        cookie = self._login().split(";", 1)[0]
        request = urllib.request.Request(
            self.base + "/api/panel/logout", data=b"{}", method="POST",
            headers={"Cookie": cookie, "Content-Type": "application/json"})
        response = urllib.request.urlopen(request, timeout=30)
        cleared = response.headers.get("Set-Cookie") or ""
        response.read()
        self.assertIn("Max-Age=0", cleared)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_json("/api/panel/state", headers={"Cookie": cookie})
        self.assertEqual(ctx.exception.code, 401)
        status, response = self.get("/panel", headers={"Cookie": cookie})
        body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("添加账号", body)


class TestPanelLoginDisabled(PanelHarness, unittest.TestCase):
    def test_login_is_rejected_when_no_api_key_is_configured(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/panel/login", {"api_key": "anything"})
        self.assertEqual(ctx.exception.code, 400)

    def test_open_panel_session_reports_auth_off(self):
        _s, payload = self.get_json("/api/panel/session")
        self.assertEqual(payload["auth_required"], False)
        self.assertEqual(payload["authenticated"], True)


if __name__ == "__main__":
    unittest.main()
