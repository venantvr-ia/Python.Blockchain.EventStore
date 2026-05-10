"""SQL DDL for the append-only event store.

The schema is split into:

* table `peers` — known signers (peer_id + public key hex);
* table `events` — append-only log with N-back parent_hashes;
* triggers — refuse UPDATE and DELETE on both tables.

The triggers are the *first* line of defence; they only block SQL
statements going through the engine. Filesystem-level tampering is
caught later by `SQLEventStore.verify_integrity()`.
"""

PEERS_DDL = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id        TEXT PRIMARY KEY,
    public_key_hex TEXT NOT NULL UNIQUE,
    registered_at  REAL NOT NULL
);
"""

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    hlc_physical_ms INTEGER NOT NULL,
    hlc_logical     INTEGER NOT NULL,
    issuer_id       TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    nonce           INTEGER NOT NULL,
    payload         TEXT    NOT NULL,
    parent_hashes   TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL UNIQUE,
    row_hash        TEXT    NOT NULL UNIQUE,
    issuer_sig      TEXT    NOT NULL,
    peer_sigs       TEXT    NOT NULL,
    UNIQUE(issuer_id, nonce),
    FOREIGN KEY (issuer_id) REFERENCES peers(peer_id)
);
"""

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_events_issuer_nonce ON events(issuer_id, nonce);",
    "CREATE INDEX IF NOT EXISTS idx_events_hlc ON events(hlc_physical_ms, hlc_logical);",
]

TRIGGERS_DDL = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_peers_no_update
    BEFORE UPDATE ON peers
    BEGIN
        SELECT RAISE(ABORT, 'peers table is append-only');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_peers_no_delete
    BEFORE DELETE ON peers
    BEGIN
        SELECT RAISE(ABORT, 'peers table is append-only');
    END;
    """,
]


def all_statements() -> list[str]:
    """Return every DDL statement in the order it must be executed."""
    return [PEERS_DDL, EVENTS_DDL, *INDEXES_DDL, *TRIGGERS_DDL]
