"""Schulportal Hessen payload encryption (jCryption style).

The portal encrypts some AJAX payloads (messages) with AES-256-CBC in the
OpenSSL "Salted__" format. The passphrase is a random key that the client
sends to the server once, wrapped with the server's RSA public key
(PKCS#1 v1.5). The server answers with a challenge: the same key encrypted
with itself, which proves both sides agree.

Reference implementation: liblanis (lanis-mobile), lib/src/session/cryptor.dart.
"""

from __future__ import annotations

import base64
import re
import secrets
from hashlib import md5

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

PASSPHRASE_SIZE = 46
_ENCODED_TAG = re.compile(r"<encoded>(.*?)</encoded>", re.S)


def bytes_to_key(passphrase: bytes, salt: bytes, length: int = 48) -> bytes:
    """OpenSSL EVP_BytesToKey with MD5, as used by CryptoJS."""
    derived = b""
    block = b""
    while len(derived) < length:
        block = md5(block + passphrase + salt).digest()
        derived += block
    return derived[:length]


def _pkcs7_pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty plaintext")
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad padding")
    return data[:-n]


def encrypt_with(passphrase: bytes, plain: bytes) -> str:
    salt = secrets.token_bytes(8)
    key_iv = bytes_to_key(passphrase, salt)
    cipher = AES.new(key_iv[:32], AES.MODE_CBC, key_iv[32:48])
    return base64.b64encode(b"Salted__" + salt + cipher.encrypt(_pkcs7_pad(plain))).decode()


def decrypt_with(passphrase: bytes, encoded: str) -> bytes:
    raw = base64.b64decode(encoded)
    if raw[:8] != b"Salted__":
        raise ValueError("payload is not in OpenSSL salted format")
    key_iv = bytes_to_key(passphrase, raw[8:16])
    cipher = AES.new(key_iv[:32], AES.MODE_CBC, key_iv[32:48])
    return _pkcs7_unpad(cipher.decrypt(raw[16:]))


class Cryptor:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = key or secrets.token_bytes(PASSPHRASE_SIZE)
        self.ready = False

    def wrapped_key(self, public_key_pem: str) -> str:
        rsa = PKCS1_v1_5.new(RSA.import_key(public_key_pem))
        return base64.b64encode(rsa.encrypt(self.key)).decode()

    def verify_challenge(self, challenge: str) -> bool:
        try:
            ok = decrypt_with(self.key, challenge) == self.key
        except ValueError:
            ok = False
        self.ready = ok
        return ok

    def encrypt(self, text: str) -> str:
        return encrypt_with(self.key, text.encode("utf-8"))

    def decrypt(self, encoded: str) -> str:
        return decrypt_with(self.key, encoded).decode("utf-8")

    def decrypt_encoded_tags(self, html: str) -> str:
        def repl(match: re.Match) -> str:
            try:
                return self.decrypt(match.group(1))
            except ValueError:
                return ""

        return _ENCODED_TAG.sub(repl, html)
