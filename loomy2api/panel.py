"""Web control panel: HTML page + JSON API.

The page is a single self-contained HTML file (no CDN, no build step) served at
``/panel``; the JSON API lives under ``/api/panel/*`` and reuses the gateway's
API-key gate when ``api_keys`` is configured.

Endpoints
---------
``GET  /panel``                       the page
``GET  /api/panel/state``             accounts + quota + totals (``?refresh=1``
                                      forces a quota refresh)
``POST /api/panel/refresh``           refresh every account's quota
``POST /api/panel/accounts``          add an account (optionally log in now)
``POST /api/panel/accounts/update``   patch loginid / password / enabled
``POST /api/panel/accounts/remove``   delete an account
``POST /api/panel/accounts/renew``    force re-login + quota refresh
``POST /api/panel/accounts/identity`` inspect / regenerate the account identity
``POST /api/panel/sms/send``          SMS login step 1: send the code → msgid
``POST /api/panel/sms/verify``        SMS login step 2: code → session → account
``GET  /api/panel/logs``              tail of the gateway log
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .account import Account, AccountError
from .constants import SESSION_EXPIRE_SECONDS
from .pool import PoolError

__all__ = ["Panel"]

WEB_DIR = Path(__file__).resolve().parent / "web"
PANEL_HTML = WEB_DIR / "index.html"

#: Hidden phone number for the panel: 138****0000
def mask_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 7:
        return str(value or "")
    return f"{digits[:3]}****{digits[-4:]}"


class Panel:
    """Stateful facade over the account pool for the web UI."""

    def __init__(self, gateway):
        self.gw = gateway
        self.log = gateway.log

    # ------------------------------------------------------------ helpers

    @property
    def pool(self):
        return self.gw.pool

    def html(self) -> bytes:
        try:
            return PANEL_HTML.read_bytes()
        except OSError:                                  # pragma: no cover
            return b"<h1>loomy2api</h1><p>panel asset missing</p>"

    def _account_view(self, acc: Account) -> Dict[str, Any]:
        view = acc.public_dict()
        view["loginid_masked"] = mask_phone(acc.loginid)
        view["points_used"] = acc.points_used
        view["mode"] = "password" if (acc.loginid and acc.password) else (
            "session" if acc.session else "empty")
        return view

    def _quota_stale(self, acc: Account) -> bool:
        minutes = float(self.gw.cfg.get("quota_refresh_minutes") or 30)
        return (time.time() - (acc.quota_updated_at or 0)) > minutes * 60

    def _refresh_all(self, only_stale: bool = True) -> None:
        for acc in self.pool.accounts:
            if not acc.session_valid:
                continue
            if only_stale and not self._quota_stale(acc):
                continue
            try:
                self.pool.refresh_quota(acc)
            except Exception as exc:                     # noqa: BLE001
                self.log(f"[panel] 刷新 {acc.name} 额度失败：{exc}")
        self.pool.save()

    # --------------------------------------------------------------- state

    def state(self, *, refresh: bool = False) -> Dict[str, Any]:
        self._refresh_all(only_stale=not refresh)
        accounts = [self._account_view(a) for a in self.pool.accounts]

        # one account can hold two sessions (ours + the client's) — count once
        seen: Dict[str, int] = {}
        for acc in self.pool.accounts:
            if isinstance(acc.available, int):
                key = acc.userid or acc.name
                seen[key] = max(seen.get(key, 0), acc.available)

        return {
            "ok": True,
            "now": int(time.time()),
            "totals": {
                "accounts": len(accounts),
                "usable": len(self.pool.usable()),
                "available": sum(seen.values()),
                "unique_accounts": len(seen),
                "requests": sum(a["requests"] for a in accounts),
                "points_used": sum(a["points_used"] for a in accounts),
            },
            "config": {
                "upstream": self.gw.cfg["upstream"],
                "strategy": self.gw.cfg.get("strategy"),
                "default_model": self.gw.cfg.get("default_model"),
                "identity_mode": self.gw.cfg.get("identity_mode"),
                "models": len(self.gw.catalogue()),
                "auth_required": bool(self.gw.cfg.api_keys),
                "quota_refresh_minutes": self.gw.cfg.get("quota_refresh_minutes"),
            },
            "accounts": accounts,
        }

    # --------------------------------------------------------------- writes

    def refresh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_all(only_stale=False)
        return self.state()

    def add_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        loginid = str(payload.get("loginid") or "").strip()
        password = str(payload.get("password") or "")
        session = str(payload.get("session") or "").strip()
        if not name:
            raise PoolError("账号名不能为空 / name is required")
        if not loginid and not session:
            raise PoolError("需要手机号和密码，或直接提供一个 session / "
                            "provide phone+password, or a session")
        if loginid and not password and not session:
            raise PoolError("只给手机号时还需要密码 / password required with phone")

        acc = self.pool.add_account(name, loginid=loginid, password=password,
                                    session=session)
        result: Dict[str, Any] = {"ok": True, "name": acc.name,
                                  "identity": acc.identity_view()}
        if loginid and password and payload.get("login", True):
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
                result["logged_in"] = True
                result["userid"] = acc.userid
            except (AccountError, PoolError) as exc:
                result["logged_in"] = False
                result["error"] = str(exc)
            self.pool.save(force=True)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def update_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        fields: Dict[str, Any] = {}
        if "loginid" in payload:
            fields["loginid"] = payload["loginid"]
        if payload.get("password"):
            fields["password"] = payload["password"]
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        acc = self.pool.update_account(name, **fields)
        if payload.get("login") and acc.loginid and acc.password:
            try:
                self.pool.ensure_session(acc, force=True)
                self.pool.refresh_quota(acc)
            except (AccountError, PoolError) as exc:
                self.log(f"[panel] {name} 重新登录失败：{exc}")
            self.pool.save(force=True)
        return {"ok": True, "name": acc.name, "state": self.state()}

    def remove_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        removed = self.pool.remove_account(name)
        if not removed:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        self.log(f"[panel] 已删除账号 {name}")
        return {"ok": True, "removed": name, "state": self.state()}

    def renew_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        result: Dict[str, Any] = {"ok": True, "name": name}
        try:
            self.pool.ensure_session(acc, force=True)
            self.pool.refresh_quota(acc)
            acc.cooldown_until = 0.0
            result["userid"] = acc.userid
            result["session_days_left"] = acc.days_left
        except (AccountError, PoolError) as exc:
            result["ok"] = False
            result["error"] = str(exc)
        self.pool.save(force=True)
        result["state"] = self.state()
        return result

    def identity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Inspect (``regenerate: false``) or rebind (``regenerate: true``)."""
        name = str(payload.get("name") or "").strip()
        acc = self.pool.get(name)
        if acc is None:
            raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        if payload.get("regenerate"):
            self.pool.rebind_identity(name)
            self.log(f"[panel] {name} 已重新绑定设备标识 "
                     f"devid={acc.identity.get('devid')}")
        elif not acc.identity:
            self.pool.client.ensure_identity(acc)
            self.pool.save(force=True)
        return {"ok": True, "name": name, "identity": acc.identity_view(),
                "state": self.state()}

    def send_sms(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """SMS login step 1 — send the code, hand the ``msgid`` back to the page.

        The panel keeps the msgid in the browser (it is only valid for 5 minutes)
        so step 2 can be a separate click without any server-side session state.
        """
        phone = str(payload.get("loginid") or payload.get("phone") or "").strip()
        if not phone:
            raise PoolError("需要手机号 / phone is required")
        try:
            data = self.pool.client.send_sms_code(phone)
        except AccountError as exc:
            raise PoolError(f"验证码发送失败：{exc}") from exc
        record = data.get("data") if isinstance(data.get("data"), dict) else data
        msgid = str((record or {}).get("msgid") or "").strip()
        if not msgid:
            # Never hand back an empty msgid: step 2 would fail with a message
            # that has nothing to do with the real cause.
            raise PoolError("短信响应缺少 msgid（请稍后重试）")
        self.log(f"[panel] 已向 {mask_phone(phone)} 下发短信验证码")
        return {"ok": True, "phone": phone, "msgid": msgid}

    def verify_sms(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """SMS login step 2 — exchange the code for a session and store the account.

        Creates the account when the name is new, otherwise refreshes the
        existing one (so "re-login by SMS" works for an expired account too).
        """
        name = str(payload.get("name") or "").strip() or "main"
        phone = str(payload.get("loginid") or payload.get("phone") or "").strip()
        code = str(payload.get("code") or "").strip()
        msgid = str(payload.get("msgid") or "").strip()
        if not phone or not code or not msgid:
            raise PoolError("手机号 / 验证码 / msgid 都不能为空 / "
                            "phone, code and msgid are all required")
        try:
            got = self.pool.client.login_by_sms(phone, code, msgid)
        except AccountError as exc:
            raise PoolError(f"短信登录失败：{exc}") from exc

        acc = self.pool.get(name)
        created = acc is None
        if acc is None:
            acc = self.pool.add_account(name, loginid=phone)
        acc.session = got["session"]
        acc.userid = got.get("userid", "") or acc.userid
        if phone:
            acc.loginid = phone
        acc.obtained_at = int(time.time())
        acc.expire_at = acc.obtained_at + SESSION_EXPIRE_SECONDS
        acc.cooldown_until = 0.0
        self.pool.refresh_quota(acc)
        self.pool.save(force=True)
        self.log(f"[panel] {name} 短信登录成功 userid={acc.userid} "
                 f"session={acc.session[:8]}…{'（新建）' if created else '（续期）'}")
        return {"ok": True, "name": name, "created": created,
                "userid": acc.userid, "days_left": acc.days_left,
                "state": self.state()}

    def onboarding(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Claim the eight newbie tasks for one account (or all of them).

        Pure API claim — no model tokens are spent (see ``onboarding.py``).
        """
        from .onboarding import claim_for_account

        name = str(payload.get("name") or "").strip()
        if name:
            targets = [self.pool.get(name)]
            if targets[0] is None:
                raise PoolError(f"没有名为 {name} 的账号 / no such account: {name}")
        else:
            targets = list(self.pool.accounts)
        if not targets:
            raise PoolError("账号池为空 / account pool is empty")

        results = [claim_for_account(self.pool, acc, log=self.log) for acc in targets]
        self.pool.save(force=True)
        return {
            "ok": all(r.get("ok") for r in results),
            "results": results,
            "claimed_points": sum(r.get("claimed_points", 0) for r in results),
            "state": self.state(),
        }

    def logs(self, lines: int = 200) -> Dict[str, Any]:
        path = self.gw.log.path
        lines = max(10, min(int(lines or 200), 2000))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"ok": True, "path": str(path), "lines": content[-lines:]}
