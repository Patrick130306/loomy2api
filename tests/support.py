"""Shared test helpers — no third-party dependencies, no network.

Everything runs against a local fake upstream so the suite is hermetic: no
Loomy account, no points and no iFlytek endpoint is ever touched.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------- RSA


def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:                        # keep the INTEGER positive
        raw = b"\x00" + raw
    return _der(0x02, raw)


def rsa_keypair(bits: int = 512):
    """Toy RSA keypair for round-trip tests (Miller-Rabin, stdlib only)."""
    import random

    def is_prime(n: int, rounds: int = 24) -> bool:
        if n < 2:
            return False
        for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % p == 0:
                return n == p
        d, r = n - 1, 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for _ in range(rounds):
            a = random.randrange(2, n - 1)
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = x * x % n
                if x == n - 1:
                    break
            else:
                return False
        return True

    def prime(bits_: int) -> int:
        while True:
            candidate = random.getrandbits(bits_) | (1 << (bits_ - 1)) | 1
            if is_prime(candidate):
                return candidate

    e = 65537
    while True:
        p, q = prime(bits // 2), prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return p * q, e, d


def spki_der_b64(n: int, e: int) -> str:
    """Build a SubjectPublicKeyInfo DER for RSA and return it base64-encoded."""
    rsa_oid = bytes.fromhex("06092A864886F70D010101")
    alg = _der(0x30, rsa_oid + b"\x05\x00")
    key = _der(0x30, _int(n) + _int(e))
    spki = _der(0x30, alg + _der(0x03, b"\x00" + key))
    return base64.b64encode(spki).decode()


# ------------------------------------------------------------- fake upstream


class FakeUpstream:
    """Local stand-in for the Loomy model gateway + iFlytek account service.

    Behaviour switches let tests exercise rotation (``valid_sessions``),
    quota exhaustion (``exhausted_sessions``) and error paths.
    """

    def __init__(self):
        self.valid_sessions: Optional[List[str]] = None   # None = accept any
        self.exhausted_sessions: List[str] = []
        #: model-id substring → (status, message); simulates upstream hiccups
        self.reject_with: Dict[str, Any] = {}
        self.calls: List[Dict[str, Any]] = []
        self.models_payload = {
            "object": "list",
            "data": [{"id": "fake-model", "object": "model", "name": "Fake (x1.0)",
                      "context_length": 1000,
                      "capabilities": {"reasoning": True, "function_calling": True}}],
        }
        self.quota = {"balance": 100, "dailyBalance": 50, "availableBalance": 150}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> str:
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/api/v1"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # -- helpers --------------------------------------------------------

    def _token(self, handler) -> str:
        auth = handler.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return handler.headers.get("token") or ""

    def _reject(self, handler, status: int, message: str = "denied") -> None:
        body = json.dumps({"error": {"message": message}}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_json(self, handler, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _read(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:                       # noqa: BLE001
                    return {}

            def do_GET(self):                           # noqa: N802
                token = fake._token(self)
                path = self.path.split("?")[0]
                fake.calls.append({"path": path, "token": token, "body": None})
                if fake.valid_sessions is not None and token not in fake.valid_sessions:
                    return fake._reject(self, 401, "invalid session")
                if path.endswith("/models"):
                    return fake._send_json(self, fake.models_payload)
                if path.endswith("/points/records"):
                    return fake._send_json(self, {
                        "code": "000000", "desc": "成功",
                        "data": dict(fake.quota, list=[])})
                return fake._reject(self, 404, "not found")

            def do_POST(self):                          # noqa: N802
                token = fake._token(self)
                path = self.path.split("?")[0]
                body = self._read()
                fake.calls.append({"path": path, "token": token, "body": body})
                if fake.valid_sessions is not None and token not in fake.valid_sessions:
                    return fake._reject(self, 401, "invalid session")
                if token in fake.exhausted_sessions:
                    return fake._reject(self, 402, "insufficient points")

                if path.endswith("/chat/completions"):
                    model = body.get("model", "fake-model")
                    for needle, (status, message) in fake.reject_with.items():
                        if needle in str(model):
                            return fake._reject(self, status, message)
                    if body.get("stream"):
                        return self._stream(model)
                    return fake._send_json(self, {
                        "id": "chatcmpl-fake", "object": "chat.completion",
                        "model": model,
                        "choices": [{"index": 0, "message": {
                            "role": "assistant",
                            "content": f"echo:{body.get('messages', [{}])[-1].get('content')}"},
                            "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 5,
                                  "points_consumed": 1},
                    })
                if path.endswith("/images/generations"):
                    return fake._send_json(self, {"data": [{"url": "http://x/img.png"}]})
                if path.endswith("/embeddings"):
                    return fake._send_json(self, {"data": [{"index": 0, "embedding": [0.1]}]})
                return fake._reject(self, 404, "not found")

            def _stream(self, model: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                        "reasoning_content": "think "}}]},
                    {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
                    {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 4,
                                              "points_consumed": 1}},
                ]
                for chunk in chunks:
                    payload = {"id": "chatcmpl-fake", "model": model, **chunk}
                    self.wfile.write(
                        f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler


# ------------------------------------------------------------------ cfg/pool


class FakeAccountClient:
    """Stand-in for :class:`loomy2api.account.AccountClient` (no network).

    Records logins so tests can assert *when* a session was refreshed, and
    mimics identity binding without touching the account service.
    """

    def __init__(self, session_prefix: str = "testsession"):
        self.fills: List[str] = []
        self.rebinds: List[str] = []
        self.session_prefix = session_prefix
        self.counter = 0

    def identity_mode(self) -> str:
        return "per_account"

    def ensure_identity(self, account):
        from loomy2api.account import new_identity
        if not account.identity:
            account.identity = new_identity("per_account")
        return account.identity

    def rebind_identity(self, account):
        from loomy2api.account import new_identity
        account.identity = new_identity("per_account")
        self.rebinds.append(account.name)
        return account.identity

    def fill(self, account):
        import time as _time
        self.counter += 1
        self.fills.append(account.name)
        account.session = f"{self.session_prefix}{self.counter}"
        account.userid = account.userid or f"uid-{account.loginid}"
        account.obtained_at = int(_time.time())
        account.expire_at = account.obtained_at + 14 * 86400
        return account

    # SMS helper used by the CLI paths
    def send_sms_code(self, phone, identity=None):
        return {"code": "000000", "data": {"msgid": "msgid-test"}}

    def login_by_sms(self, phone, code, msgid, identity=None):
        return {"session": "sms-session", "userid": "uid-sms", "phone": phone}

    def login_by_password(self, loginid, password, identity=None):
        return {"session": "pwd-session", "userid": f"uid-{loginid}", "phone": loginid}

    def get_public_key(self, identity=None):
        return "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDN", "rcode-test"


def write_accounts(path: Path, accounts: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False),
                    encoding="utf-8")


def make_config(tmp: Path, upstream: str, **overrides) -> Any:
    from loomy2api.config import load_config

    cfg = load_config(Path(tmp) / "config.json")
    cfg.update({
        "host": "127.0.0.1",
        "port": 0,
        "upstream": upstream,
        "account_base": "http://127.0.0.1:1",     # never contacted in these tests
        "accounts_file": str(Path(tmp) / "accounts.json"),
        "log_dir": str(Path(tmp) / "logs"),
        "sessions_from_client": False,
        "quota_refresh_minutes": 999,
        "max_retries": 2,
        "cooldown_seconds": 60,
        "log_console": False,
    })
    cfg.update(overrides)
    return cfg


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False
