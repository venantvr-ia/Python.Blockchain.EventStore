"""Ed25519 key pair and signature helpers.

A thin, ergonomic wrapper over `cryptography.hazmat`. The rest of the
code only needs `KeyPair`, `verify_signature`, and the hex-encoded
representations.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


@dataclass(frozen=True)
class KeyPair:
    """An Ed25519 key pair held in memory.

    The private key is kept inside the dataclass so a client can sign
    locally without leaking it. The public key is also exposed in hex
    form for storage in the `peers` table.
    """

    _private: Ed25519PrivateKey
    _public: Ed25519PublicKey

    @classmethod
    def generate(cls) -> "KeyPair":
        priv = Ed25519PrivateKey.generate()
        return cls(priv, priv.public_key())

    @property
    def public_key_hex(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return raw.hex()

    def sign(self, message: bytes) -> str:
        """Return a hex-encoded Ed25519 signature."""
        return self._private.sign(message).hex()


def public_key_from_hex(public_key_hex: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))


def verify_signature(public_key_hex: str, signature_hex: str, message: bytes) -> bool:
    """Return True iff `signature_hex` is a valid Ed25519 sig over `message`."""
    try:
        pk = public_key_from_hex(public_key_hex)
        pk.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False
