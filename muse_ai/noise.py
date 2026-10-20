"""Noise_XX_25519_AESGCM_SHA256 initiator matching the Muse web bundle.

This is intentionally small and explicit instead of relying on a generic Noise library,
so the state transitions match the captured Muse client.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"
EMPTY_AD = b""


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hkdf_noise(chaining_key: bytes, input_key_material: bytes, outputs: int = 2) -> tuple[bytes, ...]:
    temp = hmac.new(chaining_key, input_key_material, hashlib.sha256).digest()
    out1 = hmac.new(temp, b"\x01", hashlib.sha256).digest()
    out2 = hmac.new(temp, out1 + b"\x02", hashlib.sha256).digest()
    if outputs == 2:
        return out1, out2
    if outputs != 3:
        raise ValueError("Noise HKDF only supports 2 or 3 outputs here")
    out3 = hmac.new(temp, out2 + b"\x03", hashlib.sha256).digest()
    return out1, out2, out3


def nonce12(counter: int) -> bytes:
    if counter < 0 or counter >= 1 << 64:
        raise ValueError("nonce counter out of range")
    return b"\x00" * 4 + counter.to_bytes(8, "big")


class CipherState:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = bytes(key) if key is not None else None
        self._nonce = 0

    @property
    def has_key(self) -> bool:
        return self._key is not None

    @property
    def nonce(self) -> int:
        return self._nonce

    def initialize_key(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-GCM key must be 32 bytes")
        self._key = bytes(key)
        self._nonce = 0

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._key is None:
            return bytes(plaintext)
        nonce = nonce12(self._nonce)
        self._nonce += 1
        return AESGCM(self._key).encrypt(nonce, plaintext, ad)

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._key is None:
            return bytes(ciphertext)
        nonce = nonce12(self._nonce)
        self._nonce += 1
        return AESGCM(self._key).decrypt(nonce, ciphertext, ad)


class SymmetricState:
    def __init__(self) -> None:
        # Noise InitializeSymmetric(): if the protocol name is shorter than
        # HASHLEN (SHA-256 => 32), pad it with zero bytes.  If it is longer,
        # hash it.  Muse's protocol name is shorter than 32 bytes.
        if len(PROTOCOL) <= 32:
            initial_hash = PROTOCOL.ljust(32, b"\x00")
        else:
            initial_hash = sha256(PROTOCOL)

        self.ck = initial_hash
        self.h = initial_hash
        self.cipher = CipherState()

        # The Muse bundle performs the same explicit empty mixHash step after
        # initialization.
        self.mix_hash(b"")

    def mix_hash(self, data: bytes) -> None:
        self.h = sha256(self.h + data)

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = hkdf_noise(self.ck, ikm, 2)
        self.cipher = CipherState(temp_k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = hkdf_noise(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


class NoiseXXInitiator:
    def __init__(self) -> None:
        self.symmetric = SymmetricState()
        self.e_priv: X25519PrivateKey | None = None
        self.s_priv: X25519PrivateKey | None = None
        self.re: bytes | None = None
        self.rs: bytes | None = None
        self.phase = 1

    @staticmethod
    def _pub_bytes(priv: X25519PrivateKey) -> bytes:
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @staticmethod
    def _dh(priv: X25519PrivateKey, peer_raw: bytes) -> bytes:
        if len(peer_raw) != 32:
            raise ValueError("X25519 public key must be 32 bytes")
        out = priv.exchange(X25519PublicKey.from_public_bytes(peer_raw))
        if out == b"\x00" * 32:
            raise ValueError("X25519 produced all-zero output")
        return out

    def write_message1(self, payload: bytes = b"") -> bytes:
        if self.phase != 1:
            raise RuntimeError("write_message1 in wrong phase")
        self.e_priv = X25519PrivateKey.generate()
        e_pub = self._pub_bytes(self.e_priv)
        self.symmetric.mix_hash(e_pub)
        encrypted_payload = self.symmetric.encrypt_and_hash(payload)
        self.phase = 2
        return e_pub + encrypted_payload

    def read_message2(self, msg: bytes) -> bytes:
        if self.phase != 2:
            raise RuntimeError("read_message2 in wrong phase")
        if len(msg) < 96:
            raise ValueError(f"Noise message2 too short: {len(msg)}")
        assert self.e_priv is not None
        offset = 0
        self.re = msg[offset:offset + 32]
        offset += 32
        self.symmetric.mix_hash(self.re)
        self.symmetric.mix_key(self._dh(self.e_priv, self.re))  # ee
        self.rs = self.symmetric.decrypt_and_hash(msg[offset:offset + 48])
        offset += 48
        self.symmetric.mix_key(self._dh(self.e_priv, self.rs))  # es
        payload = self.symmetric.decrypt_and_hash(msg[offset:])
        self.phase = 3
        return payload

    def write_message3(self, payload: bytes = b"") -> bytes:
        if self.phase != 3:
            raise RuntimeError("write_message3 in wrong phase")
        if self.re is None:
            raise RuntimeError("missing responder ephemeral key")
        self.s_priv = X25519PrivateKey.generate()
        s_pub = self._pub_bytes(self.s_priv)
        encrypted_static = self.symmetric.encrypt_and_hash(s_pub)
        self.symmetric.mix_key(self._dh(self.s_priv, self.re))  # se
        encrypted_payload = self.symmetric.encrypt_and_hash(payload)
        self.phase = 4
        return encrypted_static + encrypted_payload

    def split(self) -> tuple[CipherState, CipherState]:
        if self.phase != 4:
            raise RuntimeError("split in wrong phase")
        self.phase = 5
        return self.symmetric.split()

    @property
    def handshake_hash(self) -> bytes:
        return bytes(self.symmetric.h)

    @property
    def remote_static_public_key(self) -> bytes | None:
        return self.rs


def encode_client_nonce_message1(nonce: bytes) -> bytes:
    # protobuf field 1, bytes: tag 0x0a followed by 32-byte length.
    if len(nonce) != 32:
        raise ValueError("client nonce must be exactly 32 bytes")
    return b"\x0a\x20" + nonce


def make_message1_payload() -> tuple[bytes, bytes]:
    nonce = os.urandom(32)
    return nonce, encode_client_nonce_message1(nonce)
