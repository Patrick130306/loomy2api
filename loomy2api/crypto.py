"""Crypto helpers — pure standard library.

Two independent things live here:

1. :func:`rsa_encrypt` — RSA/PKCS#1 v1.5 encryption of the account password.
   Implemented with ``pow()`` so the project needs no third-party dependency.
   The iFlytek account service hands out a 1024-bit DER public key, which is
   small enough that bignum modexp in Python is instant.

2. :func:`decrypt_client_env` — optional: decrypt the ``.env.prod`` that the
   desktop client ships (AES-256-GCM + scrypt).  Only useful for people who
   want to pull the endpoints/keys out of their own installation; needs
   ``cryptography`` (``pip install loomy2api[client-env]``).
"""

from __future__ import annotations

import base64
import os
from hashlib import scrypt
from typing import Optional, Tuple

from . import constants as C

__all__ = ["rsa_encrypt", "der_to_numbers", "decrypt_client_env"]


# --------------------------------------------------------------------- RSA


def _read_der(buf: bytes, pos: int) -> Tuple[int, int, int]:
    """Minimal DER reader → ``(tag, header_len, content_len)``."""
    tag = buf[pos]
    pos += 1
    if pos >= len(buf):
        raise ValueError("truncated DER")
    length = buf[pos]
    pos += 1
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4:
            raise ValueError("unsupported DER length")
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    return tag, pos, length


def der_to_numbers(der: bytes) -> Tuple[int, int]:
    """Extract ``(modulus, exponent)`` from a DER SubjectPublicKeyInfo blob.

    Handles the standard ``SEQUENCE{ SEQUENCE{ OID, NULL }, BIT STRING{
    SEQUENCE{ INTEGER n, INTEGER e } } }`` layout — no ASN.1 library needed.
    """
    tag, pos, _ = _read_der(der, 0)          # outer SEQUENCE
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")

    tag, pos, alg_len = _read_der(der, pos)  # AlgorithmIdentifier SEQUENCE
    if tag != 0x30:
        raise ValueError("missing AlgorithmIdentifier")
    pos += alg_len                           # skip OID + NULL params

    tag, body, length = _read_der(der, pos)  # BIT STRING
    if tag != 0x03:
        raise ValueError("missing BIT STRING")
    if der[body] != 0x00:
        raise ValueError("unexpected unused bits in BIT STRING")
    pos = body + 1

    tag, pos, _ = _read_der(der, pos)        # inner RSAPublicKey SEQUENCE
    if tag != 0x30:
        raise ValueError("missing RSAPublicKey")

    vals = []
    for _ in range(2):                       # INTEGER n, INTEGER e
        tag, body, length = _read_der(der, pos)
        if tag != 0x02:
            raise ValueError("expected INTEGER")
        vals.append(int.from_bytes(der[body:body + length], "big"))
        pos = body + length
    return vals[0], vals[1]


def rsa_encrypt(pubkey_der_b64: str,
                plaintext: str,
                rng: Optional[bytes] = None) -> str:
    """RSA/PKCS#1 v1.5 encrypt ``plaintext`` → base64 ciphertext.

    ``rng`` lets tests inject deterministic padding bytes.
    """
    der = base64.b64decode(pubkey_der_b64 + "=" * (-len(pubkey_der_b64) % 4))
    n, e = der_to_numbers(der)
    k = (n.bit_length() + 7) // 8
    msg = plaintext.encode("utf-8")
    if len(msg) > k - 11:
        raise ValueError(f"message too long for RSA-{k * 8}")

    ps_len = k - len(msg) - 3
    while True:
        ps = rng if rng is not None else os.urandom(ps_len)
        if len(ps) != ps_len:
            raise ValueError("rng produced wrong length")
        if 0 not in ps:                       # PKCS#1 requires non-zero padding
            break
    block = b"\x00\x02" + ps + b"\x00" + msg
    c = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(c.to_bytes(k, "big")).decode()


# ------------------------------------------------------- client .env (opt.)


def _is_encrypted(raw: str) -> bool:
    return raw.startswith(C.ENV_CRYPT_MAGIC + ":")


def decrypt_client_env(raw: str) -> str:
    """Decrypt a ``LOOMYENC1:`` blob; plaintext passes through untouched.

    Requires the optional ``cryptography`` dependency (AES-GCM + scrypt).
    """
    if not _is_encrypted(raw):
        return raw
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "decrypting the client's .env.prod needs the optional dependency: "
            "pip install loomy2api[client-env]"
        ) from exc

    b64 = raw[len(C.ENV_CRYPT_MAGIC) + 1:].strip()
    # Node's Buffer.from(x, 'base64') is lenient, Python's b64decode is not:
    # the client's ciphertext is often missing base64 padding.
    payload = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    salt = payload[:C.SALT_LEN]
    iv = payload[C.SALT_LEN:C.SALT_LEN + C.IV_LEN]
    tag = payload[C.SALT_LEN + C.IV_LEN:C.SALT_LEN + C.IV_LEN + C.TAG_LEN]
    data = payload[C.SALT_LEN + C.IV_LEN + C.TAG_LEN:]

    key = scrypt(C.ENV_CRYPT_PASSPHRASE.encode(), salt=salt, n=16384, r=8, p=1,
                 dklen=C.KEY_LEN)
    return AESGCM(key).decrypt(iv, data + tag, None).decode("utf-8")


def parse_env(text: str) -> dict:
    """``KEY=VALUE`` lines → dict (comments and blanks ignored)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
