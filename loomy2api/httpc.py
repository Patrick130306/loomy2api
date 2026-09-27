"""Thin HTTP helpers (stdlib only) with explicit proxy support.

``urllib`` reads ``http_proxy`` / ``https_proxy`` implicitly, which is usually
the opposite of what you want here: the upstreams are domestic Chinese
endpoints, so the default is *direct* and a proxy must be configured on
purpose.

``proxy`` accepts ``http://``, ``https://`` and ``socks5://`` (``socks5h://``
is accepted and treated the same). SOCKS5 always asks the proxy to resolve
the name. An HTTPS proxy is a TLS-wrapped HTTP proxy: connect to it with
TLS, CONNECT the target, then TLS again when the target itself is HTTPS.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import ssl
from http.client import HTTPConnection
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

__all__ = [
    "ProxyError", "request", "open_stream", "json_body",
    "normalize_proxy", "proxy_view", "compose_proxy",
]

HttpResult = Tuple[int, Dict[str, str], bytes]

_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


class ProxyError(OSError):
    """The configured proxy could not be used."""


class _Endpoint:
    def __init__(self, scheme: str, host: str, port: int,
                 username: str = "", password: str = ""):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password


class _ReadyConnection(HTTPConnection):
    """An ``HTTPConnection`` whose socket is already the target tunnel."""

    def __init__(self, host: str, port: int, sock: socket.socket, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.sock = sock

    def connect(self) -> None:                        # already connected
        return


class _HttpProxyConnection(HTTPConnection):
    """Speak HTTP to a proxy while the Host header names the real target."""

    def __init__(self, proxy_host: str, proxy_port: int,
                 target_host: str, target_port: int, timeout: float):
        super().__init__(target_host, target_port, timeout=timeout)
        self._proxy_addr = (proxy_host, proxy_port)

    def connect(self) -> None:
        self.sock = socket.create_connection(self._proxy_addr, self.timeout)
        self.sock.settimeout(self.timeout)


def _default_proxy_port(scheme: str) -> int:
    if scheme in ("socks5", "socks5h"):
        return 1080
    if scheme == "https":
        return 443
    return 80


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def parse_proxy(value: str) -> Optional[_Endpoint]:
    """Parse a proxy URL. Empty means direct. Raises ``ValueError`` when bad."""
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _PROXY_SCHEMES:
        raise ValueError(
            "代理只支持 http://、https://、socks5:// / "
            "proxy scheme must be http, https, or socks5")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("代理缺少主机 / proxy host is required")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效 / invalid proxy port") from exc
    port = port or _default_proxy_port(scheme)
    if not 1 <= port <= 65535:
        raise ValueError("代理端口无效 / invalid proxy port")
    username = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password else ""
    canonical = "socks5" if scheme == "socks5h" else scheme
    return _Endpoint(canonical, host, port, username, password)


def normalize_proxy(value: str) -> str:
    """Canonical proxy URL, or ``\"\"`` for a direct connection."""
    endpoint = parse_proxy(value)
    if endpoint is None:
        return ""
    return _format_endpoint(endpoint)


def compose_proxy(*, scheme: str, host: str = "", port=None,
                  username: str = "", password: Optional[str] = None,
                  previous: str = "") -> str:
    """Build a proxy URL from the panel form.

    ``password is None`` keeps the previous password when the username did
    not change. An empty string clears it.
    """
    scheme = (scheme or "").strip().lower()
    if scheme in ("", "direct", "none", "off"):
        return ""
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in ("http", "https", "socks5"):
        raise ValueError(
            "代理只支持 http、https、socks5 / "
            "proxy scheme must be http, https, or socks5")
    host = (host or "").strip().strip("[]")
    if not host:
        raise ValueError("代理缺少主机 / proxy host is required")
    if port in ("", None):
        port_num = _default_proxy_port(scheme)
    else:
        try:
            port_num = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError("代理端口无效 / invalid proxy port") from exc
    if not 1 <= port_num <= 65535:
        raise ValueError("代理端口无效 / invalid proxy port")
    username = username or ""
    if password is None:
        previous_ep = parse_proxy(previous) if previous else None
        if (previous_ep and previous_ep.username == username
                and previous_ep.password):
            password = previous_ep.password
        else:
            password = ""
    return _format_endpoint(_Endpoint(scheme, host, port_num, username, password or ""))


def proxy_view(value: str) -> Dict[str, object]:
    """Panel-safe description. Never includes the password."""
    try:
        endpoint = parse_proxy(value)
    except ValueError:
        endpoint = None
    if endpoint is None:
        return {
            "enabled": False, "scheme": "direct", "host": "", "port": "",
            "username": "", "has_password": False, "masked": "",
        }
    auth = ""
    if endpoint.username:
        auth = quote(endpoint.username, safe="") + (":***@" if endpoint.password else "@")
    masked = (f"{endpoint.scheme}://{auth}{_format_host(endpoint.host)}:{endpoint.port}")
    return {
        "enabled": True,
        "scheme": endpoint.scheme,
        "host": endpoint.host,
        "port": endpoint.port,
        "username": endpoint.username,
        "has_password": bool(endpoint.password),
        "masked": masked,
    }


