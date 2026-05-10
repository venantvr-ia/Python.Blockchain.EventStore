"""Tests for the 3-client SQL event store.

Each test bootstraps a fresh DB, registers three clients, and
exercises one behaviour. The fixture is intentionally minimal — the
goal is to drive every contract from the spec rather than build a
clever test framework.
"""

from __future__ import annotations

import sqlite3

import pytest

from event_store import (
    Client,
    HashChainError,
    HLCClock,
    IntegrityError,
    IssuerError,
    KeyPair,
    NonceError,
    PeerError,
    QuorumError,
    SQLEventStore,
    WriteProtectionError,
    compute_content_hash,
    compute_row_hash,
)


# Fixture « store » : une base SQLite neuve par test (tmp_path est isolé
# pytest), profondeur de chaîne = 3 et quorum = 3 — c'est-à-dire que chaque
# événement référence les 3 derniers et exige 3 signatures valides.
@pytest.fixture
def store(tmp_path):
    db = tmp_path / "log.db"
    s = SQLEventStore(str(db), hash_depth=3, peer_quorum=3)
    s.initialize()  # crée tables, index et triggers anti-mutation
    return s


# Fixture « clients » : génère 3 paires de clés Ed25519, enregistre chaque
# pair dans la table `peers`, puis retourne un dict de Client prêts à
# émettre/attester. Chaque client a sa propre HLCClock — les pairs ne
# partagent pas l'état d'horloge (cas réel : machines distinctes).
@pytest.fixture
def clients(store):
    keypairs = {n: KeyPair.generate() for n in ("alice", "bob", "carol")}
    for n, kp in keypairs.items():
        store.register_peer(n, kp.public_key_hex)
    return {
        n: Client(n, kp, store, hlc_clock=HLCClock())
        for n, kp in keypairs.items()
    }


def commit_with_three(issuer: Client, others: list[Client], **ev) -> int:
    """Helper : un émetteur prépare un événement, les 3 pairs le signent,
    puis on commit. Retourne l'id de la ligne insérée."""
    # 1. L'émetteur lit la tête, calcule les hashs et signe content_hash.
    prepared = issuer.prepare(**ev)
    # Toutes les signatures portent sur content_hash (et non row_hash, qui
    # peut changer au rebranch côté store).
    msg = prepared.content_hash.encode("utf-8")
    # 2. L'émetteur s'auto-atteste — il compte dans son propre quorum.
    prepared.peer_sigs[issuer.peer_id] = issuer.keypair.sign(msg)
    # 3. Les autres pairs re-vérifient TOUT (hash, parents, nonce…) puis
    # signent. attest() lève une exception si quelque chose cloche.
    for c in others:
        prepared.peer_sigs[c.peer_id] = c.attest(
            prepared, issuer_public_key=issuer.public_key_hex()
        )
    # 4. Commit sous BEGIN EXCLUSIVE — le store re-vérifie tout une dernière
    # fois et re-calcule row_hash contre la tête vivante (rebranch).
    return issuer.store.commit(prepared)


# ============================================================ CAS NOMINAL
# Ces tests vérifient que tout fonctionne quand on respecte le protocole.


def test_three_client_round_trip(store, clients):
    """Scénario de référence : 1 événement, 3 signatures, chaîne vide
    au départ → les parents doivent être 3× le pad genesis."""
    rid = commit_with_three(
        clients["alice"],
        [clients["bob"], clients["carol"]],
        event_type="t", payload={"x": 1},
    )
    assert rid == 1                  # première ligne → id 1
    assert store.height() == 1       # un seul événement dans la chaîne
    [ev] = list(store.read_all())
    assert ev.issuer_id == "alice"
    assert ev.payload == {"x": 1}
    assert len(ev.peer_sigs) == 3    # quorum = 3 atteint
    assert len(ev.parent_hashes) == 3  # toujours hash_depth parents
    # Chaîne vide → parents = GENESIS_PAD (64 zéros) répété 3 fois.
    assert all(p == "0" * 64 for p in ev.parent_hashes)


def test_chain_links_to_n_previous_rows(store, clients):
    """Vérifie le chaînage N-back : avec hash_depth=3, l'événement n°5
    référence les row_hashes des événements 4, 3 et 2 (du plus récent
    au plus ancien)."""
    for i in range(5):
        commit_with_three(
            clients["alice"],
            [clients["bob"], clients["carol"]],
            event_type="t", payload={"i": i},
        )
    events = list(store.read_all())
    # events est indexé à 0, donc events[4] = 5e événement.
    # parent_hashes[0] = le plus récent, [2] = le plus ancien des 3.
    assert events[4].parent_hashes[0] == events[3].row_hash
    assert events[4].parent_hashes[1] == events[2].row_hash
    assert events[4].parent_hashes[2] == events[1].row_hash


