"""Configuration loading.

Precedence: environment variables > config.json > built-in defaults.
Paths in the config are resolved relative to the project root (the directory
holding config.json), so the project can live anywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from . import constants as C

__all__ = ["Config", "load_config", "PROJECT_ROOT"]

#: Repository root = parent of this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 17890,

    # upstreams -----------------------------------------------------------
    "upstream": C.DEFAULT_MODEL_BASE,
    "account_base": C.DEFAULT_ACCOUNT_BASE,
    "account_appid": C.DEFAULT_ACCOUNT_APPID,
    "access_key_id": "",          # empty → use the client-shipped constant
    "access_key_secret": "",
    "loomy_version": C.DEFAULT_LOOMY_VERSION,
    "proxy": "",                  # "" = direct; http:// https:// or socks5://host:port

    # gateway -------------------------------------------------------------
    "api_keys": [],               # [] = no auth; otherwise Bearer / x-api-key
    "default_model": C.DEFAULT_MODEL,
    "timeout": 1200,
    "request_purpose": "chat.message",
    "log_dir": "logs",
    "log_requests": True,
    "strip_model_prefix": True,   # accept "imodel/deepseek-v4-flash-0731"

    # account pool --------------------------------------------------------
    "accounts_file": "accounts.json",
    "strategy": "balance",        # balance | round_robin | lru
    "max_retries": 2,             # extra accounts tried on auth/quota errors
    "cooldown_seconds": 300,
    "session_renew_before_days": 3,
    "quota_refresh_minutes": 30,
    "sessions_from_client": True, # also honour the desktop client's session
    "client_root": "",            # override the client's data dir (tests)
    # identity_mode: per_account → each account gets its own devid / campus id;
    #                client       → mirror the shipped client exactly.
    "identity_mode": "per_account",
}

ENV_MAP = {
    "LOOMY_HOST": "host",
    "LOOMY_PORT": "port",
    "LOOMY_UPSTREAM": "upstream",
    "LOOMY_ACCOUNT_BASE": "account_base",
    "LOOMY_AK_ID": "access_key_id",
    "LOOMY_AK_SECRET": "access_key_secret",
    "LOOMY_API_KEYS": "api_keys",
    "LOOMY_DEFAULT_MODEL": "default_model",
    "LOOMY_PROXY": "proxy",
    "LOOMY_ACCOUNTS_FILE": "accounts_file",
    "LOOMY_LOG_DIR": "log_dir",
    "LOOMY_STRATEGY": "strategy",
}


class Config(dict):
    """A dict with attribute access plus a few derived helpers."""

    def __getattr__(self, item: str) -> Any:          # pragma: no cover
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    # -- derived -------------------------------------------------------

    @property
    def ak_id(self) -> str:
        return str(self.get("access_key_id") or C.DEFAULT_ACCESS_KEY_ID)

    @property
    def ak_secret(self) -> str:
        return str(self.get("access_key_secret") or C.DEFAULT_ACCESS_KEY_SECRET)

    @property
    def api_keys(self) -> List[str]:
        keys = self.get("api_keys") or []
        return [str(k) for k in keys if k]

    def path(self, key: str, default: str = "") -> Path:
        raw = str(self.get(key) or default)
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p


def _coerce(cfg: Dict[str, Any], key: str, value: str) -> Any:
    base = DEFAULTS.get(key)
    if key in ("host", "upstream", "proxy") or isinstance(base, str):
        return value
    if isinstance(base, int):
        try:
            return int(value)
        except ValueError:
            return base
    if isinstance(base, list) and key == "api_keys":
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config(DEFAULTS)

    config_path = Path(path) if path else PROJECT_ROOT / "config.json"
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            raise RuntimeError(f"config.json 解析失败 / parse error: {exc}") from exc
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})

    for env_key, cfg_key in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            cfg[cfg_key] = _coerce(cfg, cfg_key, value)

    # Remember where to write panel edits back. Missing file is fine.
    cfg.config_path = config_path
    cfg.proxy_from_env = bool(os.environ.get("LOOMY_PROXY"))
    return cfg
