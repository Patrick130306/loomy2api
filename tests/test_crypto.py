"""Crypto tests: DER parsing + RSA/PKCS#1 v1.5 encryption round-trip."""

from __future__ import annotations

import base64
import unittest

from loomy2api import crypto
from tests.support import rsa_keypair, spki_der_b64

#: Public key handed out by the live account service (public data, no secret).
REAL_PUKEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDNCA0OSvscqyTGPguV7roG6Ct6jqpRVuL1"
    "RtroTH97JBizHdLUaF5Fz9MyKmeFXPVtFn/CINsLzSqxuA8uT4cR5fKmsKlBqAyHNt+sSE+B"
    "6DUDspT5zFPDc/3IeI3/A9sTsl5n1XOLAPjyBodbG2FcDPWp9+MaFoMjaUDvDQ8BJQIDAQAB"
)


class TestDer(unittest.TestCase):
    def test_real_public_key(self):
        der = base64.b64decode(REAL_PUKEY)
        n, e = crypto.der_to_numbers(der)
        self.assertEqual(e, 65537)
        self.assertEqual(n.bit_length(), 1024)

    def test_generated_key_round_trip(self):
        n, e, _ = rsa_keypair(512)
        n2, e2 = crypto.der_to_numbers(base64.b64decode(spki_der_b64(n, e)))
        self.assertEqual((n2, e2), (n, e))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            crypto.der_to_numbers(b"\x31\x02\x00\x00")


class TestRsaEncrypt(unittest.TestCase):
    def test_padding_decrypts_back_to_plaintext(self):
        n, e, d = rsa_keypair(512)
        pukey = spki_der_b64(n, e)
        ciphertext = base64.b64decode(crypto.rsa_encrypt(pukey, "hunter2"))
        self.assertEqual(len(ciphertext), (n.bit_length() + 7) // 8)

        block = pow(int.from_bytes(ciphertext, "big"), d, n).to_bytes(
            len(ciphertext), "big")
        self.assertEqual(block[:2], b"\x00\x02")
        self.assertEqual(block[block.index(b"\x00", 2) + 1:], b"hunter2")
        self.assertGreaterEqual(block.index(b"\x00", 2) - 2, 8)   # ≥8 pad bytes
        self.assertNotIn(0, block[2:block.index(b"\x00", 2)])

    def test_deterministic_padding_for_tests(self):
        n, e, _ = rsa_keypair(512)
        pukey = spki_der_b64(n, e)
        size = (n.bit_length() + 7) // 8 - len("x") - 3
        out = crypto.rsa_encrypt(pukey, "x", rng=b"\x41" * size)
        self.assertEqual(len(base64.b64decode(out)), (n.bit_length() + 7) // 8)

    def test_message_too_long(self):
        n, e, _ = rsa_keypair(512)
        with self.assertRaises(ValueError):
            crypto.rsa_encrypt(spki_der_b64(n, e), "x" * 200)


class TestEnvParsing(unittest.TestCase):
    def test_parse_env(self):
        parsed = crypto.parse_env("# c\nA=1\nB = two \n\n")
        self.assertEqual(parsed, {"A": "1", "B": "two"})

    def test_plaintext_passthrough(self):
        self.assertEqual(crypto.decrypt_client_env("A=1"), "A=1")

    def test_padding_lenient_base64(self):
        # The client's ciphertext base64 is frequently missing padding; the
        # helper must not blow up before the (optional) AES step.
        raw = "LOOMYENC1:" + base64.b64encode(b"\x00" * 47).decode().rstrip("=")
        with self.assertRaises(Exception):
            crypto.decrypt_client_env(raw)      # no cryptography / bad ciphertext


if __name__ == "__main__":
    unittest.main()
