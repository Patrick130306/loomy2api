"""Proxy transport: direct, HTTP CONNECT, HTTPS proxy, and SOCKS5."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse

from loomy2api.httpc import (
    ProxyError, compose_proxy, normalize_proxy, proxy_view, request,
)


def _recv_headers(conn: socket.socket) -> bytes:
    data = b""
    conn.settimeout(3)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _pong(conn: socket.socket) -> None:
    _recv_headers(conn)
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\npong")
    conn.close()


def _splice(left: socket.socket, right: socket.socket) -> None:
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(left, right), daemon=True),
        threading.Thread(target=pump, args=(right, left), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)


class _Server:
    def __init__(self, handler):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.3)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(handler,), daemon=True)
        self._thread.start()

    def _run(self, handler):
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._guard, args=(handler, conn), daemon=True).start()

    @staticmethod
    def _guard(handler, conn):
        try:
            handler(conn)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)


def _http_origin(conn: socket.socket) -> None:
    _pong(conn)


def _http_proxy(conn: socket.socket) -> None:
    head = _recv_headers(conn)
    line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = line.split(" ")
    if len(parts) < 3:
        conn.close()
        return
    method, target = parts[0], parts[1]
    if method == "CONNECT":
        host, port = target.rsplit(":", 1)
        remote = socket.create_connection((host.strip("[]"), int(port)), timeout=3)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        _splice(conn, remote)
        return
    parsed = urlparse(target)
    remote = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
    remote.sendall(head)
    _splice(conn, remote)


def _socks5(conn: socket.socket, *, user: str = "", password: str = "") -> None:
    version, nmethods = conn.recv(1)[0], None
    # recv may return just one byte; read exactly.
    rest = b""
    need = 1
    got = conn.recv(1)
    nmethods = got[0]
    methods = b""
    while len(methods) < nmethods:
        methods += conn.recv(nmethods - len(methods))
    if user:
        if 2 not in methods:
            conn.sendall(b"\x05\xff")
            conn.close()
            return
        conn.sendall(b"\x05\x02")
        auth = conn.recv(2)
        ulen = auth[1]
        uname = conn.recv(ulen)
        plen = conn.recv(1)[0]
        passwd = conn.recv(plen)
        if uname.decode() != user or passwd.decode() != password:
            conn.sendall(b"\x01\x01")
            conn.close()
            return
        conn.sendall(b"\x01\x00")
    else:
        conn.sendall(b"\x05\x00")
    hdr = b""
    while len(hdr) < 4:
        hdr += conn.recv(4 - len(hdr))
    atyp = hdr[3]
    if atyp == 3:
        length = conn.recv(1)[0]
        host = b""
        while len(host) < length:
            host += conn.recv(length - len(host))
        host = host.decode()
    elif atyp == 1:
        host = socket.inet_ntoa(conn.recv(4))
    else:
        conn.close()
        return
    port_bytes = b""
    while len(port_bytes) < 2:
        port_bytes += conn.recv(2 - len(port_bytes))
    port = int.from_bytes(port_bytes, "big")
    try:
        remote = socket.create_connection((host, port), timeout=3)
    except OSError:
        conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
        conn.close()
        return
    conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
    _splice(conn, remote)


class ProxyTransportTest(unittest.TestCase):
    def test_direct_http(self):
        origin = _Server(_http_origin)
        self.addCleanup(origin.close)
        status, _hdrs, body = request(f"http://127.0.0.1:{origin.port}/hi", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"pong")

    def test_socks5_forwards_http(self):
        origin = _Server(_http_origin)
        proxy = _Server(_socks5)
        self.addCleanup(origin.close)
        self.addCleanup(proxy.close)
        status, _hdrs, body = request(
            f"http://127.0.0.1:{origin.port}/hi",
            proxy=f"socks5://127.0.0.1:{proxy.port}", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"pong")

    def test_socks5_password(self):
        origin = _Server(_http_origin)
        proxy = _Server(lambda conn: _socks5(conn, user="alice", password="s3cret"))
        self.addCleanup(origin.close)
        self.addCleanup(proxy.close)
        status, _hdrs, body = request(
            f"http://127.0.0.1:{origin.port}/hi",
            proxy=f"socks5://alice:s3cret@127.0.0.1:{proxy.port}", timeout=5)
        self.assertEqual(body, b"pong")
        self.assertEqual(status, 200)
        with self.assertRaises(ProxyError):
            request(f"http://127.0.0.1:{origin.port}/hi",
                    proxy=f"socks5://alice:nope@127.0.0.1:{proxy.port}", timeout=5)

    def test_http_proxy_absolute_form(self):
        origin = _Server(_http_origin)
        proxy = _Server(_http_proxy)
        self.addCleanup(origin.close)
        self.addCleanup(proxy.close)
        status, _hdrs, body = request(
            f"http://127.0.0.1:{origin.port}/hi",
            proxy=f"http://127.0.0.1:{proxy.port}", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"pong")

    def test_normalize_and_compose(self):
        self.assertEqual(normalize_proxy(""), "")
        self.assertEqual(normalize_proxy("socks5h://10.0.0.8:1080"),
                         "socks5://10.0.0.8:1080")
        url = compose_proxy(scheme="socks5", host="10.0.0.8", port=1080,
                            username="u", password="p@ss")
        self.assertEqual(url, "socks5://u:p%40ss@10.0.0.8:1080")
        again = compose_proxy(scheme="https", host="10.0.0.9", port=443,
                              username="u", password=None, previous=url)
        self.assertIn("p%40ss", again)
        view = proxy_view(url)
        self.assertNotIn("p@ss", str(view))
        self.assertEqual(view["masked"], "socks5://u:***@10.0.0.8:1080")
        self.assertTrue(view["has_password"])
        with self.assertRaises(ValueError):
            normalize_proxy("file:///tmp/x")
        self.assertEqual(compose_proxy(scheme="direct"), "")


class HttpsProxyTest(unittest.TestCase):
    def setUp(self):
        openssl = subprocess.run(["openssl", "version"], capture_output=True)
        if openssl.returncode != 0:
            self.skipTest("openssl is required to mint a test certificate")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cert = Path(self.tmp.name) / "cert.pem"
        key = Path(self.tmp.name) / "key.pem"
        minted = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert), "-days", "1", "-nodes",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ], capture_output=True)
        if minted.returncode != 0:
            self.skipTest(minted.stderr.decode("utf-8", "replace")[:200])
        self.cert = cert
        self.key = key

    def _tls_server(self, handler):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)

        def wrapped(conn):
            try:
                tls = context.wrap_socket(conn, server_side=True)
            except ssl.SSLError:
                conn.close()
                return
            handler(tls)

        return _Server(wrapped)

    def test_https_proxy_connects_to_https_origin(self):
        origin = self._tls_server(_pong)
        proxy = self._tls_server(_http_proxy)
        self.addCleanup(origin.close)
        self.addCleanup(proxy.close)
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(self.cert)
        try:
            status, _hdrs, body = request(
                f"https://127.0.0.1:{origin.port}/hi",
                proxy=f"https://127.0.0.1:{proxy.port}", timeout=5)
        finally:
            if previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous
        self.assertEqual(status, 200)
        self.assertEqual(body, b"pong")


if __name__ == "__main__":
    unittest.main()
