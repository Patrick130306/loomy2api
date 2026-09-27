"""HTTP gateway: OpenAI-compatible + Anthropic-compatible endpoints."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from . import anthropic as anth
from .panel import Panel
from .pool import AccountPool, PoolError
from .upstream import ModelGateway, sse_usage, human_usage

__all__ = ["Gateway", "serve"]


class Logger:
    """Timestamped file+console logger (never raises)."""

    def __init__(self, log_dir: Path, name: str = "gateway.log",
                 console: bool = True):
        self.path = Path(log_dir) / name
        self.console = console
        self._lock = threading.Lock()

    def __call__(self, message: str, *, console: Optional[bool] = None) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        if self.console if console is None else console:
            print(line, flush=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:                               # noqa: BLE001
            pass


class Gateway:
    """Holds the configuration, the account pool and the model catalogue."""

    def __init__(self, cfg, log: Optional[Logger] = None):
        self.cfg = cfg
        self.log = log or Logger(cfg.path("log_dir", "logs"),
                                 console=bool(cfg.get("log_console", True)))
        self.pool = AccountPool(cfg, logger=self.log)
        self.models_client = ModelGateway(cfg)
        self.panel = Panel(self)
        self.models: list = []
        self._models_lock = threading.Lock()

    # -- model catalogue -------------------------------------------------

    def refresh_models(self) -> list:
        try:
            acc = self.pool.acquire()
            payload = self.models_client.models(acc.session)
            data = payload.get("data") or []
            with self._models_lock:
                self.models = data
            self.log(f"上游模型列表已刷新：{len(data)} 个")
        except Exception as exc:                        # noqa: BLE001
            with self._models_lock:
                have = bool(self.models)
            if not have:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
                self.log(f"[warn] 拉取模型列表失败，使用内置清单：{exc}")
        return self.models

    def catalogue(self) -> list:
        with self._models_lock:
            if not self.models:
                self.models = [dict(m, object="model", owned_by="loomy")
                               for m in C.FALLBACK_MODELS]
            return list(self.models)

    def resolve_model(self, requested: str) -> str:
        name = str(requested or "").strip()
        if self.cfg.get("strip_model_prefix", True) and "/" in name:
            name = name.split("/", 1)[1].strip()
        aliases = self.cfg.get("model_aliases") or {}
        if name in aliases:
            return str(aliases[name])
        if not name:
            return str(self.cfg.get("default_model") or C.DEFAULT_MODEL)
        return name

    # -- background ------------------------------------------------------

    def start(self) -> None:
        self.pool.bootstrap()
        self.pool.start()
        self.refresh_models()


# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loomy2api"
    gateway: Gateway = None                        # injected by serve()

    def setup(self):                               # noqa: D102
        super().setup()
        self._body_read = False

    # ------------------------------------------------------------- basics

    def log_message(self, fmt, *args):             # silence default access log
        return

    def _json(self, status: int, obj: Any, extra: Optional[dict] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        # Drain any unread request body and close the connection: on Windows an
        # abort (RST) rather than a clean error surfaces otherwise when we
        # reject before reading the payload (e.g. a 401 from the API-key gate).
        self._drain()
        self.close_connection = True
        self._json(status, {"error": {"message": message, "type": kind, "code": status}})

    def _drain(self) -> None:
        if self._body_read:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        self._body_read = True
        if length:
            try:
                self.rfile.read(length)
            except Exception:                           # noqa: BLE001
                pass

    def _query_int(self, name: str, default: int) -> int:
        from urllib.parse import parse_qs, urlparse as _urlparse
        values = parse_qs(_urlparse(self.path).query).get(name) or []
        try:
            return int(values[0])
        except (IndexError, ValueError):
            return default

    def _read_body(self) -> bytes:
        if self._body_read:
            return b""
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            raise ValueError(f"请求体不是合法 JSON / body is not valid JSON: {exc}") from exc

    def _path(self) -> str:
        """Normalise the path, keeping the non-``/v1`` surfaces intact.

        Clients are sloppy about the prefix, so ``/chat/completions`` is
        rewritten to ``/v1/chat/completions``; the panel, its API and the
        health/admin endpoints are exempt.
        """
        path = self.path.split("?", 1)[0]
        while "/v1/v1" in path:
            path = path.replace("/v1/v1", "/v1")
        if path in ("", "/", "/index.html"):
            return "/panel"                       # the panel is the landing page
        exempt = ("/panel", "/api/", "/health", "/admin", "/favicon")
        if not path.startswith(exempt):
            if path != "/v1" and not path.startswith("/v1/"):
                path = "/v1" + path
        return path.rstrip("/") or "/"

    # --------------------------------------------------------------- auth

    def _authorized(self) -> bool:
        keys = self.gateway.cfg.api_keys
        if not keys:
            return True
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = token or (self.headers.get("x-api-key") or "").strip()
        if token in keys:
            return True
        self._error(401, "无效的 API Key / invalid API key", "authentication_error")
        return False

    # ------------------------------------------------------------ routing

    def do_OPTIONS(self):                             # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):                                # noqa: N802
        self.do_GET()

    def do_GET(self):                                 # noqa: N802
        path = self._path()
        if path in ("/v1/health", "/health"):
            return self._health()
        if path in ("/panel", "/", "/index.html"):
            return self._panel_page()
        if not self._authorized():
            return
        try:
            if path == "/v1/models":
                return self._models()
            if path == "/v1/points":
                return self._points()
            if path in ("/v1/admin/accounts", "/admin/accounts"):
                return self._accounts()
            if path == "/api/panel/state":
                refresh = "refresh=1" in (self.path or "")
                return self._json(200, self.gateway.panel.state(refresh=refresh))
            if path == "/api/panel/logs":
                lines = self._query_int("lines", 200)
                return self._json(200, self.gateway.panel.logs(lines))
            if path == "/favicon.ico":
                return self._error(404, "no favicon")
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except PoolError as exc:
            return self._error(503, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] GET {self.path}: {exc}\n{traceback.format_exc()}")
            return self._error(502, f"上游调用失败 / upstream failure: {exc}", "upstream_error")

    def do_POST(self):                                # noqa: N802
        path = self._path()
        if not self._authorized():
            return
        try:
            if path == "/v1/chat/completions":
                return self._chat()
            if path == "/v1/messages":
                return self._messages()
            if path == "/v1/embeddings":
                return self._passthrough("embeddings")
            if path == "/v1/images/generations":
                return self._passthrough("images/generations")
            if path in ("/v1/admin/accounts/reload", "/admin/accounts/reload"):
                self.gateway.pool.load()
                self.gateway.refresh_models()
                return self._json(200, self.gateway.pool.snapshot())
            if path.startswith("/api/panel/"):
                return self._panel_api(path)
            return self._error(404, f"未知路径 / unknown path: {self.path}")
        except ValueError as exc:
            return self._error(400, str(exc))
        except PoolError as exc:
            return self._error(400, str(exc), "account_pool_error")
        except Exception as exc:                        # noqa: BLE001
            self.gateway.log(f"[error] POST {self.path}: {exc}\n{traceback.format_exc()}")
            try:
                return self._error(502, f"上游调用失败 / upstream failure: {exc}",
                                   "upstream_error")
            except Exception:                           # noqa: BLE001
                return

    # ------------------------------------------------------------- panel

    def _panel_page(self) -> None:
        body = self.gateway.panel.html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _panel_api(self, path: str) -> None:
        payload = self._read_json()
        panel = self.gateway.panel
        handlers = {
            "/api/panel/accounts": panel.add_account,
            "/api/panel/accounts/update": panel.update_account,
            "/api/panel/accounts/remove": panel.remove_account,
            "/api/panel/accounts/renew": panel.renew_account,
            "/api/panel/accounts/identity": panel.identity,
            "/api/panel/refresh": lambda _p: panel.refresh(_p),
        }
        handler = handlers.get(path)
        if handler is None:
            return self._error(404, f"未知面板接口 / unknown panel endpoint: {path}")
        result = handler(payload)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return self._json(200, result)

    # ------------------------------------------------------ simple routes

    def _health(self) -> None:
        gw = self.gateway
        accounts = gw.pool.accounts
        return self._json(200, {
            "status": "ok" if gw.pool.usable() else "degraded",
            "name": "loomy2api",
            "upstream": gw.cfg["upstream"],
            "accounts": len(accounts),
            "usable_accounts": len(gw.pool.usable()),
            "models": len(gw.catalogue()),
            "auth_required": bool(gw.cfg.api_keys),
            "strategy": gw.cfg.get("strategy"),
            "panel": f"http://{gw.cfg['host']}:{gw.cfg['port']}/panel",
        })

    def _models(self) -> None:
        gw = self.gateway
        catalogue = gw.catalogue()
        if not catalogue:
            gw.refresh_models()
            catalogue = gw.catalogue()
        aliases = gw.cfg.get("model_aliases") or {}
        extra = [{"id": alias, "object": "model", "owned_by": "alias",
                  "target": target} for alias, target in aliases.items()
                 if alias != target]
        return self._json(200, {"object": "list", "data": list(catalogue) + extra})

    def _points(self) -> None:
        gw = self.gateway
        out = []
        for acc in gw.pool.accounts:
            gw.pool.refresh_quota(acc)
            out.append({
                "name": acc.name,
                "userid": acc.userid,
                "available": acc.available,
                "balance": acc.balance,
                "daily_balance": acc.daily_balance,
                "requests": acc.requests,
                "points_used": acc.points_used,
            })
        gw.pool.save()
        # the same account can appear twice (own session + imported client
        # session) — count its quota only once
        seen: Dict[str, int] = {}
        for item in out:
            key = item["userid"] or item["name"]
            if isinstance(item["available"], int):
                seen[key] = max(seen.get(key, 0), item["available"])
        return self._json(200, {
            "total_available": sum(seen.values()),
            "unique_accounts": len(seen),
            "accounts": out,
        })

    def _accounts(self) -> None:
        return self._json(200, self.gateway.pool.snapshot())

    # ------------------------------------------------------- passthroughs

    def _passthrough(self, suffix: str) -> None:
        gw = self.gateway
        raw = self._read_body()
        acc = gw.pool.acquire()
        status, headers, data = gw.models_client.call(
            acc.session, suffix, json.loads(raw.decode("utf-8")) if raw else {})
        if status >= 400:
            gw.pool.report_failure(acc, status, data[:200].decode("utf-8", "replace"))
        else:
            gw.pool.report_success(acc)
        self.send_response(status)
        self.send_header("Content-Type",
                         headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------- chat (core)

    def _chat(self) -> None:
        gw = self.gateway
        req = self._read_json()
        model = gw.resolve_model(req.get("model"))
        stream = bool(req.get("stream"))

        extra: Dict[str, str] = {}
        for src, dst in (("chat_id", "ChatId"), ("msg_id", "MsgId"),
                         ("turn_id", "TurnId")):
            value = req.pop(src, None)
            if value:
                extra[dst] = value
        purpose = req.pop("request_purpose", None)
        if purpose:
            extra["X-Loomy-Request-Purpose"] = purpose
        req["model"] = model

        if stream:
            return self._chat_stream(req, extra)

        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            status, _hdrs, data = gw.models_client.call(
                acc.session, "chat/completions", req, extra)
            if status == 200:
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception:                       # noqa: BLE001
                    obj = None
                usage = (obj or {}).get("usage") or {}
                gw.pool.report_success(acc, usage)
                gw.log(f"[call] {acc.name} {model} {time.time() - started:.1f}s "
                       f"http=200 {human_usage(usage)}")
                return self._json(200, obj)
            last = (status, data)
            gw.log(f"[call] {acc.name} {model} http={status} → 换账号重试")
            gw.pool.report_failure(acc, status, data[:200].decode("utf-8", "replace"))
            if status in (400, 404, 422):               # not an account problem
                break
        status, data = last
        try:
            return self._json(status or 502, json.loads(data.decode("utf-8")))
        except Exception:                               # noqa: BLE001
            return self._error(status or 502, data[:500].decode("utf-8", "replace"),
                               "upstream_error")

    def _chat_stream(self, req: Dict[str, Any], extra: Dict[str, str]) -> None:
        gw = self.gateway
        model = req.get("model")
        tried: list = []
        conn = resp = None

        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            conn, resp = gw.models_client.stream(acc.session, "chat/completions", req, extra)
            if resp.status == 200:
                break
            data = resp.read()
            conn.close()
            gw.log(f"[call] {acc.name} {model} stream http={resp.status} → 换账号重试")
            gw.pool.report_failure(acc, resp.status,
                                   data[:200].decode("utf-8", "replace"))
            conn = resp = None

        if resp is None:
            return self._error(502, "所有账号都失败了 / all accounts failed",
                               "upstream_error")
        if resp.status != 200:
            data = resp.read()
            conn.close()
            try:
                return self._json(resp.status, json.loads(data.decode("utf-8")))
            except Exception:                           # noqa: BLE001
                return self._error(resp.status, data[:500].decode("utf-8", "replace"),
                                   "upstream_error")

        started = time.time()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        usage: Dict[str, Any] = {}
        buffer = b""
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    sse_usage(line.strip(), usage)
            if buffer:
                sse_usage(buffer.strip(), usage)
            gw.pool.report_success(acc, usage)
            gw.log(f"[call] {acc.name} {model} {time.time() - started:.1f}s "
                   f"http=200 {human_usage(usage)} stream")
        except (BrokenPipeError, ConnectionResetError):
            gw.log(f"[call] {acc.name} {model} client disconnected")
        finally:
            try:
                conn.close()
            except Exception:                           # noqa: BLE001
                pass

    # -------------------------------------------------- anthropic messages

    def _messages(self) -> None:
        gw = self.gateway
        req = self._read_json()
        payload = anth.anthropic_to_openai(req)
        payload["model"] = gw.resolve_model(payload.get("model"))
        model = payload["model"]
        if req.get("stream"):
            return self._messages_stream(payload, model)

        tried: list = []
        last: Tuple[int, bytes] = (0, b"")
        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            started = time.time()
            status, _h, data = gw.models_client.call(acc.session, "chat/completions", payload)
            if status == 200:
                obj = json.loads(data.decode("utf-8"))
                usage = obj.get("usage") or {}
                gw.pool.report_success(acc, usage)
                gw.log(f"[messages] {acc.name} {model} {time.time() - started:.1f}s "
                       f"http=200 {human_usage(usage)}")
                return self._json(200, anth.openai_to_anthropic(obj, model))
            last = (status, data)
            gw.pool.report_failure(acc, status, data[:200].decode("utf-8", "replace"))
            if status in (400, 404, 422):
                break
        status, data = last
        return self._json(status or 502, {
            "type": "error",
            "error": {"type": "api_error",
                      "message": data[:500].decode("utf-8", "replace")}})

    def _messages_stream(self, payload: Dict[str, Any], model: str) -> None:
        gw = self.gateway
        tried: list = []
        conn = resp = None

        for _ in range(int(gw.cfg.get("max_retries") or 0) + 1):
            try:
                acc = gw.pool.acquire(exclude=tried)
            except PoolError as exc:
                gw.log(f"[warn] {exc}")
                break
            tried.append(acc.name)
            conn, resp = gw.models_client.stream(acc.session, "chat/completions", payload)
            if resp.status == 200:
                break
            data = resp.read()
            conn.close()
            gw.pool.report_failure(acc, resp.status,
                                   data[:200].decode("utf-8", "replace"))
            conn = resp = None

        if resp is None:
            return self._json(502, {"type": "error", "error": {
                "type": "api_error", "message": "all accounts failed"}})
        if resp.status != 200:
            data = resp.read()
            conn.close()
            return self._json(resp.status, {"type": "error", "error": {
                "type": "api_error", "message": data[:500].decode("utf-8", "replace")}})

        translator = anth.StreamTranslator(model)
        started = time.time()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        def emit(event: str, data: Dict[str, Any]) -> None:
            self.wfile.write(
                f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                .encode("utf-8"))
            self.wfile.flush()

        buffer = b""
        try:
            for event, data in translator.start():
                emit(event, data)
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(body)
                    except Exception:                   # noqa: BLE001
                        continue
                    for event, data in translator.feed(obj):
                        emit(event, data)
            for event, data in translator.finish():
                emit(event, data)
            gw.pool.report_success(acc, translator.usage)
            gw.log(f"[messages] {acc.name} {model} {time.time() - started:.1f}s "
                   f"http=200 {human_usage(translator.usage)} stream")
        except (BrokenPipeError, ConnectionResetError):
            gw.log(f"[messages] {acc.name} {model} client disconnected")
        finally:
            try:
                conn.close()
            except Exception:                           # noqa: BLE001
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets two processes bind the same port with SO_REUSEADDR, which
    # silently sends requests to an old instance.  Only enable it elsewhere.
    allow_reuse_address = (os.name != "nt")


def serve(cfg, *, gateway: Optional[Gateway] = None) -> Gateway:
    gw = gateway or Gateway(cfg)
    gw.start()

    handler = type("BoundHandler", (Handler,), {"gateway": gw})
    httpd = Server((str(cfg["host"]), int(cfg["port"])), handler)
    gw.log(f"loomy2api 已启动 / listening → http://{cfg['host']}:{cfg['port']}"
           f"  (OpenAI: /v1/chat/completions · Anthropic: /v1/messages)"
           f"  账号 {len(gw.pool.accounts)} 个，可用 {len(gw.pool.usable())} 个")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        gw.log("收到中断，退出 / interrupted, shutting down")
    finally:
        gw.pool.stop()
        gw.pool.flush()
        httpd.server_close()
    return gw
