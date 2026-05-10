"""End-to-end demo with 3 clients and a SQLite database.

Three peers (`alice`, `bob`, `carol`) bootstrap a shared SQL log with
`hash_depth=3` and `peer_quorum=3` — every commit needs all three
signatures.

The script walks through:

1. registering the three clients;
2. each client issues an event in turn, attested by the other two;
3. a 4th, unregistered client tries to issue — rejected;
4. one client tries to forge another's attestation — rejected;
5. the SQL triggers block UPDATE / DELETE statements;
6. raw filesystem tampering (drop trigger + UPDATE) is detected by
   `verify_integrity()`.

Run with::

    PYTHONPATH=. python demo.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from event_store import (
    Client,
    EventStoreError,
    HLCClock,
    KeyPair,
    SQLEventStore,
)


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def issue_with_quorum(
    issuer: Client,
    attesters: list[Client],
    *,
    event_type: str,
    payload: dict,
) -> int:
    """Issue an event, collect attestations from `attesters`, commit."""
    prepared = issuer.prepare(event_type=event_type, payload=payload)

    # The issuer attests their own event first.
    prepared.peer_sigs[issuer.peer_id] = issuer.keypair.sign(
        prepared.content_hash.encode("utf-8")
    )
    # Then every other client validates and signs.
    for c in attesters:
        sig = c.attest(prepared, issuer_public_key=issuer.public_key_hex())
        prepared.peer_sigs[c.peer_id] = sig

    return issuer.store.commit(prepared)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="evstore_3c_")
    db_path = os.path.join(tmp, "log.db")
    print(f"db: {db_path}")

    # ----------------------------------------------------------------- 1
    banner("Bootstrap: 3 clients, hash_depth=3, peer_quorum=3")
    store = SQLEventStore(db_path, hash_depth=3, peer_quorum=3)
    store.initialize()

    # Each client gets a key pair AND a shared HLC clock view (one per
    # client — they don't share clock state).
    keypairs = {name: KeyPair.generate() for name in ("alice", "bob", "carol")}
    for name, kp in keypairs.items():
        store.register_peer(name, kp.public_key_hex)

    clients = {
        name: Client(name, kp, store, hlc_clock=HLCClock())
        for name, kp in keypairs.items()
    }
    print(f"  registered: {sorted(clients)}")

    # ----------------------------------------------------------------- 2
    banner("alice issues an event; bob & carol attest")
    rid = issue_with_quorum(
        clients["alice"],
        [clients["bob"], clients["carol"]],
        event_type="account.opened",
        payload={"account": "ACC-001", "owner": "Alice"},
    )
    print(f"  committed row id={rid}, height={store.height()}")

    # ----------------------------------------------------------------- 3
    banner("bob issues an event; alice & carol attest")
    rid = issue_with_quorum(
        clients["bob"],
        [clients["alice"], clients["carol"]],
        event_type="deposit.made",
        payload={"account": "ACC-001", "amount_cents": 50_000},
    )
    print(f"  committed row id={rid}, height={store.height()}")

    # ----------------------------------------------------------------- 4
    banner("carol issues an event; alice & bob attest")
    rid = issue_with_quorum(
        clients["carol"],
        [clients["alice"], clients["bob"]],
        event_type="audit.read",
        payload={"account": "ACC-001", "purpose": "monthly review"},
    )
    print(f"  committed row id={rid}, height={store.height()}")

    # ----------------------------------------------------------------- 5
    banner("Inspect the chain — N-back parents are visible")
    for ev in store.read_all():
        parents = [p[:8] for p in ev.parent_hashes]
        print(
            f"  id={ev.id} issuer={ev.issuer_id:6} type={ev.event_type:18} "
            f"hash={ev.row_hash[:12]}  parents={parents}"
        )

    # ----------------------------------------------------------------- 6
    banner("Reject: an unregistered attacker tries to issue")
    attacker_kp = KeyPair.generate()
    rogue = Client("mallory", attacker_kp, store)
    try:
        rogue.prepare(event_type="evil", payload={})
        print("  UNEXPECTED: prepare succeeded for unknown issuer")
    except EventStoreError as exc:
        print(f"  rejected at prepare: {type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------- 7
    banner("Reject: bob forges carol's attestation")
    prepared = clients["alice"].prepare(
        event_type="transfer", payload={"from": "ACC-001", "to": "ACC-002"}
    )
    msg = prepared.content_hash.encode("utf-8")
    prepared.peer_sigs["alice"] = keypairs["alice"].sign(msg)
    prepared.peer_sigs["bob"] = keypairs["bob"].sign(msg)
    # Bob signs but pretends it comes from carol — invalid sig under
    # carol's public key.
    prepared.peer_sigs["carol"] = keypairs["bob"].sign(msg)
    try:
        store.commit(prepared)
        print("  UNEXPECTED: forged attestation accepted")
    except EventStoreError as exc:
        print(f"  rejected at commit: {type(exc).__name__}: {exc}")

    # ----------------------------------------------------------------- 8
    banner("SQL trigger blocks UPDATE/DELETE on events")
    raw = sqlite3.connect(db_path)
    for stmt in (
        "UPDATE events SET payload = '{}' WHERE id = 1",
        "DELETE FROM events WHERE id = 1",
    ):
        try:
            raw.execute(stmt)
            print(f"  UNEXPECTED: succeeded → {stmt}")
        except sqlite3.IntegrityError as exc:
            print(f"  blocked: {stmt!r} → {exc}")
    raw.close()

    # ----------------------------------------------------------------- 9
    banner("Filesystem tampering caught by verify_integrity()")
    raw = sqlite3.connect(db_path)
    raw.execute("DROP TRIGGER IF EXISTS trg_events_no_update")
    raw.execute("UPDATE events SET payload = '{\"tampered\":true}' WHERE id = 1")
    raw.commit()
    raw.close()
    try:
        store.verify_integrity()
        print("  UNEXPECTED: integrity passed despite tampering")
    except EventStoreError as exc:
        print(f"  caught: {type(exc).__name__}: {exc}")

    banner("Done")
    print(f"  db file: {db_path}")


if __name__ == "__main__":
    main()
