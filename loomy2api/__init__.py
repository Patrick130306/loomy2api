"""loomy2api — Loomy (iFlytek) quota → OpenAI/Anthropic compatible gateway.

Turn the model quota of a Loomy desktop-assistant account into a plain,
self-hosted API with a multi-account pool.  Zero third-party dependencies.

Typical use::

    from loomy2api import load_config, AccountPool, Gateway, serve

    cfg = load_config()
    pool = AccountPool(cfg)
    pool.bootstrap()
    serve(cfg, gateway=Gateway(cfg))
"""

from __future__ import annotations

__version__ = "1.0.0"

from .account import Account, AccountClient, AccountError      # noqa: F401
from .config import Config, load_config, PROJECT_ROOT          # noqa: F401
from .pool import AccountPool, PoolError                       # noqa: F401
from .server import Gateway, Logger, serve                     # noqa: F401
from .upstream import ModelGateway, UpstreamError              # noqa: F401

__all__ = [
    "__version__",
    "Account", "AccountClient", "AccountError",
    "AccountPool", "PoolError",
    "Config", "load_config", "PROJECT_ROOT",
    "Gateway", "Logger", "serve",
    "ModelGateway", "UpstreamError",
]