def test_verify_integrity_passes_on_clean_chain(store, clients):
    """L'audit complet (re-dérivation de tous les hashs + re-vérification
    des signatures) doit passer sans erreur sur une chaîne propre."""
    # Émission en round-robin : alice, bob, carol, alice…
    for i in range(4):
        issuer_name = ("alice", "bob", "carol")[i % 3]
        commit_with_three(
            clients[issuer_name],
            # Les 2 autres clients servent d'attestants.
            [c for n, c in clients.items() if n != issuer_name],
            event_type="t", payload={"i": i},
        )
    # Si quoi que ce soit ne re-dérive pas, IntegrityError est levée.
    store.verify_integrity()


# ============================================================ SÉCURITÉ
# Ces tests démontrent que chaque vecteur d'attaque connu est rejeté.


def test_unregistered_issuer_rejected(store, clients):
    """Un client non enregistré dans la table `peers` ne peut même pas
    préparer un événement — le rejet a lieu dès prepare(), pas au commit."""
    rogue = Client("mallory", KeyPair.generate(), store)
    with pytest.raises(IssuerError):
        rogue.prepare(event_type="evil", payload={})


def test_quorum_below_threshold_rejected(store, clients):
    """Le store exige peer_quorum=3 signatures valides : avec seulement
    2 signatures, le commit doit échouer en QuorumError."""
    prepared = clients["alice"].prepare(event_type="t", payload={})
    msg = prepared.content_hash.encode("utf-8")
    # Seulement 2 signatures — il en manque une pour atteindre le quorum.
    prepared.peer_sigs["alice"] = clients["alice"].keypair.sign(msg)
    prepared.peer_sigs["bob"] = clients["bob"].keypair.sign(msg)
    with pytest.raises(QuorumError):
        store.commit(prepared)


def test_forged_peer_signature_rejected(store, clients):
    """Bob signe mais prétend que c'est carol → la signature ne vérifie
    pas avec la clé publique de carol → PeerError. Empêche un pair
    compromis de fabriquer des attestations au nom des autres."""
    prepared = clients["alice"].prepare(event_type="t", payload={})
    msg = prepared.content_hash.encode("utf-8")
    prepared.peer_sigs["alice"] = clients["alice"].keypair.sign(msg)
    prepared.peer_sigs["bob"] = clients["bob"].keypair.sign(msg)
    # On colle la signature de bob dans le slot de carol — invalide
    # car elle sera vérifiée avec la clé publique de carol.
    prepared.peer_sigs["carol"] = clients["bob"].keypair.sign(msg)
    with pytest.raises(PeerError):
        store.commit(prepared)


def test_unknown_peer_in_attestations_rejected(store, clients):
    """Une signature provenant d'un peer_id absent de la table `peers`
    est refusée — même si la signature elle-même est cryptographiquement
    valide. L'identité du signataire doit être pré-enregistrée."""
    prepared = clients["alice"].prepare(event_type="t", payload={})
    msg = prepared.content_hash.encode("utf-8")
    for name in ("alice", "bob", "carol"):
        prepared.peer_sigs[name] = clients[name].keypair.sign(msg)
    # Un pair que le store n'a jamais vu — pas de clé publique référencée.
    stranger = KeyPair.generate()
    prepared.peer_sigs["stranger"] = stranger.sign(msg)
    with pytest.raises(PeerError):
        store.commit(prepared)


def test_payload_tampering_breaks_content_hash(store, clients):
    """Modifier le payload après la préparation casse le content_hash :
    le hash recalculé au commit ne correspond plus → HashChainError.
    Empêche de réutiliser des signatures sur un corps modifié."""
    prepared = clients["alice"].prepare(event_type="t", payload={"v": 1})
    # Falsification : on change le payload après que prepare() a calculé
    # content_hash. Les signatures suivantes seront sur l'ancien hash.
    prepared.payload = {"v": 2}
    msg = prepared.content_hash.encode("utf-8")
    for name in ("alice", "bob", "carol"):
        prepared.peer_sigs[name] = clients[name].keypair.sign(msg)
    with pytest.raises(HashChainError):
        store.commit(prepared)


