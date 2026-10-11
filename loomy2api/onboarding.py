"""Loomy onboarding tasks — 10 000 points, claimable purely through the API.

Ported from ``dsh-codearts-auth``'s ``src/loomy-onboarding.ts``.

The upstream does **not** verify the prerequisite behaviour.  In the official
Electron client the completion conditions are evaluated locally
(``onboarding-service.js``), and the server only does "idempotent set + add
points".  Measured 2026-09-26: eight bare ``POST /complete`` calls took a fresh
account from 0 to 10 000 points without sending a single message, generating a
PPT or installing a skill.  So this implementation is a **pure API claim** and
spends zero model tokens.

Endpoints (model gateway ``loomyad.xunfei.cn/api/v1``)::

    GET  /onboarding/tasks            → {tasks: {key: bool}, earned, total}
    POST /onboarding/tasks/complete   body {key} → {alreadyCompleted, balance}

Three rules carried over verbatim from the reference implementation:

* idempotency is judged by the response body's ``alreadyCompleted`` — **not**
  the HTTP status, **not** ``code`` (already-completed replays count as wins);
* the server's ``earned`` is **not** trusted — points are recomputed locally
  from :data:`TASK_POINTS` (the client does the same);
* the report body carries **only** ``key`` — no device fingerprint, no version,
  no channel.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "AUTH_ERROR_CODE",
    "BAD_REQUEST_CODE",
    "OK_CODE",
    "ONBOARDING_TOTAL",
    "TASK_POINTS",
    "TASK_TITLES",
    "OnboardingError",
    "claim_all",
    "claim_for_account",
    "complete_task",
    "compute_earned",
    "fetch_tasks",
    "normalize_tasks",
]

OK_CODE = "000000"
BAD_REQUEST_CODE = "100001"
#: session expired / not logged in — must stop the whole batch, not retry.
AUTH_ERROR_CODE = "100002"

#: task key → points.  Sum is exactly 10 000; the order is the official order.
TASK_POINTS: Dict[str, int] = {
    "first_message": 500,
    "pick_skill": 1000,
    "generate_ppt": 1500,
    "set_schedule": 1000,
    "install_skill": 1500,
    "configure_remote": 1000,
    "create_soul": 1500,
    "share_soul": 2000,
}

#: task key → Chinese title (the client's registry labels).
TASK_TITLES: Dict[str, str] = {
    "first_message": "发送你的第一条消息",
    "pick_skill": "试试选择一个技能",
    "generate_ppt": "生成第一份 PPT",
    "set_schedule": "设置定时任务",
    "install_skill": "在技能广场安装一个技能",
    "configure_remote": "配置远程控制",
    "create_soul": "创建你的第一个搭子",
    "share_soul": "把搭子分享给朋友",
}

ONBOARDING_TOTAL = 10_000


class OnboardingError(Exception):
    """Business-level failure (Loomy always answers HTTP 200, code in the body)."""

    def __init__(self, message: str, code: str = "", *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    @property
    def auth_failed(self) -> bool:
        return self.code == AUTH_ERROR_CODE


# ---------------------------------------------------------------- helpers


def compute_earned(tasks: Dict[str, bool]) -> int:
    """Points already claimed, computed **locally** (unknown keys ignored)."""
    return sum(points for key, points in TASK_POINTS.items() if tasks.get(key) is True)


def normalize_tasks(raw: Any) -> Dict[str, bool]:
    """Keep the eight known keys, coerce everything to bool, fill missing with False."""
    source = raw if isinstance(raw, dict) else {}
    return {key: source.get(key) is True for key in TASK_POINTS}


def _request(gateway, session: str, path: str, payload: Optional[Dict[str, Any]] = None,
             method: str = "POST") -> Any:
    """One business call; unwraps ``{code, desc, data}``.

    ``AUTH_ERROR_CODE`` is raised as-is so the caller can abort the batch
    instead of firing a string of doomed requests.
    """
    try:
        status, _headers, body = gateway.call(session, path, payload or {}, method=method)
    except Exception as exc:                            # noqa: BLE001
        raise OnboardingError(f"{path} 请求失败：{exc}", code="NETWORK",
                              retryable=True) from exc

    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as exc:                            # noqa: BLE001
        raise OnboardingError(
            f"{path} 响应不是 JSON（HTTP {status}）："
            f"{body[:200].decode('utf-8', 'replace')}", code=f"HTTP_{status}",
            retryable=status >= 500) from exc

    if not isinstance(parsed, dict):
        return parsed

    code = str(parsed.get("code") or parsed.get("errorCode") or "")
    if code and code != OK_CODE:
        message = parsed.get("desc") or parsed.get("message") or code
        raise OnboardingError(f"{path} 失败：{message}", code=code,
                              retryable=code in (BAD_REQUEST_CODE, AUTH_ERROR_CODE))
    return parsed.get("data")


# ---------------------------------------------------------------- API


def fetch_tasks(gateway, session: str) -> Dict[str, Any]:
    """Query onboarding state.

    ``earned`` is recomputed locally — the server's value is only a hint and has
    historically disagreed with the client's table.
    """
    data = _request(gateway, session, "onboarding/tasks", method="GET")
    record = data if isinstance(data, dict) else {}
    tasks = normalize_tasks(record.get("tasks"))
    total = record.get("total")
    return {
        "tasks": tasks,
        "earned": compute_earned(tasks),
        "total": int(total) if isinstance(total, (int, float)) and total else ONBOARDING_TOTAL,
    }


def complete_task(gateway, session: str, key: str) -> Dict[str, Any]:
    """Claim one task.  A replay (``alreadyCompleted: true``) counts as success."""
    if key not in TASK_POINTS:
        # Same as the official client: refuse locally instead of sending a
        # request that is guaranteed to fail.
        raise OnboardingError(f"未知的 task key: {key}", code=BAD_REQUEST_CODE)
    data = _request(gateway, session, "onboarding/tasks/complete", {"key": key})
    record = data if isinstance(data, dict) else {}
    balance = record.get("balance")
    return {
        "key": key,
        "points": TASK_POINTS[key],
        "title": TASK_TITLES.get(key, key),
        "already_completed": record.get("alreadyCompleted") is True,
        "balance": int(balance) if isinstance(balance, (int, float)) else None,
    }


def claim_all(gateway, session: str) -> Dict[str, Any]:
    """Claim every outstanding task, serially, skipping what is already done.

    Serial on purpose (the official client does the same).  Already-completed
    keys are **skipped without a request**; an auth failure aborts the batch
    immediately.
    """
    state = fetch_tasks(gateway, session)
    claimed: List[Dict[str, Any]] = []
    skipped: List[str] = []
    failed: List[Dict[str, Any]] = []
    tasks = dict(state["tasks"])

    for key in TASK_POINTS:
        if state["tasks"].get(key) is True:
            skipped.append(key)
            continue
        try:
            result = complete_task(gateway, session, key)
        except OnboardingError as exc:
            failed.append({"key": key, "title": TASK_TITLES.get(key, key),
                           "error": str(exc), "code": exc.code})
            if exc.auth_failed:
                # Session is dead — every later call would fail too.
                break
            continue
        # A replay counts as claimed as well (idempotent).
        claimed.append(result)
        tasks[key] = True

    return {
        "claimed": claimed,
        "skipped": skipped,
        "failed": failed,
        "earned": compute_earned(tasks),
        "total": state["total"],
        "claimed_points": sum(item["points"] for item in claimed),
    }


def claim_for_account(pool, account, *, log=None) -> Dict[str, Any]:
    """Convenience wrapper: make sure the session is fresh, claim, refresh quota.

    Never raises for business failures — the panel and the CLI both want a
    per-account result object rather than an exception.
    """
    emit = log or (lambda _msg: None)
    result: Dict[str, Any] = {"name": account.name, "ok": False}
    try:
        pool.ensure_session(account)
    except Exception as exc:                            # noqa: BLE001
        result["error"] = f"session 不可用：{exc}"
        return result

    try:
        outcome = claim_all(pool.gateway, account.session)
    except OnboardingError as exc:
        result["error"] = str(exc)
        result["code"] = exc.code
        if exc.auth_failed:
            emit(f"[新手任务] {account.name} session 已失效，请重新登录")
        return result

    result.update({
        "ok": True,
        "claimed": outcome["claimed"],
        "skipped": outcome["skipped"],
        "failed": outcome["failed"],
        "claimed_points": outcome["claimed_points"],
        "earned": outcome["earned"],
        "total": outcome["total"],
    })
    emit(f"[新手任务] {account.name} 领取完成：本次 +{outcome['claimed_points']} 积分，"
         f"已完成 {len(outcome['skipped'])}/{len(TASK_POINTS)}，"
         f"累计 {outcome['earned']}/{outcome['total']}")
    try:
        pool.refresh_quota(account)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        pool.save()
    except Exception:                                   # noqa: BLE001
        pass
    return result
