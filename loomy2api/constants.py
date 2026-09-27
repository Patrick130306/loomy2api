"""Default endpoints and client constants.

Everything here is *configurable* at runtime (config.json / environment
variables); these are only defaults.

The account access key pair below is the one the official Loomy client itself
ships inside ``resources/.env.prod`` (a client-side constant that is present in
every installation, obfuscated with a passphrase that is hard-coded next to it
in ``electron/utils/env-file-crypto.js``).  It is used to sign requests to the
iFlytek *account* service, which is what turns "log in with a phone number"
into a plain HTTP call.  Override it with ``access_key_id`` /
``access_key_secret`` in config.json or with the ``LOOMY_AK_ID`` /
``LOOMY_AK_SECRET`` environment variables if your client ships different ones.
"""

from __future__ import annotations

# ---------------------------------------------------------------- upstreams

#: Model gateway (OpenAI-compatible: /chat/completions, /models, /embeddings, ...)
DEFAULT_MODEL_BASE = "https://loomyad.xunfei.cn/api/v1"

#: iFlytek account service (login / userinfo)
DEFAULT_ACCOUNT_BASE = "https://account.xfinfr.com"

DEFAULT_ACCOUNT_APPID = "GM3LOOMY"

#: Client-shipped signing constants for the account service.
DEFAULT_ACCESS_KEY_ID = "2thryby66wxi53sk"
DEFAULT_ACCESS_KEY_SECRET = "zsak6eadrbawz683wf5r3m2snrwj868r"

#: Sent as the ``loomy-version`` header; the gateway only checks it is present.
DEFAULT_LOOMY_VERSION = "0.9.38"

#: Passphrase used by the client to obfuscate ``resources/.env.prod``
#: (hard-coded in ``electron/utils/env-file-crypto.js`` of the client).
ENV_CRYPT_MAGIC = "LOOMYENC1"
ENV_CRYPT_PASSPHRASE = "loomy::env::3f9c1d2a7b6e4f08::local-obfuscation::v1"

#: Where the client keeps its data on Windows (``<root>/<sha256(user)[:12]>``).
CLIENT_PUBLIC_ROOT = r"C:\Users\Public\Loomy"

# ---------------------------------------------------------------- account

#: The client always asks for a 14-day session; there is no refresh token.
SESSION_EXPIRE_SECONDS = 14 * 24 * 3600

DEVICE_ID = "web"
WEB_MODEL_ID = "Web"
CLIENT_VERSION = "1.0.0"
CLIENT_UA = "Loomy|Desktop|Electron|macOS"

#: Prefix of the promotions device id the client derives from the machine
#: fingerprint (``loomy-campus-fp-<sha256(machineId)>``).  It is sent only with
#: ``/points/activation`` and ``/points/first-login`` bodies — never with chat
#: requests — but it is the one field that is device-scoped by design.
CAMPUS_DEVICE_ID_PREFIX = "loomy-campus-"

#: ``X-Loomy-Request-Purpose`` values understood by the upstream (17 total).
REQUEST_PURPOSES = (
    "chat.message", "chat.title", "pet.comment", "pet.reward",
    "kb.vision", "kb.audio", "kb.summary", "kb.embed",
    "search.web", "image.generate", "assistant.completion", "assistant.asr-correct",
    "memory.extract", "memory.recall", "memory.dream",
    "skill.evolution", "skill.curator",
)

# ---------------------------------------------------------------- models

#: Fallback catalogue, used when the upstream /models call fails.
#: ``multiplier`` is the points-per-request cost factor reported by the client.
FALLBACK_MODELS = (
    {"id": "spark-x", "name": "Spark X2.5 (x0.1)", "multiplier": 0.1,
     "context_length": 1048576, "modalities": ["text"]},
    {"id": "GLM-5.3-Flash", "name": "GLM 5.3 Flash (x0.8)", "multiplier": 0.8,
     "context_length": 1048576, "modalities": ["text", "image", "video"]},
    {"id": "qwen3.8-flash", "name": "qwen 3.8 flash (x0.8)", "multiplier": 0.8,
     "context_length": 1000000, "modalities": ["text", "image", "video"]},
    {"id": "deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash 0731 (x3.0)",
     "multiplier": 3.0, "context_length": 1048576, "modalities": ["text"]},
    {"id": "mimo-v2.5", "name": "MiMo V2.5 (x3.3)", "multiplier": 3.3,
     "context_length": 1048576, "modalities": ["text", "image", "audio", "video"]},
    {"id": "MiniMax-M3", "name": "MiniMax M3 (x4.0)", "multiplier": 4.0,
     "context_length": 1048576, "modalities": ["text", "image", "video"]},
    {"id": "Kimi-k2.6", "name": "Kimi k2.6 (x6.5)", "multiplier": 6.5,
     "context_length": 262144, "modalities": ["text", "image", "video"]},
    {"id": "qwen-3.8-max", "name": "Qwen 3.8 Max (x12.0)", "multiplier": 12.0,
     "context_length": 1000000, "modalities": ["text"]},
    {"id": "Hy-Image-3.5-preview", "name": "Hy image 3.5 preview",
     "multiplier": 0.0, "context_length": 100000, "modalities": ["text", "image"]},
    {"id": "doubao-seedream-5-lite", "name": "Doubao seedream 5 lite",
     "multiplier": 0.0, "context_length": 128000, "modalities": ["text", "image"]},
    {"id": "qwen-image-3.0-pro", "name": "qwen image 3.0 pro",
     "multiplier": 0.0, "context_length": 0, "modalities": ["text", "image"]},
)

DEFAULT_MODEL = "deepseek-v4-flash-0731"

#: Minimum AES-GCM tag / iv / salt sizes from the client's crypto module.
SALT_LEN, IV_LEN, TAG_LEN, KEY_LEN = 16, 12, 16, 32
