"""Account model + iFlytek account-service client.

One :class:`Account` is one Loomy/iFlytek account: phone number, password,
and the 14-day session that the account service hands out.  The session is
exactly what the model gateway wants as a Bearer token, so "logging in
server-side" is all that stands between us and a usable API key.

Login paths (both fully server-side, no desktop client involved):

* password — ``/login/account/getPuKey`` returns a 1024-bit RSA public key
  **and** an ``rcode``; the password is RSA/PKCS#1 v1.5 encrypted and posted to
  ``/login/account/byPwd``.  No captcha is involved: ``rcode`` is a server
  nonce, so this is fully automatable.
* SMS — ``/login/phone/sendMsgCode`` → ``msgid``, then
  ``/login/phone/checkCode``.  Needs a human to read the code once.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from .crypto import rsa_encrypt
from .httpc import request as http_request
from .signer import build_headers

__all__ = ["Account", "AccountClient", "AccountError"]


class AccountError(RuntimeError):
    """Raised for account-service business errors."""

    def __init__(self, message: str, code: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class Account:
    """A single account plus its cached session/ quota state."""

    name: str
    loginid: str = ""            # phone number used to log in
    password: str = ""
    enabled: bool = True
    persist: bool = True         # False for accounts derived from the client

    session: str = ""
    userid: str = ""
    expire_at: int = 0           # unix seconds
    obtained_at: int = 0

    # quota cache (filled from /points/records)
    balance: Optional[int] = None
    daily_balance: Optional[int] = None
    available: Optional[int] = None
    multiplier: float = 0.0      # not used for routing, informational
    quota_updated_at: int = 0

    # runtime state (never persisted)
    cooldown_until: float = 0.0
    last_used: float = 0.0
    requests: int = 0
    points_used: int = 0
    failures: int = 0
    last_error: str = ""
    source: str = "config"       # config | client

    # ------------------------------------------------------------------
    @property
    def session_valid(self) -> bool:
        return bool(self.session) and (self.expire_at == 0 or self.expire_at > time.time())

    @property
    def days_left(self) -> Optional[float]:
        if not self.expire_at:
            return None
        return (self.expire_at - time.time()) / 86400

    @property
    def in_cooldown(self) -> bool:
        return self.cooldown_until > time.time()

    def cooldown(self, seconds: float, reason: str = "") -> None:
        self.cooldown_until = time.time() + max(1.0, float(seconds))
        self.failures += 1
        if reason:
            self.last_error = reason

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Dict[str, Any], index: int = 0) -> "Account":
        name = str(raw.get("name") or raw.get("loginid") or f"account{index + 1}")
        return cls(
            name=name,
            loginid=str(raw.get("loginid") or raw.get("phone") or ""),
            password=str(raw.get("password") or ""),
            enabled=raw.get("enabled", True) is not False,
            session=str(raw.get("session") or ""),
            userid=str(raw.get("userid") or ""),
            expire_at=int(raw.get("expireAt") or raw.get("expire_at") or 0),
            obtained_at=int(raw.get("obtainedAt") or raw.get("obtained_at") or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.loginid:
            out["loginid"] = self.loginid
        if self.password:
            out["password"] = self.password
        if self.session:
            out["session"] = self.session
        if self.userid:
            out["userid"] = self.userid
        if self.expire_at:
            out["expireAt"] = self.expire_at
        if self.obtained_at:
            out["obtainedAt"] = self.obtained_at
        return out

    def public_dict(self) -> Dict[str, Any]:
        """Status view without secrets (used by /admin/accounts)."""
        out = {
            "name": self.name,
            "userid": self.userid,
            "enabled": self.enabled,
            "has_password": bool(self.password),
            "session": (self.session[:8] + "…") if self.session else "",
            "session_days_left": (round(self.days_left, 2) if self.days_left is not None else None),
            "balance": self.balance,
            "daily_balance": self.daily_balance,
            "available": self.available,
            "quota_updated_at": self.quota_updated_at,
            "requests": self.requests,
            "points_used": self.points_used,
            "failures": self.failures,
            "in_cooldown": self.in_cooldown,
            "cooldown_seconds_left": (round(self.cooldown_until - time.time(), 1)
                                      if self.in_cooldown else 0),
            "source": self.source,
            "last_error": self.last_error,
        }
        return out


class AccountClient:
    """Talks to ``account_base`` (login + userinfo) on behalf of an account."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- plumbing -------------------------------------------------------

    def _base(self) -> Dict[str, str]:
        return {
            "appid": self.cfg.get("account_appid") or C.DEFAULT_ACCOUNT_APPID,
            "modelid": C.WEB_MODEL_ID,
            "version": C.CLIENT_VERSION,
            "devid": C.DEVICE_ID,
            "ua": C.CLIENT_UA,
            "traceid": uuid.uuid4().hex,
        }

    def call(self, path: str, body: Optional[dict] = None,
             *, timeout: float = 30) -> Dict[str, Any]:
        body_str = json.dumps(body, ensure_ascii=False) if body else ""
        headers = build_headers(
            self.cfg.ak_id, self.cfg.ak_secret,
            method="POST", path=path, body=body_str,
        )
        url = f"{str(self.cfg['account_base']).rstrip('/')}{path}"
        try:
            status, _hdrs, data = http_request(
                url, method="POST", headers=headers,
                body=body_str.encode("utf-8") if body_str else None,
                timeout=timeout, proxy=str(self.cfg.get("proxy") or ""),
            )
        except Exception as exc:                        # noqa: BLE001
            raise AccountError(f"account service unreachable: {exc}",
                               code="NETWORK", retryable=True) from exc

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:                               # noqa: BLE001
            raise AccountError(
                f"account service returned HTTP {status}: "
                f"{data[:200].decode('utf-8', 'replace')}", code=f"HTTP_{status}",
                retryable=status >= 500)

        code = str(payload.get("code") or payload.get("errorCode") or "")
        if code and code != "000000":
            msg = payload.get("desc") or payload.get("message") or code
            raise AccountError(f"{path} failed: {msg}", code=code,
                               retryable=code in ("100001", "100002"))
        if status >= 400 and "message" in payload:
            raise AccountError(f"{path} HTTP {status}: {payload['message']}",
                               code=f"HTTP_{status}", retryable=status >= 500)
        return payload

    # -- login ----------------------------------------------------------

    def get_public_key(self) -> Tuple[str, str]:
        """→ ``(pukey_b64, rcode)``.  Zero side effects: a good first probe."""
        payload = self.call("/login/account/getPuKey", {"base": self._base()})
        data = payload.get("data") or {}
        pukey, rcode = data.get("pukey") or "", data.get("rcode") or ""
        if not pukey:
            raise AccountError("getPuKey returned no public key", code="NO_PUKEY")
        return pukey, rcode

    def login_by_password(self, loginid: str, password: str) -> Dict[str, str]:
        """Full password login → ``{session, userid, phone}``."""
        pukey, rcode = self.get_public_key()
        encrypted = rsa_encrypt(pukey, password)
        payload = self.call("/login/account/byPwd", {
            "base": self._base(),
            "param": {
                "loginid": loginid,
                "password": encrypted,
                "rcode": rcode,
                "type": 1,
                "expire": C.SESSION_EXPIRE_SECONDS,
            },
        })
        return _extract_session(payload)

    def send_sms_code(self, phone: str) -> Dict[str, Any]:
        return self.call("/login/phone/sendMsgCode", {
            "base": self._base(),
            "param": {"ccode": "86", "phone": phone, "expire": 300},
        })

    def login_by_sms(self, phone: str, code: str, msgid: str) -> Dict[str, str]:
        payload = self.call("/login/phone/checkCode", {
            "base": self._base(),
            "param": {"ccode": "86", "phone": phone, "mcode": code, "msgid": msgid,
                      "expire": C.SESSION_EXPIRE_SECONDS},
        })
        return _extract_session(payload)

    def query_base_info(self, session: str) -> Dict[str, Any]:
        return self.call("/userinfo/query/baseInfo",
                         {"base": self._base(), "param": {"session": session}})

    def logout(self, session: str) -> Dict[str, Any]:
        return self.call("/login/account/logout",
                         {"base": self._base(), "param": {"session": session}})

    # -- convenience ----------------------------------------------------

    def fill(self, account: Account) -> Account:
        """Log the account in (password path) and refresh its session fields."""
        if not account.loginid or not account.password:
            raise AccountError(
                f"account {account.name}: loginid/password missing",
                code="NO_CREDENTIALS")
        got = self.login_by_password(account.loginid, account.password)
        account.session = got["session"]
        account.userid = got.get("userid", "")
        account.obtained_at = int(time.time())
        account.expire_at = account.obtained_at + C.SESSION_EXPIRE_SECONDS
        return account


def _extract_session(payload: Dict[str, Any]) -> Dict[str, str]:
    """Pull session/userid/phone out of whatever shape the server returns."""
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(node, dict):
        raise AccountError(f"unexpected login payload: {payload}")
    session = str(node.get("session") or "")
    if not session:
        raise AccountError(f"login succeeded but returned no session: {payload}")
    userid = str(node.get("userid") or node.get("userId") or "")
    phone = str(node.get("phone") or node.get("loginid") or "")
    return {"session": session, "userid": userid, "phone": phone}
