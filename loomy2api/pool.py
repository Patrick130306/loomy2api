"""Multi-account pool: session lifecycle, quota tracking, rotation.

Design notes
------------
* One account = one Loomy/iFlytek account = one 14-day session.  Sessions are
  refreshed automatically (password login) before they expire, so a pool keeps
  working unattended.
* Routing strategies:

  ``balance``      pick the account with the most available points (default)
  ``round_robin``  cycle in order
  ``lru``          least recently used

* An account that answers ``401/403`` or reports an exhausted quota is put in
  a cooldown and skipped until it expires; the gateway then retries the request
  on the next account.
* The pool state (sessions, quota) is written back to ``accounts.json``
  atomically.  Passwords live in the same file — keep it out of git
  (``.gitignore`` already covers it).
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import constants as C
from .account import Account, AccountClient, AccountError
from .upstream import ModelGateway, UpstreamError

__all__ = ["AccountPool", "PoolError"]


class PoolError(RuntimeError):
    pass


class AccountPool:
    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: None)
        self.client = AccountClient(cfg)
        self.gateway = ModelGateway(cfg)
        self._lock = threading.RLock()
        self._accounts: List[Account] = []
        self._rr_index = 0
        self._rr_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_save = 0.0
        self._dirty = False
        self._file = cfg.path("accounts_file", "accounts.json")
        self.load()

    # ------------------------------------------------------------ storage

    def load(self) -> None:
        raw: object = {}
        if self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
            except Exception as exc:                    # noqa: BLE001
                raise PoolError(f"{self._file} 解析失败 / parse error: {exc}") from exc

        items = raw.get("accounts") if isinstance(raw, dict) else raw
        accounts: List[Account] = []
        for idx, item in enumerate(items or []):
            if isinstance(item, dict):
                accounts.append(Account.from_dict(item, idx))

        if self.cfg.get("sessions_from_client", True):
            accounts.extend(self._client_accounts(existing=accounts))

        with self._lock:
            # keep runtime stats across reloads for same-named accounts
            previous = {a.name: a for a in self._accounts}
            for acc in accounts:
                old = previous.get(acc.name)
                if old is not None:
                    acc.requests, acc.points_used, acc.failures = (
                        old.requests, old.points_used, old.failures)
                    acc.last_used, acc.last_error = old.last_used, old.last_error
                    if acc.expire_at == 0 and old.expire_at:
                        acc.session, acc.expire_at, acc.userid = (
                            old.session, old.expire_at, old.userid)
            self._accounts = accounts

        self.log(f"账号池载入 {len(accounts)} 个账号"
                 + (f"（{', '.join(a.name for a in accounts)}）" if accounts else ""))

    def _client_accounts(self, existing: Sequence[Account]) -> List[Account]:
        """Import sessions from any installed desktop client (read-only).

        These are *derived* accounts: they are never written back to
        accounts.json (the client owns that file), and an import that collides
        with a configured account by session or by name is skipped.
        """
        pattern = os.path.join(str(self.cfg.get("client_root") or C.CLIENT_PUBLIC_ROOT),
                               "*", "userData", "auth-session.json")
        found: List[Account] = []
        known_sessions = {a.session for a in existing if a.session}
        known_names = {a.name for a in existing}
        # Same *account* reached through two different sessions (the client's
        # session vs. ours) must not become two pool entries: match on userid.
        known_users = {a.userid for a in existing if a.userid}
        for path in glob.glob(pattern):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:                           # noqa: BLE001
                continue
            session = str(data.get("session") or "")
            userid = str(data.get("userid") or "")
            if not session or session in known_sessions:
                continue
            if userid and userid in known_users:
                continue
            name = f"desktop-{str(data.get('phone') or 'unknown')[-4:]}"
            if name in known_names:
                continue
            created = int(data.get("updatedAt") or 0)
            # the client stores milliseconds since epoch; sessions last 14 days
            expire = (created // 1000 + C.SESSION_EXPIRE_SECONDS) if created else 0
            found.append(Account(
                name=name,
                loginid=str(data.get("phone") or ""),
                session=session, userid=userid,
                expire_at=expire, obtained_at=created // 1000, source="client",
                persist=False,
            ))
            known_sessions.add(session)
            known_names.add(name)
            if userid:
                known_users.add(userid)
            self.log(f"发现桌面客户端登录态：{path}")
        return found

    def save(self, *, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and (now - self._last_save) < 5:
                self._dirty = True
                return
            self._dirty = False
            self._last_save = now
            payload = {"accounts": [a.to_dict() for a in self._accounts if a.persist]}
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._file)

    def flush(self) -> None:
        """Force a write if a throttled save is pending."""
        if self._dirty:
            self.save(force=True)

    # ------------------------------------------------------------ access

    @property
    def accounts(self) -> List[Account]:
        with self._lock:
            return list(self._accounts)

    def get(self, name: str) -> Optional[Account]:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None

    def add_account(self, name: str, loginid: str = "", password: str = "",
                    session: str = "", userid: str = "") -> Account:
        if self.get(name):
            raise PoolError(f"账号 {name} 已存在 / account already exists")
        acc = Account(name=name, loginid=loginid, password=password,
                      session=session, userid=userid)
        if session and not acc.expire_at:
            acc.obtained_at = int(time.time())
            acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
        with self._lock:
            self._accounts.append(acc)
        self.save()
        return acc

    def remove_account(self, name: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.name != name]
            removed = len(self._accounts) != before
        if removed:
            self.save()
        return removed

    # ------------------------------------------------------ session keep

    def ensure_session(self, acc: Account, *, force: bool = False) -> Account:
        """Log in / refresh the account session when needed."""
        renew_days = float(self.cfg.get("session_renew_before_days") or 3)
        needs = force or not acc.session_valid
        if not needs and acc.days_left is not None and acc.days_left < renew_days:
            needs = True
            self.log(f"账号 {acc.name} session 剩余 {acc.days_left:.1f} 天，提前续期")
        if not needs:
            return acc
        if not (acc.loginid and acc.password):
            raise PoolError(
                f"账号 {acc.name} 的 session 不可用且没有账号密码，无法自动重登"
                f"（请在 accounts.json 补 loginid/password，或用 `loomy2api login` 短信登录）")
        self.client.fill(acc)
        self.save()
        self.log(f"账号 {acc.name} 登录成功 userid={acc.userid} "
                 f"session={acc.session[:8]}… 剩余 {(acc.days_left or 0):.1f} 天")
        return acc

    def refresh_quota(self, acc: Account) -> Account:
        try:
            quota = self.gateway.quota(acc.session)
        except UpstreamError as exc:
            if exc.status in (401, 403):
                acc.session, acc.expire_at = "", 0
                self.log(f"账号 {acc.name} session 被上游拒绝（HTTP {exc.status}）")
            else:
                self.log(f"账号 {acc.name} 额度查询失败：{exc}")
            return acc
        acc.balance = quota.get("balance")
        acc.daily_balance = quota.get("daily_balance")
        acc.available = quota.get("available")
        acc.quota_updated_at = int(time.time())
        return acc

    # ------------------------------------------------------------ routing

    def usable(self, exclude: Sequence[str] = ()) -> List[Account]:
        now = time.time()
        return [a for a in self.accounts
                if a.enabled and not a.in_cooldown and a.name not in exclude
                and a.session_valid and a.available != 0]

    def acquire(self, exclude: Sequence[str] = ()) -> Account:
        """Return the next account to use (session guaranteed valid)."""
        candidates = self.usable(exclude)
        if not candidates:
            # nothing healthy: give single-account setups a chance to self-heal
            all_enabled = [a for a in self.accounts if a.enabled and a.name not in exclude]
            if not all_enabled:
                raise PoolError("账号池里没有可用账号（accounts.json 为空或全部禁用）")
            candidates = all_enabled
            self.log("没有健康账号，尝试对现有账号续期/重登")

        strategy = str(self.cfg.get("strategy") or "balance").lower()
        if strategy == "round_robin":
            with self._rr_lock:
                acc = candidates[self._rr_index % len(candidates)]
                self._rr_index = (self._rr_index + 1) % max(1, len(candidates))
        elif strategy == "lru":
            acc = min(candidates, key=lambda a: a.last_used or 0)
        else:  # balance
            acc = max(candidates, key=lambda a: (a.available if a.available is not None else 10**9,
                                                 -a.last_used))

        self.ensure_session(acc)
        acc.last_used = time.time()
        return acc

    def report_success(self, acc: Account, usage: Optional[Dict] = None) -> None:
        acc.requests += 1
        points = (usage or {}).get("points_consumed")
        if isinstance(points, (int, float)):
            acc.points_used += int(points)
            if acc.available is not None:
                acc.available = max(0, int(acc.available) - int(points))
        acc.last_error = ""
        self.save()

    def report_failure(self, acc: Account, status: int, reason: str = "") -> None:
        acc.failures += 1
        acc.last_error = reason or f"HTTP {status}"
        if status in (401, 403):
            acc.session, acc.expire_at = "", 0
        cooldown = float(self.cfg.get("cooldown_seconds") or 300)
        acc.cooldown(cooldown, acc.last_error)
        self.log(f"账号 {acc.name} 失败 HTTP {status} → 冷却 {cooldown:.0f}s（{acc.last_error}）")
        self.save()

    def snapshot(self) -> Dict:
        return {
            "strategy": self.cfg.get("strategy"),
            "count": len(self.accounts),
            "usable": len(self.usable()),
            "accounts": [a.public_dict() for a in self.accounts],
        }

    # --------------------------------------------------------- background

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="account-keeper",
                                        daemon=True)
        self._thread.start()
        self.log("账号守护线程已启动（续期 + 额度刷新）")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(60, int(float(self.cfg.get("quota_refresh_minutes") or 30) * 60))
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            try:
                self.tick()
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号守护线程异常：{exc}")

    def tick(self) -> None:
        """One maintenance pass: renew sessions, refresh quota."""
        for acc in self.accounts:
            if not acc.enabled:
                continue
            try:
                self.ensure_session(acc)
                self.refresh_quota(acc)
            except (AccountError, PoolError) as exc:
                self.log(f"账号 {acc.name} 维护失败：{exc}")
            except Exception as exc:                    # noqa: BLE001
                self.log(f"账号 {acc.name} 维护异常：{exc}")
        self.save()

    def bootstrap(self) -> None:
        """Login/Fill any account that has no usable session."""
        for acc in self.accounts:
            if acc.enabled and not acc.session_valid:
                try:
                    self.ensure_session(acc)
                except Exception as exc:                # noqa: BLE001
                    self.log(f"账号 {acc.name} 启动登录失败：{exc}")
        self.save()
