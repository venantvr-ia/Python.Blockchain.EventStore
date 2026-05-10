"""SQL event store with N-back hash chaining and peer attestation.

Public API:

* `SQLEventStore`, `PreparedEvent`, `StoredEvent` — the store itself.
* `Client` — peer + issuer wrapper with `prepare()` and `attest()`.
* `KeyPair`, `verify_signature` — Ed25519 helpers.
* `HLCClock` — monotonic hybrid logical clock.
* `compute_content_hash`, `compute_row_hash`, `pad_parent_hashes`,
  `GENESIS_PAD` — re-derivation primitives used by audits.
* Exceptions — see `event_store.exceptions`.
"""

from .client import Client
from .crypto import KeyPair, verify_signature
from .exceptions import (
    EventStoreError,
    HashChainError,
    IntegrityError,
    IssuerError,
    NonceError,
    PeerError,
    QuorumError,
    SchemaError,
    WriteProtectionError,
)
from .store import (
    GENESIS_PAD,
    HLCClock,
    PreparedEvent,
    SQLEventStore,
    StoredEvent,
    compute_content_hash,
    compute_row_hash,
    pad_parent_hashes,
)

__all__ = [
    "SQLEventStore",
    "PreparedEvent",
    "StoredEvent",
    "Client",
    "KeyPair",
    "verify_signature",
    "HLCClock",
    "GENESIS_PAD",
    "compute_content_hash",
    "compute_row_hash",
    "pad_parent_hashes",
    "EventStoreError",
    "HashChainError",
    "IntegrityError",
    "IssuerError",
    "NonceError",
    "PeerError",
    "QuorumError",
    "SchemaError",
    "WriteProtectionError",
]
