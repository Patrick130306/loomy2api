"""Web control panel: HTML page + JSON API.

The page is a single self-contained HTML file (no CDN, no build step) served at
``/panel``. When ``api_keys`` is set, anonymous visitors get ``login.html``
instead, and the JSON API accepts either the gateway key or the panel session
cookie.

Endpoints
---------
``GET  /panel``                       the page, or the login form
``POST /api/panel/login``             trade an API key for a session cookie
``POST /api/panel/logout``            drop that cookie
``GET  /api/panel/session``           ``{auth_required, authenticated}`` only
``GET  /api/panel/state``             accounts + quota + totals (``?refresh=1``
                                      forces a quota refresh)
``POST /api/panel/refresh``           refresh every account's quota
``POST /api/panel/proxy``             set the upstream proxy (socks5/http/https)
``POST /api/panel/accounts``          add an account (optionally log in now)
``POST /api/panel/accounts/update``   patch loginid / password / enabled
``POST /api/panel/accounts/remove``   delete an account
``POST /api/panel/accounts/renew``    force re-login + quota refresh
``POST /api/panel/accounts/identity`` inspect / regenerate the account identity
``GET  /api/panel/logs``              tail of the gateway log
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .account import Account, AccountError
from .httpc import compose_proxy, normalize_proxy, proxy_view
from .pool import PoolError

__all__ = ["Panel"]

WEB_DIR = Path(__file__).resolve().parent / "web"
PANEL_HTML = WEB_DIR / "index.html"
LOGIN_HTML = WEB_DIR / "login.html"

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

    def login_html(self) -> bytes:
        try:
            return LOGIN_HTML.read_bytes()
        except OSError:                                  # pragma: no cover
            return b"<h1>loomy2api</h1><p>login asset missing</p>"

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
                "proxy": proxy_view(str(self.gw.cfg.get("proxy") or "")),
                "proxy_from_env": bool(getattr(self.gw.cfg, "proxy_from_env", False)),
            },
            "accounts": accounts,
        }

    # --------------------------------------------------------------- writes

    def refresh(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_all(only_stale=False)
        return self.state()

    def set_proxy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Point upstream traffic at a SOCKS5, HTTP, or HTTPS proxy.

        Takes either ``{"proxy": "socks5://host:1080"}`` or the panel fields
        ``scheme`` / ``host`` / ``port`` / ``username`` / ``password``.
        ``scheme: "direct"`` clears it. A missing password keeps the previous
        one. The value is applied immediately and written back to config.json.
        """
        current = str(self.gw.cfg.get("proxy") or "")
        if payload.get("scheme") or "host" in payload:
            url = compose_proxy(
                scheme=str(payload.get("scheme") or "direct"),
                host=str(payload.get("host") or ""),
                port=payload.get("port"),
                username=str(payload.get("username") or ""),
                password=payload.get("password") if "password" in payload else None,
                previous=current,
            )
        elif "proxy" in payload:
            url = normalize_proxy(str(payload.get("proxy") or ""))
        else:
            raise ValueError("缺少代理配置 / proxy settings are required")
        self.gw.cfg["proxy"] = url
        self._persist_proxy(url)
        shown = proxy_view(url)["masked"] or "直连"
        self.log(f"出口代理已更新：{shown}")
        return {"ok": True, "proxy": proxy_view(url), "state": self.state()}

    def _persist_proxy(self, url: str) -> None:
        path = getattr(self.gw.cfg, "config_path", None)
        if not path:
            return
        path = Path(path)
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:                     # noqa: BLE001
                raise ValueError(f"config.json 无法读取 / cannot read config.json: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError("config.json 不是对象 / config.json is not an object")
            data = loaded
        data["proxy"] = url
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

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

    def logs(self, lines: int = 200) -> Dict[str, Any]:
        path = self.gw.log.path
        lines = max(10, min(int(lines or 200), 2000))
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"ok": True, "path": str(path), "lines": content[-lines:]}
