"""iFlytek account-service request signing (HMAC-SHA1).

Ported 1:1 from the client's ``electron/xfyun/sign.js``::

    stringToSign = "\\n".join([
        METHOD, ESCAPED_PATH, ESCAPED_QUERY, Content-MD5,
        Content-Type, Date, Nonce, SignedHeaders, CanonicalizedHeaders,
    ])
    signature = base64(hmac_sha1(accessKeySecret, stringToSign))
    Authorization = "account <accessKeyId>:<signature>"

The last two fields are empty strings when no custom ``x-*`` headers are sent,
but they must still be present — the string is 9 lines, not 7.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Dict, Mapping, Optional
from urllib.parse import quote

__all__ = ["build_headers", "sign", "string_to_sign"]

#: RFC 3986 sub-delims that ``urllib.parse.quote`` keeps but the server escapes.
_EXTRA_ESCAPES = {"!": "%21", "'": "%27", "(": "%28", ")": "%29", "*": "%2A"}


def _escape(value: str) -> str:
    out = quote(str(value), safe="-_.~")
    for raw, enc in _EXTRA_ESCAPES.items():
        out = out.replace(raw, enc)
    return out


def content_md5(body: str) -> str:
    """base64(md5(body)) — empty string for an empty body."""
    if not body:
        return ""
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode()


def _escaped_path(path: str) -> str:
    p = path if path.startswith("/") else "/" + path
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return "/".join(_escape(seg) if seg else "" for seg in p.split("/"))


def _escaped_query(params: Mapping[str, object]) -> str:
    if not params:
        return ""
    return "&".join(
        f"{_escape(k)}={_escape('' if v is None else v)}" for k, v in params.items()
    )


def _canonicalized(headers: Mapping[str, object], prefix: str = "x-"):
    picked = {k.lower(): str(v).strip() for k, v in headers.items()
              if k.lower().startswith(prefix)}
    if not picked:
        return "", ""
    keys = sorted(picked)
    canonical = "".join(f"{k}:{picked[k]}\n" for k in keys)
    return ";".join(keys), canonical


def string_to_sign(*, method: str, path: str, query: Optional[Mapping] = None,
                   body: str = "", content_type: str = "application/json",
                   date: str, nonce: str,
                   custom_headers: Optional[Mapping] = None) -> str:
    signed_headers, canonical = _canonicalized(custom_headers or {})
    return "\n".join([
        method.upper(),
        _escaped_path(path),
        _escaped_query(query or {}),
        content_md5(body),
        content_type,
        date,
        nonce,
        signed_headers,
        canonical.rstrip("\n"),
    ])


def sign(*, secret: str, **kwargs) -> str:
    digest = hmac.new(secret.encode("utf-8"),
                      string_to_sign(**kwargs).encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def build_headers(access_key_id: str, access_key_secret: str, *,
                  method: str, path: str, body: str = "",
                  query: Optional[Mapping] = None,
                  content_type: str = "application/json",
                  custom_headers: Optional[Mapping] = None,
                  clock=None) -> Dict[str, str]:
    """Full header set for one account-service request."""
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                         time.gmtime(None if clock is None else clock))
    nonce = str(uuid.uuid4())
    signature = sign(
        secret=access_key_secret, method=method, path=path, query=query,
        body=body, content_type=content_type, date=date, nonce=nonce,
        custom_headers=custom_headers,
    )
    headers = {
        "Authorization": f"account {access_key_id}:{signature}",
        "Date": date,
        "Nonce": nonce,
        "Content-Type": content_type,
    }
    md5 = content_md5(body)
    if md5:
        headers["Content-MD5"] = md5
    for key, value in (custom_headers or {}).items():
        headers[key] = str(value)
    return headers
