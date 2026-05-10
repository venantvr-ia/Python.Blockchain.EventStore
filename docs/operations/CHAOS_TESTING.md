# Tests de chaos et fuzzing

## Problème

La suite de tests actuelle ([tests/test_event_store.py](../../tests/test_event_store.py)) couvre les chemins **prévus** : émission nominale, attaques connues (signature forgée, peer inconnu, replay de nonce). Elle ne couvre pas :

- les **combinaisons** d'incidents (panne disque + signature forgée simultanément) ;
- les **inputs aléatoires** (payloads malformés, JSON malicieux, séquences d'opérations dans tous les ordres possibles) ;
- les **invariants à long horizon** (la chaîne reste auditable après 10 000 commits aléatoires) ;
- les **comportements concurrents** (commits en parallèle, race entre `prepare` et `register_peer`).

Un journal inviolable doit l'être *en pratique*, pas seulement sur les tests qu'on a écrits.

## Options et tradeoffs

| Option | Idée | Couverture | Effort |
|---|---|---|---|
| **Property-based** (Hypothesis) | Génère des inputs aléatoires, réduit les contre-exemples | Très large | Moyen |
| **Fuzzing structurel** (atheris) | Mutation byte-level orientée par coverage | Bugs subtils | Fort |
| **Stateful testing** | Génère des séquences aléatoires d'appels API | Concurrence, transitions | Moyen-fort |
| **Chaos en environnement** (kill -9, full disk, clock skew) | Inject des pannes réelles en intégration | Réalisme prod | Fort |
| **Differential testing** | Compare le comportement à une référence | Détecte les régressions | Moyen |

## Recommandation

**Hypothesis (property-based + stateful) en CI**, complété par **chaos en intégration** sur les versions release.

Les deux niveaux capturent des classes de bugs différentes : Hypothesis détecte les bugs logiques en quelques minutes ; les tests de chaos en environnement révèlent les bugs liés au système (FS, kernel, ordonnancement).

## Propriétés à vérifier

Invariants formulables comme propriétés Hypothesis :

| Propriété | Invariant |
|---|---|
| **Audit après N commits** | Pour tout n, après n commits valides aléatoires, `verify_integrity()` passe |
| **Audit détecte toute mutation** | Pour tout `update SET col=...`, `verify_integrity()` lève `IntegrityError` |
| **Idempotence du commit** | Commit deux fois le même `PreparedEvent` → exactement un succès puis un rejet |
| **Monotonicité du nonce** | Pour tout émetteur, après n commits, son max(nonce) == n-1 |
| **Stabilité du content_hash** | `compute_content_hash` est déterministe pour le même body |
| **Quorum strict** | Avec `peer_quorum=k`, k-1 sigs valides → rejet ; k sigs valides → succès |
| **Order HLC** | `commit_batch` produit des events ordonnés par HLC croissant |

## Schéma proposé

```python
# tests/properties/test_audit_holds.py
from hypothesis import given, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant

class StoreMachine(RuleBasedStateMachine):
    """Modèle stateful : commits aléatoires + invariant verify_integrity OK."""

    def __init__(self):
        super().__init__()
        self.tmp = tempfile.mkdtemp()
        self.store = SQLEventStore(f"{self.tmp}/log.db", hash_depth=3, peer_quorum=3)
        self.store.initialize()
        self.peers = {n: KeyPair.generate() for n in ("alice", "bob", "carol")}
        for n, kp in self.peers.items():
            self.store.register_peer(n, kp.public_key_hex)
        self.clients = {n: Client(n, kp, self.store, hlc_clock=HLCClock())
                        for n, kp in self.peers.items()}

    @rule(
        issuer=st.sampled_from(["alice", "bob", "carol"]),
        event_type=st.text(min_size=1, max_size=20),
        payload=st.dictionaries(
            keys=st.text(min_size=1, max_size=10),
            values=st.one_of(st.integers(), st.text(), st.booleans()),
            max_size=5,
        ),
    )
    def emit(self, issuer, event_type, payload):
        commit_with_three(self.clients[issuer],
                          [c for n, c in self.clients.items() if n != issuer],
                          event_type=event_type, payload=payload)

    @invariant()
    def integrity_holds(self):
        self.store.verify_integrity()  # ne doit jamais lever

TestStoreMachine = StoreMachine.TestCase
```

Chaos d'intégration (test indépendant, lent) :

```python
# tests/chaos/test_kill_during_commit.py
def test_recovery_after_sigkill():
    """Tue le process pendant un commit ; au redémarrage, audit OK,
    pas de transaction partielle, le commit a soit complètement réussi
    soit complètement échoué."""
    pid = launch_committer_subprocess()
    time.sleep(random.uniform(0.001, 0.1))
    os.kill(pid, signal.SIGKILL)
    store = SQLEventStore(...)
    store.verify_integrity()  # doit passer
    # height() est soit n, soit n-1 — jamais entre les deux états
```

## Intégration au store actuel

- **Nouveau répertoire** `tests/properties/` (Hypothesis) et `tests/chaos/` (intégration).
- **Dépendances** : `hypothesis` en `dev_requires`, `pytest-xdist` pour paralléliser.
- **CI** : Hypothesis tourne à chaque PR (~1–5 min) ; chaos tourne nightly ou pré-release (~30 min).
- **Reproductibilité** : Hypothesis sauvegarde les contre-exemples dans `.hypothesis/` — committer pour avoir des régressions stables.

## Limites / risques

- **Faux négatifs** : Hypothesis n'explore qu'un sous-espace ; un bug rare peut passer entre les mailles. Compenser par du fuzzing en continu (24h/24 sur un nœud dédié).
- **Tests flaky** : les chaos tests dépendent du timing OS, donnant des résultats variables. Isoler dans une suite séparée, retry policy explicite (≤ 3 fois).
- **Coût CI** : un property test peut prendre des minutes. Limiter la profondeur (`max_examples=200` en CI, `1000` en nightly).
- **Couverture cryptographique** : Hypothesis ne génère pas de signatures cryptographiquement valides. Les attaques sur le crypto restent à tester explicitement (cf. tests existants).
- **Détermination des invariants** : un invariant trop strict produit du bruit, trop laxe rate des bugs. Démarrer avec ceux du tableau ci-dessus, étendre à mesure que des bugs sont découverts.
