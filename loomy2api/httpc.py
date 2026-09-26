"""Thin HTTP helpers (stdlib only) with explicit proxy support.

``urllib`` reads ``http_proxy`` / ``https_proxy`` implicitly, which is usually
the opposite of what you want here: the upstreams are domestic Chinese
endpoints, so the default is *direct* and a proxy must be configured on
purpose.
"""

from __future__ import annotations

import json
import ssl
from http.client import HTTPConnection, HTTPSConnection
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

__all__ = ["request", "open_stream", "HttpResult"]

HttpResult = Tuple[int, Dict[str, str], bytes]


def _split(url: str):
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {url}")
    return u


def _connect(url: str, timeout: float, proxy: str = "") -> HTTPConnection:
    """Open a connection, honouring ``proxy`` (``http://host:port``)."""
    u = _split(url)
    if proxy:
        p = _split(proxy)
        if u.scheme == "https":
            # CONNECT tunnel
            conn = HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                   context=ssl.create_default_context())
            conn.set_tunnel(u.hostname, u.port or 443,
                            headers={"Proxy-Connection": "keep-alive"})
            return conn
        return HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    if u.scheme == "https":
        return HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                               context=ssl.create_default_context())
    return HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def _path_with_query(url: str) -> str:
    u = _split(url)
    return u.path + (f"?{u.query}" if u.query else "")


def _proxy_scheme_ok(url: str, proxy: str) -> bool:  # pragma: no cover - helper
    return bool(proxy)


def request(url: str, *, method: str = "GET", headers: Optional[dict] = None,
            body: Optional[bytes] = None, timeout: float = 30,
            proxy: str = "") -> HttpResult:
    """Blocking request → ``(status, headers, body)``."""
    conn = _connect(url, timeout, proxy)
    try:
        conn.request(method, _path_with_query(url), body=body,
                     headers=headers or {})
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
    conn = _connect(url, timeout, proxy)
    try:
        conn.request(method, _path_with_query(url), body=body,
                     headers=headers or {})
        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise
    return conn, resp


def json_body(obj) -> Tuple[bytes, Dict[str, str]]:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return payload, {"Content-Type": "application/json; charset=utf-8",
                     "Content-Length": str(len(payload))}
