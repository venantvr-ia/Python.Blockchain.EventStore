"""SQL event store with N-back hash chaining and peer attestation.

Public surface:

* `SQLEventStore` — open or create the SQLite log, commit prepared
  events under an exclusive transaction, verify integrity end-to-end.
* `PreparedEvent` — an event built locally by an issuer, ready to be
  attested by peers and then committed.
* `StoredEvent` — an event read back from the database.
* `HLCClock` — monotonic hybrid logical clock used by issuers.
* `compute_content_hash`, `compute_row_hash`, `pad_parent_hashes`,
  `GENESIS_PAD` — primitives that callers (peers, audit code) use to
  re-derive the same hashes as the store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .crypto import KeyPair, verify_signature
from .exceptions import (
    HashChainError,
    IntegrityError,
    IssuerError,
    NonceError,
    PeerError,
    QuorumError,
    SchemaError,
    WriteProtectionError,
)
from .schema import all_statements

GENESIS_PAD = "0" * 64


# ----------------------------------------------------------------------- HLC


class HLCClock:
    """Hybrid Logical Clock — monotonic across system time jitter.

    Each `tick()` returns `(physical_ms, logical)` strictly greater than
    the previous one. If wall-clock time goes backward (NTP step,
    suspend/resume), the logical counter increments to preserve
    monotonicity. Persist the clock between restarts to avoid issuing
    events with a physical_ms below an already-committed event.
    """

    def __init__(self, *, physical_ms: int = 0, logical: int = 0) -> None:
        self._physical_ms = physical_ms
        self._logical = logical
        self._lock = threading.Lock()

    def tick(self) -> tuple[int, int]:
        with self._lock:
            now_ms = int(time.time() * 1000)
            if now_ms > self._physical_ms:
                self._physical_ms = now_ms
                self._logical = 0
            else:
                self._logical += 1
            return self._physical_ms, self._logical

    def state(self) -> tuple[int, int]:
        with self._lock:
            return self._physical_ms, self._logical


# ---------------------------------------------------------------- helpers


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_content_hash(
    *,
    created_at: float,
    hlc_physical_ms: int,
    hlc_logical: int,
    issuer_id: str,
    event_type: str,
    nonce: int,
    payload: dict,
) -> str:
    """SHA-256 over the canonical body. Independent of chain position."""
    body = {
        "created_at": created_at,
        "hlc_physical_ms": hlc_physical_ms,
        "hlc_logical": hlc_logical,
        "issuer_id": issuer_id,
        "event_type": event_type,
        "nonce": nonce,
        "payload": payload,
    }
    return hashlib.sha256(_canonical_json(body)).hexdigest()


def compute_row_hash(content_hash: str, parent_hashes: list[str]) -> str:
    """SHA-256( content_hash || concat(parent_hashes) ) — the chain link."""
    h = hashlib.sha256()
    h.update(content_hash.encode("ascii"))
    for p in parent_hashes:
        h.update(p.encode("ascii"))
    return h.hexdigest()


def pad_parent_hashes(recent_row_hashes: list[str], depth: int) -> list[str]:
    """Pad with GENESIS_PAD so the parent list always has `depth` entries.

    `recent_row_hashes` must be ordered most-recent first.
    """
    parents = list(recent_row_hashes[:depth])
    while len(parents) < depth:
        parents.append(GENESIS_PAD)
    return parents


# ---------------------------------------------------------------- dataclasses


@dataclass
class PreparedEvent:
    """An event built locally, signed by its issuer, awaiting peer sigs."""

    created_at: float
    hlc_physical_ms: int
    hlc_logical: int
    issuer_id: str
    event_type: str
    nonce: int
    payload: dict
    parent_hashes: list[str]
    content_hash: str
    row_hash: str
    issuer_sig: str
    peer_sigs: dict[str, str] = field(default_factory=dict)


@dataclass
class StoredEvent:
    """An event read back from the database."""

    id: int
    created_at: float
    hlc_physical_ms: int
    hlc_logical: int
    issuer_id: str
    event_type: str
    nonce: int
    payload: dict
    parent_hashes: list[str]
    content_hash: str
    row_hash: str
    issuer_sig: str
    peer_sigs: dict[str, str]


# ---------------------------------------------------------------- store


class SQLEventStore:
    """SQLite-backed append-only event store with peer attestation."""

    def __init__(
        self,
        db_path: str,
        *,
        hash_depth: int = 4,
        peer_quorum: int = 3,
        check_skew: bool = False,
        max_past_skew_ms: int = 5_000,
        max_future_skew_ms: int = 1_000,
    ) -> None:
        if hash_depth < 1:
            raise ValueError("hash_depth must be >= 1")
        if peer_quorum < 1:
            raise ValueError("peer_quorum must be >= 1")
        self.db_path = db_path
        self.hash_depth = hash_depth
        self.peer_quorum = peer_quorum
        self.check_skew = check_skew
        self.max_past_skew_ms = max_past_skew_ms
        self.max_future_skew_ms = max_future_skew_ms
        self._write_lock = threading.Lock()

    # ---------- connection management

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    @contextmanager
    def _exclusive(self) -> Iterator[sqlite3.Connection]:
        """Open a connection holding an exclusive write transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    # ---------- schema

    def initialize(self) -> None:
        conn = self._connect()
        try:
            for stmt in all_statements():
                conn.execute(stmt)
        except sqlite3.Error as exc:
            raise SchemaError(str(exc)) from exc
        finally:
            conn.close()

    # ---------- peers

    def register_peer(self, peer_id: str, public_key_hex: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO peers(peer_id, public_key_hex, registered_at) VALUES (?, ?, ?)",
                (peer_id, public_key_hex, time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise PeerError(f"cannot register peer {peer_id}: {exc}") from exc
        finally:
            conn.close()

    def get_peer_pubkey(self, peer_id: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT public_key_hex FROM peers WHERE peer_id = ?", (peer_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def list_peers(self) -> dict[str, str]:
        conn = self._connect()
        try:
            return {
                pid: pk
                for pid, pk in conn.execute(
                    "SELECT peer_id, public_key_hex FROM peers"
                )
            }
        finally:
            conn.close()

    # ---------- chain head accessors

    def height(self) -> int:
        conn = self._connect()
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
            return int(n)
        finally:
            conn.close()

    def _head_parents(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT row_hash FROM events ORDER BY id DESC LIMIT ?",
            (self.hash_depth,),
        ).fetchall()
        return pad_parent_hashes([r[0] for r in rows], self.hash_depth)

    def _next_nonce(self, conn: sqlite3.Connection, issuer_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(nonce), -1) FROM events WHERE issuer_id = ?",
            (issuer_id,),
        ).fetchone()
        return int(row[0]) + 1

    def _head_parents_snapshot(self) -> list[str]:
        """Read the current head parents through a fresh connection."""
        conn = self._connect()
        try:
            return self._head_parents(conn)
        finally:
            conn.close()

    def _next_nonce_snapshot(self, issuer_id: str) -> int:
        conn = self._connect()
        try:
            return self._next_nonce(conn, issuer_id)
        finally:
            conn.close()

    # ---------- prepare

    def prepare_event(
        self,
        *,
        issuer_id: str,
        issuer_keypair: KeyPair,
        event_type: str,
        payload: dict,
        hlc_clock: HLCClock | None = None,
    ) -> PreparedEvent:
        """Build a PreparedEvent: read head, compute hashes, sign content."""
        if self.get_peer_pubkey(issuer_id) is None:
            raise IssuerError(f"unknown issuer {issuer_id!r}")

        clock = hlc_clock or HLCClock()
        physical_ms, logical = clock.tick()

        if self.check_skew:
            now_ms = int(time.time() * 1000)
            if physical_ms < now_ms - self.max_past_skew_ms:
                raise IssuerError("HLC physical_ms is too far in the past")
            if physical_ms > now_ms + self.max_future_skew_ms:
                raise IssuerError("HLC physical_ms is too far in the future")

        conn = self._connect()
        try:
            parent_hashes = self._head_parents(conn)
            nonce = self._next_nonce(conn, issuer_id)
        finally:
            conn.close()

        created_at = time.time()
        content_hash = compute_content_hash(
            created_at=created_at,
            hlc_physical_ms=physical_ms,
            hlc_logical=logical,
            issuer_id=issuer_id,
            event_type=event_type,
            nonce=nonce,
            payload=payload,
        )
        row_hash = compute_row_hash(content_hash, parent_hashes)
        issuer_sig = issuer_keypair.sign(content_hash.encode("utf-8"))

        return PreparedEvent(
            created_at=created_at,
            hlc_physical_ms=physical_ms,
            hlc_logical=logical,
            issuer_id=issuer_id,
            event_type=event_type,
            nonce=nonce,
            payload=payload,
            parent_hashes=parent_hashes,
            content_hash=content_hash,
            row_hash=row_hash,
            issuer_sig=issuer_sig,
            peer_sigs={},
        )

    # ---------- commit

    def commit(self, prepared: PreparedEvent) -> int:
        with self._write_lock, self._exclusive() as conn:
            return self._insert_one(conn, prepared, rebranch=True)

    def commit_batch(self, prepared_events: list[PreparedEvent]) -> list[int]:
        """Commit several prepared events atomically, sorted by HLC.

        The sequencer reorders the batch by `(hlc_physical_ms,
        hlc_logical)` and re-derives every `row_hash` against the live
        head — so signatures (which cover `content_hash`) survive.
        """
        if not prepared_events:
            return []
        ordered = sorted(
            prepared_events, key=lambda p: (p.hlc_physical_ms, p.hlc_logical)
        )
        with self._write_lock, self._exclusive() as conn:
            return [self._insert_one(conn, p, rebranch=True) for p in ordered]

    def _insert_one(
        self,
        conn: sqlite3.Connection,
        prepared: PreparedEvent,
        *,
        rebranch: bool,
    ) -> int:
        # 1. Issuer must be a registered peer.
        issuer_pk = self._row_pubkey(conn, prepared.issuer_id)
        if issuer_pk is None:
            raise IssuerError(f"issuer {prepared.issuer_id!r} not registered")

        # 2. Issuer signature must verify the *content_hash*.
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
            raise HashChainError("content_hash does not match the canonical body")
        if not verify_signature(
            issuer_pk, prepared.issuer_sig, prepared.content_hash.encode("utf-8")
        ):
            raise IssuerError("issuer signature does not verify")

        # 3. Re-read parents under the exclusive lock and rebranch.
        live_parents = self._head_parents(conn)
        if rebranch:
            prepared.parent_hashes = live_parents
            prepared.row_hash = compute_row_hash(
                prepared.content_hash, prepared.parent_hashes
            )
        elif prepared.parent_hashes != live_parents:
            raise HashChainError("head moved since prepare_event")

        # 4. Re-check the nonce.
        expected_nonce = self._next_nonce(conn, prepared.issuer_id)
        if prepared.nonce != expected_nonce:
            raise NonceError(
                f"nonce mismatch for {prepared.issuer_id}: got "
                f"{prepared.nonce}, expected {expected_nonce}"
            )

        # 5. Validate peer signatures against registered peers.
        valid_peers = 0
        msg = prepared.content_hash.encode("utf-8")
        for peer_id, sig in prepared.peer_sigs.items():
            peer_pk = self._row_pubkey(conn, peer_id)
            if peer_pk is None:
                raise PeerError(f"unknown peer {peer_id!r} in attestations")
            if not verify_signature(peer_pk, sig, msg):
                # A registered peer with a bad signature is hostile.
                raise PeerError(f"invalid signature from peer {peer_id!r}")
            valid_peers += 1
        if valid_peers < self.peer_quorum:
            raise QuorumError(
                f"only {valid_peers} valid peer signatures, need {self.peer_quorum}"
            )

        # 6. Insert.
        try:
            cur = conn.execute(
                """
                INSERT INTO events(
                    created_at, hlc_physical_ms, hlc_logical,
                    issuer_id, event_type, nonce, payload,
                    parent_hashes, content_hash, row_hash,
                    issuer_sig, peer_sigs
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    prepared.created_at,
                    prepared.hlc_physical_ms,
                    prepared.hlc_logical,
                    prepared.issuer_id,
                    prepared.event_type,
                    prepared.nonce,
                    json.dumps(prepared.payload, sort_keys=True),
                    json.dumps(prepared.parent_hashes),
                    prepared.content_hash,
                    prepared.row_hash,
                    prepared.issuer_sig,
                    json.dumps(prepared.peer_sigs, sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "append-only" in msg:
                raise WriteProtectionError(str(exc)) from exc
            if "issuer_id" in msg and "nonce" in msg:
                raise NonceError(str(exc)) from exc
            raise HashChainError(str(exc)) from exc
        return int(cur.lastrowid)

    @staticmethod
    def _row_pubkey(conn: sqlite3.Connection, peer_id: str) -> str | None:
        row = conn.execute(
            "SELECT public_key_hex FROM peers WHERE peer_id = ?", (peer_id,)
        ).fetchone()
        return row[0] if row else None

    # ---------- read

    @staticmethod
    def _row_to_event(row: sqlite3.Row | tuple) -> StoredEvent:
        (
            id_,
            created_at,
            hlc_physical_ms,
            hlc_logical,
            issuer_id,
            event_type,
            nonce,
            payload,
            parent_hashes,
            content_hash,
            row_hash,
            issuer_sig,
            peer_sigs,
        ) = row
        return StoredEvent(
            id=id_,
            created_at=created_at,
            hlc_physical_ms=hlc_physical_ms,
            hlc_logical=hlc_logical,
            issuer_id=issuer_id,
            event_type=event_type,
            nonce=nonce,
            payload=json.loads(payload),
            parent_hashes=json.loads(parent_hashes),
            content_hash=content_hash,
            row_hash=row_hash,
            issuer_sig=issuer_sig,
            peer_sigs=json.loads(peer_sigs),
        )

    _SELECT_COLS = (
        "id, created_at, hlc_physical_ms, hlc_logical, issuer_id, event_type, "
        "nonce, payload, parent_hashes, content_hash, row_hash, issuer_sig, peer_sigs"
    )

    def read_all(self) -> Iterable[StoredEvent]:
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM events ORDER BY id ASC"
            )
            return [self._row_to_event(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def read_in_emission_order(self) -> Iterable[StoredEvent]:
        conn = self._connect()
        try:
            cur = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM events "
                "ORDER BY hlc_physical_ms ASC, hlc_logical ASC"
            )
            return [self._row_to_event(r) for r in cur.fetchall()]
        finally:
            conn.close()

    # ---------- integrity

    def verify_integrity(self) -> None:
        """Re-derive every hash and re-verify every signature on disk.

        Raises `IntegrityError` at the first row that fails any check.
        Designed to run periodically (e.g. cron) and on every audit.
        """
        peers = self.list_peers()
        running_parents: list[str] = []  # most recent first

        for ev in self.read_all():
            expected_parents = pad_parent_hashes(running_parents, self.hash_depth)
            if ev.parent_hashes != expected_parents:
                raise IntegrityError(
                    f"row {ev.id}: parent_hashes do not match the live chain"
                )

            recomputed_content = compute_content_hash(
                created_at=ev.created_at,
                hlc_physical_ms=ev.hlc_physical_ms,
                hlc_logical=ev.hlc_logical,
                issuer_id=ev.issuer_id,
                event_type=ev.event_type,
                nonce=ev.nonce,
                payload=ev.payload,
            )
            if recomputed_content != ev.content_hash:
                raise IntegrityError(f"row {ev.id}: content_hash mismatch")

            recomputed_row = compute_row_hash(ev.content_hash, ev.parent_hashes)
            if recomputed_row != ev.row_hash:
                raise IntegrityError(f"row {ev.id}: row_hash mismatch")

            issuer_pk = peers.get(ev.issuer_id)
            if issuer_pk is None:
                raise IntegrityError(f"row {ev.id}: issuer {ev.issuer_id} unknown")
            msg = ev.content_hash.encode("utf-8")
            if not verify_signature(issuer_pk, ev.issuer_sig, msg):
                raise IntegrityError(f"row {ev.id}: issuer signature invalid")

            valid_peer_sigs = 0
            for peer_id, sig in ev.peer_sigs.items():
                peer_pk = peers.get(peer_id)
                if peer_pk is None:
                    raise IntegrityError(
                        f"row {ev.id}: attestation from unknown peer {peer_id}"
                    )
                if verify_signature(peer_pk, sig, msg):
                    valid_peer_sigs += 1
                else:
                    raise IntegrityError(
                        f"row {ev.id}: invalid attestation from {peer_id}"
                    )
            if valid_peer_sigs < self.peer_quorum:
                raise IntegrityError(
                    f"row {ev.id}: only {valid_peer_sigs} valid attestations, "
                    f"need {self.peer_quorum}"
                )

            running_parents.insert(0, ev.row_hash)
            running_parents = running_parents[: self.hash_depth]
