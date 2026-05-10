"""Domain exceptions for the SQL event store.

Each error class flags a specific failure mode so that callers can
react precisely instead of catching a generic exception.
"""


class EventStoreError(Exception):
    """Base error for the event store."""


class SchemaError(EventStoreError):
    """The SQL schema is missing, partial, or doesn't match expectations."""


class WriteProtectionError(EventStoreError):
    """An UPDATE or DELETE was attempted against the append-only log."""


class HashChainError(EventStoreError):
    """Parent hashes don't match the live head, or a row hash mismatches."""


class NonceError(EventStoreError):
    """Issuer nonce is not the next expected value (replay or gap)."""


class IssuerError(EventStoreError):
    """Issuer is unknown or its signature does not verify."""


class PeerError(EventStoreError):
    """A peer attestation is invalid or comes from an unknown peer."""


class QuorumError(EventStoreError):
    """Not enough valid peer signatures to reach the quorum threshold."""


class IntegrityError(EventStoreError):
    """An on-disk row failed cryptographic re-verification."""
