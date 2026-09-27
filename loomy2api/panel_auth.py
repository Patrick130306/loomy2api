"""Panel login sessions.

The model API keeps using ``Authorization: Bearer`` / ``x-api-key``. The
control panel does not: when ``api_keys`` is set, ``/panel`` is a login form
and a successful login plants an HttpOnly cookie. The cookie is a random
session id, never the API key, and it is accepted only by ``/api/panel/*``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, Iterable, Tuple

__all__ = ["PanelSessions", "api_key_ok"]

COOKIE = "loomy_panel"


def api_key_ok(presented: str, keys: Iterable[str]) -> bool:
    """True when ``presented`` matches one configured key.

    Both sides are hashed first so the comparison is fixed-length. An empty
    key list never matches.
    """
    presented = presented or ""
    given = hashlib.sha256(presented.encode("utf-8")).digest()
    ok = False
    seen = False
    for key in keys:
        if not key:
            continue
        seen = True
        candidate = hashlib.sha256(str(key).encode("utf-8")).digest()
        ok = hmac.compare_digest(given, candidate) or ok
    return seen and ok


def set_cookie(token: str, *, secure: bool, max_age: int) -> str:
    parts = [
        f"{COOKIE}={token}",
        "HttpOnly",
        "Path=/",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie(*, secure: bool) -> str:
    parts = [
        f"{COOKIE}=",
        "HttpOnly",
        "Path=/",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class PanelSessions:
    """In-memory panel sessions for the single gateway process.

    A restart drops every session, which logs the panel out. That is
    deliberate: the session store is not written next to ``accounts.json``.
    """

    COOKIE = COOKIE
    TTL = 12 * 3600
    MAX = 64
    FAIL_LIMIT = 8
    LOCK_SECONDS = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, float] = {}
        self._fails: Dict[str, Tuple[int, float]] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._purge(now)
            if len(self._tokens) >= self.MAX:
                oldest = min(self._tokens, key=self._tokens.get)
                self._tokens.pop(oldest, None)
            self._tokens[token] = now + self.TTL
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        want = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            self._purge(now)
            matched = None
            for saved, expiry in self._tokens.items():
                have = hashlib.sha256(saved.encode("utf-8")).digest()
                if hmac.compare_digest(want, have) and expiry >= now:
                    matched = saved
                    break
            if matched is None:
                return False
            self._tokens[matched] = now + self.TTL
            return True

    def revoke(self, token: str) -> None:
        if not token:
            return
        want = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            for saved in list(self._tokens):
                have = hashlib.sha256(saved.encode("utf-8")).digest()
                if hmac.compare_digest(want, have):
                    self._tokens.pop(saved, None)
                    return

    def locked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            count, until = self._fails.get(ip, (0, 0.0))
            if until and until <= now:
                self._fails.pop(ip, None)
                return False
            return until > now

    def note_failure(self, ip: str) -> bool:
        """Record a bad password. Return True when ``ip`` is locked out."""
        now = time.time()
        with self._lock:
            count, until = self._fails.get(ip, (0, 0.0))
            if until > now:
                return True
            if until:
                count = 0
            count += 1
            locked = count >= self.FAIL_LIMIT
            self._fails[ip] = (count, now + self.LOCK_SECONDS if locked else 0.0)
            return locked

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def _purge(self, now: float) -> None:
        dead = [token for token, expiry in self._tokens.items() if expiry < now]
        for token in dead:
            self._tokens.pop(token, None)
