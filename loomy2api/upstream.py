"""Client for the Loomy model gateway (OpenAI-compatible upstream)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .httpc import json_body, open_stream, request as http_request

__all__ = ["ModelGateway", "UpstreamError", "build_model_headers"]


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def build_model_headers(session: str, *, version: str = "",
                        extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Headers the model gateway requires.

    * ``Authorization`` **and** ``token`` — the client's provider uses
      ``useSessionAuth``, and the gateway accepts either, so both are sent.
    * ``traceparent`` — without it the upstream hangs until timeout (measured
      by the client authors, documented in their source).
    * ``loomy-version`` — presence-checked only.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session}",
        "token": session,
        "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
    }
    if version:
        headers["loomy-version"] = str(version)
    for key, value in (extra or {}).items():
        if value:
            headers[key] = str(value)
    return headers


class ModelGateway:
    """Thin wrapper around the upstream HTTP API."""

    def __init__(self, cfg):
        self.cfg = cfg

    # -- helpers --------------------------------------------------------

    @property
    def base(self) -> str:
        return str(self.cfg["upstream"]).rstrip("/")

    def _url(self, suffix: str) -> str:
        return f"{self.base}/{suffix.lstrip('/')}"

    def _headers(self, session: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        return build_model_headers(session, version=str(self.cfg.get("loomy_version") or ""),
                                   extra=extra)

    # -- read-only ------------------------------------------------------

    def models(self, session: str) -> Dict[str, Any]:
        status, _h, data = http_request(
            self._url("models"), method="GET", headers=self._headers(session),
            timeout=float(self.cfg.get("timeout") or 60),
            proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/models HTTP {status}: "
                                f"{data[:200].decode('utf-8', 'replace')}", status)
        return json.loads(data.decode("utf-8"))

    def points_records(self, session: str, page_size: int = 20) -> Dict[str, Any]:
        url = self._url(f"points/records?record_type=all&page_no=1&page_size={page_size}")
        status, _h, data = http_request(
            url, method="GET", headers=self._headers(session),
            timeout=30, proxy=str(self.cfg.get("proxy") or ""))
        if status != 200:
            raise UpstreamError(f"/points/records HTTP {status}", status)
        return json.loads(data.decode("utf-8"))

    def quota(self, session: str) -> Dict[str, Any]:
        """→ ``{balance, daily_balance, available}``."""
        payload = self.points_records(session, page_size=1)
        data = payload.get("data") or {}
        return {
            "balance": data.get("balance"),
            "daily_balance": data.get("dailyBalance"),
            "available": data.get("availableBalance"),
        }

    # -- request passthrough -------------------------------------------

    def call(self, session: str, suffix: str, payload: Dict[str, Any],
             extra_headers: Optional[Dict[str, str]] = None,
             method: str = "POST") -> Tuple[int, Dict[str, str], bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        return http_request(
            self._url(suffix), method=method, headers=self._headers(session, extra_headers),
            body=body, timeout=float(self.cfg.get("timeout") or 1200),
            proxy=str(self.cfg.get("proxy") or ""))

    def stream(self, session: str, suffix: str, payload: Dict[str, Any],
               extra_headers: Optional[Dict[str, str]] = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return open_stream(
            self._url(suffix), method="POST",
            headers=self._headers(session, extra_headers), body=body,
            timeout=float(self.cfg.get("timeout") or 1200),
            proxy=str(self.cfg.get("proxy") or ""))


def extract_usage(obj: Any) -> Dict[str, Any]:
    """Usage/points accounting that tolerates missing fields."""
    if not isinstance(obj, dict):
        return {}
    usage = obj.get("usage")
    return usage if isinstance(usage, dict) else {}


def sse_usage(line: bytes, sink: Dict[str, Any]) -> None:
    """Fold ``usage`` out of one SSE line into ``sink`` (last write wins)."""
    if not line.startswith(b"data:"):
        return
    chunk = line[5:].strip()
    if not chunk or chunk == b"[DONE]":
        return
    try:
        obj = json.loads(chunk)
    except Exception:                                   # noqa: BLE001
        return
    usage = extract_usage(obj)
    if usage:
        sink.update(usage)


def human_usage(usage: Dict[str, Any]) -> str:
    parts = []
    if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
        parts.append(f"tok={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning:
        parts.append(f"think={reasoning}")
    if usage.get("points_consumed") is not None:
        parts.append(f"points={usage['points_consumed']}")
    return " ".join(parts)


def now() -> int:
    return int(time.time())
