"""End-to-end gateway tests against a local fake upstream (hermetic).

Covers: OpenAI chat (stream + non-stream), Anthropic /v1/messages (both),
model catalogue, quota endpoint, passthroughs, API-key auth and account
rotation when one account's session is rejected.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from loomy2api.server import Gateway, Handler, Server
from tests.support import FakeUpstream, make_config, write_accounts


class _Harness:
    """Shared fixture: fake upstream + a real gateway on an ephemeral port.

    Deliberately not a ``TestCase`` — the auth tests below need the same
    fixture without inheriting the unauthenticated test cases.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.upstream = FakeUpstream()
        base = self.upstream.start()
        write_accounts(self.dir / "accounts.json", [
            {"name": "a", "session": "s1",
             "expireAt": int(__import__("time").time()) + 14 * 86400},
            {"name": "b", "session": "s2",
             "expireAt": int(__import__("time").time()) + 14 * 86400},
        ])
        self.cfg = make_config(self.dir, base)
        self.gateway = Gateway(self.cfg)
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

    def post(self, path, payload, *, stream=False, headers=None, timeout=30):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST")
        response = urllib.request.urlopen(request, timeout=timeout)
        body = response.read()
        return response.status, body, response

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, json.loads(response.read())

    def sse(self, body: bytes):
        """Parse an SSE payload into ``[(event, data)]``."""
        events = []
        for block in body.decode("utf-8").split("\n\n"):
            event, data = None, None
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            if event or data:
                events.append((event, data))
        return events

    # -- tests ----------------------------------------------------------


