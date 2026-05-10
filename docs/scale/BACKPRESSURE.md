# Backpressure et rate limiting

## Problème

Aucun mécanisme ne protège le store d'un émetteur :

- **buggé** qui boucle et publie 10 000 events/seconde ;
- **compromis** qui veut saturer le quorum pour bloquer les autres émetteurs (DoS interne) ;
- **honnête mais explosif** qui produit un burst pendant un incident métier.

Le `_write_lock` ([event_store/store.py:194](../../event_store/store.py#L194)) sérialise tout — un émetteur abusif peut littéralement empêcher les autres d'écrire.

## Options et tradeoffs

| Option | Idée | Détection | Action |
|---|---|---|---|
| **Hard limit global** | Plafond de commit/seconde au niveau store | Mesure simple | Refus tout-ou-rien, frappe les honnêtes aussi |
| **Quota par émetteur** | Plafond N events/intervalle T par `issuer_id` | Compteur sliding window | Refus à `prepare()` quand dépassé |
| **Token bucket** | Chaque émetteur a un seau de jetons, recharge à débit constant | Lissage des bursts autorisé | Pénalise les bursts persistants, pas ponctuels |
| **Priority queue** | Plusieurs classes de service (haute / basse) | Configuration métier | Plus complexe, mais critique préservé |

## Recommandation

**Token bucket par `issuer_id`**, configuré à l'enregistrement du peer. Permet les bursts tout en plafonnant le débit moyen — plus juste qu'un hard limit, plus facile à régler qu'une priority queue.

Au dépassement : émission d'un événement `quota.exceeded` (typé, traçable, alertable) plutôt qu'un refus silencieux. L'émetteur sait pourquoi il est rejeté et le superviseur le voit.

```mermaid
flowchart TB
    P[prepare()] --> CHK{Token<br/>disponible<br/>pour issuer ?}
    CHK -- oui --> CONS[Consommer 1 token] --> OK[PreparedEvent]
    CHK -- non --> Q[Émettre quota.exceeded<br/>par un peer admin]
    Q --> ERR[QuotaError côté caller]
```

## Schéma proposé

Une table `peer_quotas` (ou des champs dans `peers`) :

```sql
ALTER TABLE peers ADD COLUMN bucket_capacity INTEGER NOT NULL DEFAULT 100;
ALTER TABLE peers ADD COLUMN bucket_refill_per_sec REAL NOT NULL DEFAULT 10.0;
```

Token bucket en mémoire (mis à jour au commit) :

```python
class TokenBucket:
    def __init__(self, capacity, refill_rate):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last = time.monotonic()

    def try_consume(self, n=1) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill_rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

# Dans SQLEventStore.prepare_event:
if not self._buckets[issuer_id].try_consume():
    raise QuotaError(f"rate limit for {issuer_id}")
```

Et un événement `quota.exceeded` émis par un peer admin :

```python
admin.prepare(
    event_type="quota.exceeded",
    payload={"issuer_id": "alice", "seen_qps": 250.0, "limit_qps": 10.0},
)
```

## Intégration au store actuel

- **Fichier touché** : [event_store/store.py](../../event_store/store.py) — ajout de `_buckets` (dict en mémoire), check dans `prepare_event` et `commit`.
- **Schéma** : champs additifs sur `peers`, défauts permissifs.
- **Persistance des buckets** : pas indispensable. Au redémarrage, chaque bucket repart plein → un attaquant gagne au plus une fenêtre, ce qui reste raisonnable.
- **Interaction avec le sharding** ([SHARDING.md](SHARDING.md)) : les buckets sont par `issuer_id`, donc sharding-agnostiques. Ils suivent l'émetteur sur son shard.

## Limites / risques

- **Quotas trop bas = faux positifs** : un service métier légitime mais bursty (rapport mensuel, batch de bascule) sera bloqué. Prévoir un événement `quota.adjusted{issuer_id, new_capacity}` pour ajuster sans redémarrage.
- **Émetteurs partageant un peer_id** : si plusieurs services s'identifient comme `alice`, ils se partagent le bucket. Recommander un peer_id par instance (`alice@host01`).
- **DoS au niveau prepare** : `prepare()` est cher (lecture de la tête). Un émetteur peut faire tomber le store en spammant `prepare` même si le commit échoue. Mitigation : cap aussi sur `prepare_event` côté store, ou rate limit en amont (reverse proxy).
- **Coordination multi-process** : si plusieurs processus partagent le `.db`, leurs buckets en mémoire ne se voient pas. Solution simple : compteur en table dédiée mise à jour transactionnellement, plus lent mais correct.