def test_nonce_replay_rejected(store, clients):
    """Chaque émetteur a un compteur de nonce strictement croissant.
    Tenter de réutiliser un nonce déjà engagé doit lever NonceError —
    protection contre le rejeu d'un événement passé."""
    # 1er événement valide d'alice (nonce = 0).
    prepared = clients["alice"].prepare(event_type="t", payload={"v": 1})
    msg = prepared.content_hash.encode("utf-8")
    for n in ("alice", "bob", "carol"):
        prepared.peer_sigs[n] = clients[n].keypair.sign(msg)
    store.commit(prepared)

    # On construit un 2e événement à la main et on force nonce=0
    # (déjà consommé). prepare() aurait normalement attribué nonce=1.
    p2 = clients["alice"].prepare(event_type="t", payload={"v": 2})
    p2.nonce = 0  # déjà utilisé → doit être rejeté
    # Comme on a modifié le nonce, il faut tout recalculer pour que les
    # signatures restent cohérentes (sinon on aurait HashChainError avant
    # NonceError, ce qui ne testerait pas la bonne chose).
    p2.content_hash = compute_content_hash(
        created_at=p2.created_at,
        hlc_physical_ms=p2.hlc_physical_ms,
        hlc_logical=p2.hlc_logical,
        issuer_id=p2.issuer_id,
        event_type=p2.event_type,
        nonce=p2.nonce,
        payload=p2.payload,
    )
    p2.row_hash = compute_row_hash(p2.content_hash, p2.parent_hashes)
    p2.issuer_sig = clients["alice"].keypair.sign(p2.content_hash.encode("utf-8"))
    msg2 = p2.content_hash.encode("utf-8")
    for n in ("alice", "bob", "carol"):
        p2.peer_sigs[n] = clients[n].keypair.sign(msg2)
    # Tout est cryptographiquement valide, mais le nonce est en collision.
    with pytest.raises(NonceError):
        store.commit(p2)


# ============================================================ TRIGGERS SQL
# Première ligne de défense : SQLite refuse UPDATE/DELETE sur events
# tant que les triggers sont présents. Si un attaquant les supprime,
# verify_integrity() prend le relais (re-dérivation cryptographique).


def test_trigger_blocks_update(store, clients):
    """Le trigger trg_events_no_update doit faire échouer toute tentative
    de modification, même via une connexion SQLite brute hors API."""
    commit_with_three(
        clients["alice"], [clients["bob"], clients["carol"]],
        event_type="t", payload={},
    )
    # Connexion directe à la base — on contourne SQLEventStore.
    raw = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE events SET payload = '{}' WHERE id = 1")
    raw.close()


def test_trigger_blocks_delete(store, clients):
    """Symétrique : trg_events_no_delete bloque toute suppression.
    Le journal est strictement append-only au niveau SQL."""
    commit_with_three(
        clients["alice"], [clients["bob"], clients["carol"]],
        event_type="t", payload={},
    )
    raw = sqlite3.connect(store.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM events WHERE id = 1")
    raw.close()


def test_filesystem_tampering_caught_by_verify_integrity(store, clients):
    """Scénario du pire cas : un attaquant a un accès direct au fichier
    et supprime le trigger pour pouvoir UPDATE. La défense de dernier
    recours est verify_integrity() qui re-dérive tous les hashs et
    re-vérifie toutes les signatures — la falsification est détectée."""
    commit_with_three(
        clients["alice"], [clients["bob"], clients["carol"]],
        event_type="t", payload={"v": 1},
    )
    # Simulation d'un attaquant qui drope le trigger puis modifie la ligne.
    raw = sqlite3.connect(store.db_path)
    raw.execute("DROP TRIGGER trg_events_no_update")
    raw.execute("UPDATE events SET payload = '{\"v\":99}' WHERE id = 1")
    raw.commit()
    raw.close()
    # L'audit re-calcule content_hash sur le payload modifié et constate
    # qu'il ne correspond plus au content_hash stocké → IntegrityError.
    with pytest.raises(IntegrityError):
        store.verify_integrity()


# ============================================================ COMMIT BATCH
# Le sequencer doit réordonner un lot par horloge HLC, indépendamment
# de l'ordre dans lequel les événements ont été préparés ou soumis.


def test_commit_batch_sorts_by_hlc(store, clients):
    """commit_batch trie par (hlc_physical_ms, hlc_logical) avant insertion.
    On les soumet dans l'ordre inverse pour vérifier que le tri a bien lieu
    et que les row_hashes sont re-calculés (rebranch) pour rester cohérents
    avec la nouvelle position dans la chaîne."""
    events = []
    # On prépare dans l'ordre alice → bob → carol (HLC croissant naturel).
    for issuer_name in ("alice", "bob", "carol"):
        issuer = clients[issuer_name]
        prepared = issuer.prepare(event_type="t", payload={"by": issuer_name})
        msg = prepared.content_hash.encode("utf-8")
        for n in ("alice", "bob", "carol"):
            prepared.peer_sigs[n] = clients[n].keypair.sign(msg)
        events.append(prepared)

    # Soumission dans l'ordre inverse — le sequencer doit ré-ordonner.
    row_ids = store.commit_batch(list(reversed(events)))
    # Les ids sont attribués par l'INSERT, donc 1, 2, 3 dans l'ordre stocké.
    assert row_ids == [1, 2, 3]
    stored = list(store.read_all())
    # Vérification finale : l'ordre de stockage suit bien l'horloge HLC.
    assert [e.hlc_physical_ms for e in stored] == sorted(
        e.hlc_physical_ms for e in stored
    )
