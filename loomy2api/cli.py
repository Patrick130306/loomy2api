"""Command line interface.

    loomy2api serve                 start the gateway
    loomy2api accounts              show pool status (quota, sessions, cooldown)
    loomy2api add <name> ...        add an account
    loomy2api remove <name>         remove an account
    loomy2api login [name ...]      log in / refresh sessions
    loomy2api sms <phone>           send an SMS code (SMS login path)
    loomy2api verify <name> <phone> <code> <msgid>
    loomy2api models                list upstream models
    loomy2api quota                 show quota of every account
    loomy2api chat "prompt"         one-shot chat through the gateway
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

from . import constants as C
from .config import load_config
from .account import AccountClient, Account, AccountError
from .pool import AccountPool, PoolError
from .server import Gateway, Logger, serve
from .upstream import ModelGateway


def _pool(cfg) -> AccountPool:
    return AccountPool(cfg, logger=Logger(cfg.path("log_dir", "logs")))


def _human_quota(acc: Account) -> str:
    if acc.available is None:
        return "额度未知"
    return (f"可用 {acc.available} = 余额 {acc.balance} + 每日 {acc.daily_balance}")


def cmd_serve(cfg, args) -> int:
    if args.port:
        cfg["port"] = args.port
    serve(cfg)
    return 0


def cmd_accounts(cfg, args) -> int:
    pool = _pool(cfg)
    for acc in pool.accounts:
        if acc.session_valid:
            pool.refresh_quota(acc)          # offline-tolerant by design
    snap = pool.snapshot()
    print(f"策略 {snap['strategy']} · 账号 {snap['count']} 个 · 可用 {snap['usable']} 个")
    print("-" * 78)
    header = f"{'名称':<18}{'可用积分':>10}{'剩余天数':>10}{'请求':>6}{'扣分':>7}  状态"
    print(header)
    for acc in pool.accounts:
        days = f"{acc.days_left:.1f}" if acc.days_left is not None else "-"
        state = "正常"
        if not acc.enabled:
            state = "已禁用"
        elif acc.in_cooldown:
            state = f"冷却 {acc.cooldown_until - time.time():.0f}s"
        elif not acc.session_valid:
            state = "无登录态"
        print(f"{acc.name:<18}{str(acc.available if acc.available is not None else '-'):>10}"
              f"{days:>10}{acc.requests:>6}{acc.points_used:>7}  {state}")
        if acc.last_error and state != "正常":
            print(f"{'':<18}└─ {acc.last_error[:60]}")
    return 0


def cmd_add(cfg, args) -> int:
    pool = _pool(cfg)
    acc = pool.add_account(args.name, loginid=args.phone or "",
                           password=args.password or "",
                           session=args.session or "")
    if args.phone and args.password and not args.no_login:
        try:
            pool.ensure_session(acc, force=True)
            pool.refresh_quota(acc)
            print(f"已添加并登录 {acc.name}：{_human_quota(acc)}")
        except (AccountError, PoolError) as exc:
            print(f"账号已保存，但登录失败：{exc}")
    else:
        print(f"已添加账号 {acc.name}")
    pool.flush()
    return 0


def cmd_remove(cfg, args) -> int:
    pool = _pool(cfg)
    if not pool.get(args.name):
        print(f"没有名为 {args.name} 的账号")
        return 1
    if not args.yes:
        answer = input(f"确认删除账号 {args.name}？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 0
    pool.remove_account(args.name)
    print(f"已删除 {args.name}")
    return 0


def cmd_login(cfg, args) -> int:
    pool = _pool(cfg)
    targets: List[Account] = ([pool.get(n) for n in args.names] if args.names
                              else pool.accounts)
    targets = [t for t in targets if t]
    if not targets:
        print("没有要登录的账号（先用 loomy2api add 添加）")
        return 1
    failed = 0
    for acc in targets:
        try:
            pool.ensure_session(acc, force=True)
            pool.refresh_quota(acc)
            print(f"✓ {acc.name}: userid={acc.userid} session={acc.session[:8]}… "
                  f"剩余 {(acc.days_left or 0):.1f} 天 · {_human_quota(acc)}")
        except (AccountError, PoolError) as exc:
            failed += 1
            print(f"✗ {acc.name}: {exc}")
    pool.flush()
    return 1 if failed else 0


def cmd_sms(cfg, args) -> int:
    client = AccountClient(cfg)
    try:
        payload = client.send_sms_code(args.phone)
    except AccountError as exc:
        print(f"发送失败：{exc}")
        return 1
    msgid = (payload.get("data") or {}).get("msgid") or payload.get("msgid") or ""
    print(f"验证码已下发到 {args.phone}，msgid={msgid}")
    print(f"下一步：loomy2api verify <账号名> {args.phone} <验证码> {msgid}")
    return 0


def cmd_verify(cfg, args) -> int:
    client = AccountClient(cfg)
    pool = _pool(cfg)
    try:
        got = client.login_by_sms(args.phone, args.code, args.msgid)
    except AccountError as exc:
        print(f"登录失败：{exc}")
        return 1
    acc = pool.get(args.name)
    if acc is None:
        acc = pool.add_account(args.name, loginid=args.phone)
    acc.session = got["session"]
    acc.userid = got.get("userid", "")
    acc.obtained_at = int(time.time())
    acc.expire_at = acc.obtained_at + C.SESSION_EXPIRE_SECONDS
    pool.refresh_quota(acc)
    pool.save(force=True)
    print(f"✓ {acc.name} 登录成功 userid={acc.userid} session={acc.session[:8]}…")
    return 0


def cmd_identity(cfg, args) -> int:
    pool = _pool(cfg)
    acc = pool.get(args.name)
    if acc is None:
        print(f"没有名为 {args.name} 的账号")
        return 1
    if args.rebind:
        try:
            pool.rebind_identity(args.name)
            print(f"✓ {args.name} 已绑定新设备标识 devid={acc.identity.get('devid')}")
        except (AccountError, PoolError) as exc:
            print(f"✗ 重绑失败：{exc}")
            return 1
    view = acc.identity_view()
    print(f"{args.name} 设备标识：")
    for key in ("devid", "ua", "modelid", "version", "campus_device_id"):
        print(f"  {key:<18}{view.get(key) or '—'}")
    if view.get("created_at"):
        print(f"  {'created_at':<18}"
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(view['created_at']))}")
    if not view.get("bound"):
        print("  （尚未绑定，下次登录或 --rebind 时生成）")
    return 0


def cmd_models(cfg, args) -> int:
    pool = _pool(cfg)
    client = ModelGateway(cfg)
    acc = pool.acquire()
    data = client.models(acc.session).get("data") or []
    print(f"{'id':<26}{'倍率':<10}{'上下文':>10}  能力")
    print("-" * 78)
    for model in data:
        caps = model.get("capabilities") or {}
        tags = ",".join(k for k, v in (
            ("reasoning", caps.get("reasoning")),
            ("vision", caps.get("vision")),
            ("tools", caps.get("function_calling")),
            ("audio", "audio" in (caps.get("input_modalities") or [])),
        ) if v)
        name = str(model.get("name") or "")
        mult = name[name.rfind("x") : -1] if "x" in name else "-"
        print(f"{model.get('id', ''):<26}{mult:<10}"
              f"{str(model.get('context_length') or '-'):>10}  {tags}")
    return 0


def cmd_quota(cfg, args) -> int:
    pool = _pool(cfg)
    total = 0
    for acc in pool.accounts:
        pool.refresh_quota(acc)
        if isinstance(acc.available, int):
            total += acc.available
        print(f"{acc.name:<18}{_human_quota(acc)}")
    pool.save(force=True)
    print("-" * 50)
    print(f"合计可用积分：{total}")
    return 0


def cmd_chat(cfg, args) -> int:
    pool = _pool(cfg)
    client = ModelGateway(cfg)
    acc = pool.acquire()
    payload = {
        "model": args.model or cfg.get("default_model") or C.DEFAULT_MODEL,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": False,
    }
    started = time.time()
    status, _h, data = client.call(acc.session, "chat/completions", payload)
    if status != 200:
        print(f"HTTP {status}: {data[:400].decode('utf-8', 'replace')}")
        return 1
    obj = json.loads(data.decode("utf-8"))
    message = (obj.get("choices") or [{}])[0].get("message") or {}
    print(f"[{acc.name} {time.time() - started:.1f}s] {message.get('content')}")
    print(f"usage: {json.dumps(obj.get('usage'), ensure_ascii=False)}")
    pool.report_success(acc, obj.get("usage") or {})
    pool.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("loomy2api", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default=None, help="config.json 路径")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="启动网关")
    p.add_argument("--port", type=int, default=0, help="覆盖配置里的端口")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("accounts", help="查看账号池状态")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("add", help="添加账号")
    p.add_argument("name")
    p.add_argument("--phone", default="", help="手机号（登录用）")
    p.add_argument("--password", default="", help="密码（可自动续期）")
    p.add_argument("--session", default="", help="已有的 session（不想给密码时）")
    p.add_argument("--no-login", action="store_true", help="只保存不立即登录")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="删除账号")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true", help="不确认直接删")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("login", help="登录/续期账号")
    p.add_argument("names", nargs="*", help="账号名（缺省=全部）")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("sms", help="发短信验证码（短信登录第一步）")
    p.add_argument("phone")
    p.set_defaults(func=cmd_sms)

    p = sub.add_parser("verify", help="用验证码换 session")
    p.add_argument("name")
    p.add_argument("phone")
    p.add_argument("code")
    p.add_argument("msgid")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("identity", help="查看/重绑账号的设备标识")
    p.add_argument("name")
    p.add_argument("--rebind", action="store_true", help="生成新标识并重新登录")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("models", help="列出上游模型")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("quota", help="查看各账号积分")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("chat", help="单次对话自检")
    p.add_argument("prompt")
    p.add_argument("--model", default="")
    p.set_defaults(func=cmd_chat)

    return parser


def _configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles and pipes default to a legacy codepage (cp1252 on CI
    runners, cp936 on a Chinese desktop), and every message this CLI prints is
    UTF-8 — without this, ``--help`` alone blows up with UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # 3.7+
        except Exception:                               # noqa: BLE001
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    cfg = load_config(args.config)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130
    except (PoolError, AccountError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(main())