class GatewayTestCase(_Harness, unittest.TestCase):
    """OpenAI/Anthropic surface, no gateway-side auth configured."""

    def test_health(self):
        status, payload = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["accounts"], 2)
        self.assertEqual(payload["usable_accounts"], 2)
        self.assertFalse(payload["auth_required"])

    def test_models_lists_the_upstream_catalogue(self):
        _status, payload = self.get("/v1/models")
        ids = [m["id"] for m in payload["data"]]
        self.assertIn("fake-model", ids)
        self.assertTrue(all(m.get("owned_by") != "alias" for m in payload["data"]))

    def test_chat_non_stream(self):
        status, body, _ = self.post("/v1/chat/completions", {
            "model": "imodel/fake-model",
            "messages": [{"role": "user", "content": "ping"}],
        })
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "fake-model")     # prefix stripped
        self.assertEqual(payload["choices"][0]["message"]["content"], "echo:ping")
        self.assertEqual(payload["usage"]["points_consumed"], 1)

    def test_model_name_is_passed_through_verbatim(self):
        """No alias table: an unknown name goes upstream as-is (so a typo is a
        400 from the upstream, not a silent redirect to another model)."""
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(json.loads(body)["model"], "gpt-4o-mini")

    def test_missing_model_falls_back_to_default(self):
        _s, body, _ = self.post("/v1/chat/completions", {
            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(json.loads(body)["model"], self.cfg["default_model"])

    def test_chat_stream(self):
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        text = body.decode("utf-8")
        self.assertIn("data: ", text)
        self.assertIn('"content": "hello"', text)
        self.assertIn('"points_consumed": 1', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_messages_non_stream(self):
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        payload = json.loads(body)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"][0]["type"], "text")
        self.assertEqual(payload["content"][0]["text"], "echo:hi")
        self.assertEqual(payload["usage"]["output_tokens"], 5)

    def test_messages_stream(self):
        _s, body, _ = self.post("/v1/messages", {
            "model": "fake-model", "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        events = self.sse(body)
        names = [e for e, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-1], "message_stop")
        self.assertIn("content_block_delta", names)
        deltas = [json.loads(d) for e, d in events if e == "content_block_delta"]
        self.assertTrue(any(d["delta"].get("type") == "thinking_delta"
                            and d["delta"].get("thinking") == "think " for d in deltas))
        self.assertTrue(any(d["delta"].get("type") == "text_delta"
                            and d["delta"].get("text") == "hello" for d in deltas))

    def test_points_endpoint(self):
        _s, payload = self.get("/v1/points")
        self.assertEqual(payload["total_available"], 300)     # 150 × 2 accounts
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(payload["unique_accounts"], 2)

    def test_points_dedupes_same_user(self):
        """Two sessions of one account must not double-count the quota."""
        self.gateway.pool.get("a").userid = "u1"
        self.gateway.pool.get("b").userid = "u1"
        _s, payload = self.get("/v1/points")
        self.assertEqual(payload["total_available"], 150)
        self.assertEqual(payload["unique_accounts"], 1)

    def test_embeddings_and_images_passthrough(self):
        _s, body, _ = self.post("/v1/embeddings", {"model": "fake-model", "input": ["a"]})
        self.assertEqual(json.loads(body)["data"][0]["embedding"], [0.1])
        _s, body, _ = self.post("/v1/images/generations", {"model": "fake-model",
                                                          "prompt": "cat"})
        self.assertIn("url", json.loads(body)["data"][0])

    def test_rotation_on_rejected_session(self):
        # make the pick deterministic: the richest account is tried first
        self.gateway.pool.get("a").available = 1000
        self.gateway.pool.get("b").available = 10
        self.upstream.valid_sessions = ["s2"]         # account "a" is now invalid
        status, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "echo:x")
        # the rejected account is cooled down and its dead session dropped
        acc_a = self.gateway.pool.get("a")
        self.assertFalse(acc_a.session_valid)
        self.assertGreater(acc_a.failures, 0)
        self.assertTrue(acc_a.in_cooldown)

    def test_upstream_5xx_does_not_cool_down_the_account(self):
        """A 504/502 from the upstream is not the account's fault: retry on
        another account but keep the first one healthy (it did nothing wrong)."""
        self.upstream.reject_with = {"fake-model": (504, "upstream hiccup")}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 504)
        self.assertEqual(self.gateway.pool.usable(), self.gateway.pool.accounts)
        for acc in self.gateway.pool.accounts:
            self.assertFalse(acc.in_cooldown,
                             "5xx must not put an account in cooldown")
            self.assertTrue(acc.session_valid, "5xx must not drop the session")

    def test_rotation_when_account_quota_exhausted(self):
        self.upstream.exhausted_sessions = ["s1", "s2"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions", {
                "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 402)

    def test_stream_falls_back_to_healthy_account(self):
        self.upstream.valid_sessions = ["s2"]
        _s, body, _ = self.post("/v1/chat/completions", {
            "model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("hello", body.decode("utf-8"))

    def test_bad_json_returns_400(self):
        request = urllib.request.Request(self.base + "/v1/chat/completions",
                                         data=b"{not json",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("JSON", ctx.exception.read().decode("utf-8"))

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/nope", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_admin_accounts(self):
        _s, payload = self.get("/v1/admin/accounts")
        self.assertEqual(payload["count"], 2)
        self.assertNotIn('"password":', json.dumps(payload))


class AuthTestCase(_Harness, unittest.TestCase):
    """Same gateway, but with a gateway-side API key configured."""
    def setUp(self):
        super().setUp()
        self.gateway.cfg["api_keys"] = ["secret-key"]

    def test_missing_key_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/v1/chat/completions",
                      {"model": "fake-model",
                       "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 401)

    def test_bearer_key_accepted(self):
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer secret-key"})
        self.assertEqual(status, 200)

    def test_x_api_key_accepted(self):
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"x-api-key": "secret-key"})
        self.assertEqual(status, 200)

    def test_health_stays_public(self):
        status, _payload = self.get("/health")
        self.assertEqual(status, 200)

    def test_bare_authorization_without_bearer(self):
        """Some clients send the raw key as the Authorization value."""
        status, _body, _ = self.post(
            "/v1/chat/completions",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "secret-key"})
        self.assertEqual(status, 200)

    def test_query_string_key(self):
        status, _body, _ = self.post(
            "/v1/chat/completions?api_key=secret-key",
            {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)

    def test_key_with_stray_whitespace_or_quotes(self):
        # (a raw newline can't be sent by urllib and no real client does it,
        #  but trailing spaces and quotes from copy-paste are common)
        for raw in (' "secret-key" ', "bearer secret-key ", " secret-key\t"):
            status, _body, _ = self.post(
                "/v1/chat/completions",
                {"model": "fake-model", "messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": raw})
            self.assertEqual(status, 200, f"rejected: {raw!r}")

    def test_wrong_key_rejected_and_logged(self):
        buf = io.StringIO()
        original = self.gateway.log
        self.gateway.log = lambda msg, **kw: (buf.write(str(msg) + "\n"), original(msg))[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/v1/chat/completions",
                          {"model": "fake-model",
                           "messages": [{"role": "user", "content": "x"}]},
                          headers={"Authorization": "Bearer wrong-key-123456"})
        finally:
            self.gateway.log = original
        self.assertEqual(ctx.exception.code, 401)
        logged = buf.getvalue()
        self.assertIn("[auth] 拒绝", logged)
        self.assertIn("authorization:bearer", logged)
        self.assertIn("wrong-ke…", logged)          # masked, not the full secret
        self.assertNotIn("wrong-key-123456", logged)


if __name__ == "__main__":
    unittest.main()
