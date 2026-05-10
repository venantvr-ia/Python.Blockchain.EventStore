"""Client (a.k.a. peer) — issuer + validator wrapped in one object.

Each client holds an Ed25519 key pair and a reference to the shared
store. They issue events on their own behalf and attest events
prepared by other clients.

The validator role is the security-critical one. `attest()` re-derives
the content_hash from scratch, re-reads the live parent_hashes from
its own view of the database, re-checks the nonce, and only signs
once everything matches. A client that signs blindly is the weakest
link of the quorum — see SQL_EVENTSTORE.md §4.2.
"""

from __future__ import annotations

from .crypto import KeyPair, verify_signature
from .exceptions import HashChainError, IssuerError, NonceError
from .store import (
    HLCClock,
    PreparedEvent,
    SQLEventStore,
    compute_content_hash,
    compute_row_hash,
)


class Client:
    """A peer in the quorum: holds a key pair, issues, and attests."""

    def __init__(
        self,
        client_id: str,
        keypair: KeyPair,
        store: SQLEventStore,
        *,
        hlc_clock: HLCClock | None = None,
    ) -> None:
        self.client_id = client_id
        self.keypair = keypair
        self.store = store
        self.hlc_clock = hlc_clock or HLCClock()

    # Convenience for demos: name == peer_id, surfaced as `peer_id`.
    @property
    def peer_id(self) -> str:
        return self.client_id

    def public_key_hex(self) -> str:
        return self.keypair.public_key_hex

    # ------------------------------------------------------- issuer

    def prepare(
        self,
        *,
        event_type: str,
        payload: dict,
    ) -> PreparedEvent:
        """Read head, build the canonical body, sign content_hash."""
        return self.store.prepare_event(
            issuer_id=self.client_id,
            issuer_keypair=self.keypair,
            event_type=event_type,
            payload=payload,
            hlc_clock=self.hlc_clock,
        )

    # ------------------------------------------------------- validator

    def attest(
        self,
        prepared: PreparedEvent,
        *,
        issuer_public_key: str,
    ) -> str:
        """Re-validate the prepared event and return a peer signature.

        Returns the hex-encoded signature over `content_hash`. Raises
        if any check fails — never sign what you can't verify.
        """
        # 1. Recompute content_hash from the body.
        recomputed_content = compute_content_hash(
            created_at=prepared.created_at,
            hlc_physical_ms=prepared.hlc_physical_ms,
            hlc_logical=prepared.hlc_logical,
            issuer_id=prepared.issuer_id,
            event_type=prepared.event_type,
            nonce=prepared.nonce,
            payload=prepared.payload,
        )
        if recomputed_content != prepared.content_hash:
            raise HashChainError("content_hash does not match the body")

        # 2. Verify the issuer signature on content_hash.
        msg = prepared.content_hash.encode("utf-8")
        if not verify_signature(issuer_public_key, prepared.issuer_sig, msg):
            raise IssuerError("issuer signature does not verify")

        # 3. Read parent_hashes from our own view of the DB and compare.
        live_parents = self.store._head_parents_snapshot()
        if prepared.parent_hashes != live_parents:
            raise HashChainError(
                "prepared parent_hashes diverge from this client's view"
            )

        # 4. Recompute row_hash with those parents.
        recomputed_row = compute_row_hash(
            prepared.content_hash, prepared.parent_hashes
        )
        if recomputed_row != prepared.row_hash:
            raise HashChainError("row_hash does not match content+parents")

        # 5. Check the issuer nonce is the next expected one.
        expected_nonce = self.store._next_nonce_snapshot(prepared.issuer_id)
        if prepared.nonce != expected_nonce:
            raise NonceError(
                f"expected nonce {expected_nonce} for {prepared.issuer_id}, "
                f"got {prepared.nonce}"
            )

        # 6. All checks passed: sign the *content_hash*.
        return self.keypair.sign(msg)