def _format_endpoint(endpoint: _Endpoint) -> str:
    auth = ""
    if endpoint.username or endpoint.password:
        user = quote(endpoint.username, safe="")
        if endpoint.password:
            auth = f"{user}:{quote(endpoint.password, safe='')}@"
        else:
            auth = f"{user}@"
    return (f"{endpoint.scheme}://{auth}"
            f"{_format_host(endpoint.host)}:{endpoint.port}")


def _split(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"unsupported URL: {url}")
    return parsed


def _target(url: str) -> _Endpoint:
    parsed = _split(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _Endpoint(parsed.scheme, parsed.hostname, port)


def _path_with_query(url: str) -> str:
    parsed = _split(url)
    path = parsed.path or "/"
    return path + (f"?{parsed.query}" if parsed.query else "")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ProxyError("代理提前断开连接 / proxy closed the connection")
        buf += chunk
    return buf


def _proxy_basic(username: str, password: str) -> Dict[str, str]:
    if not username and not password:
        return {}
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Proxy-Authorization": f"Basic {token}"}


def _read_http_head(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def _http_connect(sock: socket.socket, host: str, port: int,
                  username: str = "", password: str = "") -> None:
    lines = [
        f"CONNECT {_format_host(host)}:{port} HTTP/1.1",
        f"Host: {_format_host(host)}:{port}",
    ]
    basic = _proxy_basic(username, password)
    if basic:
        lines.append(f"Proxy-Authorization: {basic['Proxy-Authorization']}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    head = _read_http_head(sock)
    status = head.split(b"\r\n", 1)[0]
    parts = status.split()
    if len(parts) < 2 or parts[1] != b"200":
        shown = status.decode("utf-8", "replace")[:120]
        raise ProxyError(f"CONNECT 被拒绝 / CONNECT rejected: {shown}")


def _socks5_connect(sock: socket.socket, host: str, port: int,
                    username: str = "", password: str = "") -> None:
    host_bytes = host.encode("idna")
    if len(host_bytes) > 255:
        raise ProxyError("SOCKS5 主机名过长 / SOCKS5 hostname is too long")
    if username or password:
        sock.sendall(b"\x05\x02\x00\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    version, method = _recv_exact(sock, 2)
    if version != 5:
        raise ProxyError("不是 SOCKS5 代理 / not a SOCKS5 proxy")
    if method == 2:
        user = username.encode("utf-8")
        passwd = password.encode("utf-8")
        if len(user) > 255 or len(passwd) > 255:
            raise ProxyError("SOCKS5 账号过长 / SOCKS5 credentials are too long")
        sock.sendall(bytes((1, len(user))) + user + bytes((len(passwd),)) + passwd)
        auth_ver, auth_status = _recv_exact(sock, 2)
        if auth_ver != 1 or auth_status != 0:
            raise ProxyError("SOCKS5 认证失败 / SOCKS5 authentication failed")
    elif method != 0:
        raise ProxyError("SOCKS5 代理不接受这种认证 / SOCKS5 auth method rejected")
    request = b"\x05\x01\x00\x03" + bytes((len(host_bytes),)) + host_bytes + port.to_bytes(2, "big")
    sock.sendall(request)
    reply = _recv_exact(sock, 4)
    if reply[1] != 0:
        raise ProxyError(f"SOCKS5 连接失败 / SOCKS5 connect failed (code {reply[1]})")
    atyp = reply[3]
    if atyp == 1:
        _recv_exact(sock, 6)
    elif atyp == 4:
        _recv_exact(sock, 18)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length + 2)
    else:
        raise ProxyError("SOCKS5 应答无法识别 / unrecognised SOCKS5 reply")


def _tls(sock: socket.socket, server_hostname: str) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    return context.wrap_socket(sock, server_hostname=server_hostname)


class _Reader(io.RawIOBase):
    def __init__(self, stream: "_OverTLS"):
        self.stream = stream

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        data = self.stream.recv(len(buf))
        if not data:
            return 0
        n = len(data)
        buf[:n] = data
        return n


class _OverTLS:
    """TLS written through an existing stream.

    Used when the proxy itself is already TLS. ``SSLContext.wrap_socket``
    would handshake on the raw file descriptor and corrupt that outer TLS,
    so this layer uses a memory BIO instead.
    """

    def __init__(self, transport: socket.socket, server_hostname: str):
        self._transport = transport
        self._in = ssl.MemoryBIO()
        self._out = ssl.MemoryBIO()
        context = ssl.create_default_context()
        self._ssl = context.wrap_bio(
            self._in, self._out, server_hostname=server_hostname)
        self._handshake()

    def _flush(self) -> None:
        data = self._out.read()
        if data:
            self._transport.sendall(data)

    def _fill(self) -> None:
        self._flush()
        chunk = self._transport.recv(16384)
        if chunk:
            self._in.write(chunk)
        else:
            self._in.write_eof()

    def _handshake(self) -> None:
        while True:
            try:
                self._ssl.do_handshake()
                self._flush()
                return
            except ssl.SSLWantReadError:
                self._fill()

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            try:
                written = self._ssl.write(view[sent:])
            except ssl.SSLWantReadError:
                self._fill()
                continue
            sent += written
            self._flush()

    def recv(self, size: int) -> bytes:
        while True:
            try:
                data = self._ssl.read(size)
            except ssl.SSLWantReadError:
                self._fill()
                continue
            return data or b""

    def makefile(self, mode: str = "rb", buffering: int = -1, *args, **kwargs):
        if "b" not in mode:
            raise ValueError(mode)
        return io.BufferedReader(_Reader(self))

    def settimeout(self, timeout: Optional[float]) -> None:
        self._transport.settimeout(timeout)

    def close(self) -> None:
        try:
            self._transport.close()
        except OSError:
            pass

    def shutdown(self, how: int) -> None:
        try:
            self._transport.shutdown(how)
        except OSError:
            pass


def _open(url: str, timeout: float, proxy: str) -> Tuple[HTTPConnection, str]:
    """Return ``(connection, request-target)`` for one URL."""
    target = _target(url)
    path = _path_with_query(url)
    endpoint = parse_proxy(proxy)
    if endpoint is None:
        if target.scheme == "https":
            conn = _ReadyConnection(
                target.host, target.port,
                _tls(_tcp(target.host, target.port, timeout), target.host),
                timeout)
            return conn, path
        return HTTPConnection(target.host, target.port, timeout=timeout), path

    if endpoint.scheme == "socks5":
        try:
            sock = _tcp(endpoint.host, endpoint.port, timeout)
        except OSError as exc:
            raise ProxyError(
                f"无法连接 SOCKS5 {endpoint.host}:{endpoint.port} / "
                f"cannot connect: {exc}") from exc
        _socks5_connect(sock, target.host, target.port, endpoint.username, endpoint.password)
        if target.scheme == "https":
            sock = _tls(sock, target.host)
        return _ReadyConnection(target.host, target.port, sock, timeout), path

    # http or https proxy
    try:
        sock = _tcp(endpoint.host, endpoint.port, timeout)
    except OSError as exc:
        raise ProxyError(
            f"无法连接代理 {endpoint.host}:{endpoint.port} / "
            f"cannot connect: {exc}") from exc
    if endpoint.scheme == "https":
        sock = _tls(sock, endpoint.host)
    if target.scheme == "https":
        _http_connect(sock, target.host, target.port, endpoint.username, endpoint.password)
        if endpoint.scheme == "https":
            sock = _OverTLS(sock, target.host)
        else:
            sock = _tls(sock, target.host)
        return _ReadyConnection(target.host, target.port, sock, timeout), path

    conn = _HttpProxyConnection(endpoint.host, endpoint.port,
                                target.host, target.port, timeout)
    # The socket is opened lazily. Stash auth for the caller to add.
    conn.proxy_auth = _proxy_basic(endpoint.username, endpoint.password)  # type: ignore[attr-defined]
    absolute = f"http://{_format_host(target.host)}:{target.port}{path}"
    return conn, absolute


def _tcp(host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout)
    sock.settimeout(timeout)
    return sock


def _merge_headers(conn: HTTPConnection, headers: Optional[dict]) -> Dict[str, str]:
    merged = dict(headers or {})
    auth = getattr(conn, "proxy_auth", None) or {}
    for key, value in auth.items():
        merged.setdefault(key, value)
    return merged


def request(url: str, *, method: str = "GET", headers: Optional[dict] = None,
            body: Optional[bytes] = None, timeout: float = 30,
            proxy: str = "") -> HttpResult:
    """Blocking request → ``(status, headers, body)``."""
    conn, target = _open(url, timeout, proxy)
    try:
        conn.request(method, target, body=body, headers=_merge_headers(conn, headers))
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k: v for k, v in resp.getheaders()}
        return resp.status, hdrs, data
    finally:
        conn.close()


def open_stream(url: str, *, method: str = "POST", headers: Optional[dict] = None,
                body: Optional[bytes] = None, timeout: float = 1200,
                proxy: str = ""):
    """Open a connection without consuming the body → ``(conn, response)``.

    The caller owns both objects and must close the connection.
    """
    conn, target = _open(url, timeout, proxy)
    try:
        conn.request(method, target, body=body, headers=_merge_headers(conn, headers))
        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise
    return conn, resp


def json_body(obj) -> Tuple[bytes, Dict[str, str]]:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return payload, {"Content-Type": "application/json; charset=utf-8",
                     "Content-Length": str(len(payload))}
