# Observabilité et métriques

## Problème

Aujourd'hui, le store fonctionne en boîte noire :

- pas de métriques de débit (commits/seconde par émetteur) ;
- pas de latence (prepare→commit, attest→sig) ;
- pas de compteurs d'erreurs (`QuorumError`, `HashChainError`, `NonceError`…) ;
- pas de visibilité sur les pairs lents ou en panne ;
- aucune alerte sur les anomalies (rejet anormal, fork, peer compromis suspect).

Conséquence : un incident ne se voit que quand un consommateur final se plaint.

## Options et tradeoffs

| Option | Idée | Effort | Couverture |
|---|---|---|---|
| **Logs structurés** | Émettre du JSON sur stdout/stderr aux moments clés | Faible | Audit forensique |
| **Métriques Prometheus** | Compteurs/histogrammes scrapés | Moyen | Tableaux de bord temps réel |
| **OpenTelemetry traces** | Traces des chemins prepare→attest→commit | Moyen-fort | Diagnostic latence |
| **Events `metric.*`** dans la chaîne | Métriques émises comme events | Faible | Auditable, mais lourd |

## Recommandation

**Trio classique** : logs structurés + métriques Prometheus + traces OpenTelemetry. Chaque outil répond à une question différente :

- *« qu'est-ce qui s'est passé ? »* → logs ;
- *« est-ce que ça va bien en ce moment ? »* → métriques ;
- *« pourquoi cette opération est-elle lente ? »* → traces.

Pas d'events `metric.*` dans la chaîne — ils pollueraient le journal métier sans valeur ajoutée. Si une métrique mérite d'être auditable (ex. taux de quorum atteint sur une période), elle est calculée *à partir* de la chaîne, pas écrite *dans* la chaîne.

## Métriques à exposer

| Nom | Type | Labels | Rationale |
|---|---|---|---|
| `evstore_commits_total` | counter | `issuer_id`, `event_type` | Débit par émetteur, par type |
| `evstore_commit_duration_seconds` | histogram | (idem) | Latence de bout en bout |
| `evstore_attest_duration_seconds` | histogram | `peer_id` | Pair lent ? |
| `evstore_signature_verifications_total` | counter | `result=ok\|invalid` | Saturé en CPU ? |
| `evstore_quorum_failures_total` | counter | `reason` | Disponibilité du quorum |
| `evstore_chain_height` | gauge | — | Croissance du journal |
| `evstore_peer_last_seen_seconds` | gauge | `peer_id` | Heartbeat ([WATERMARKS.md](../distribution/WATERMARKS.md)) |
| `evstore_integrity_check_duration_seconds` | histogram | — | Audit en dérive ? |
| `evstore_integrity_failures_total` | counter | — | **Alerte critique** |

## Schéma proposé

Hooks dans le store :

```python
class StoreMetrics:
    def __init__(self):
        self.commits = Counter("evstore_commits_total", ["issuer_id", "event_type"])
        self.commit_duration = Histogram("evstore_commit_duration_seconds")
        self.height = Gauge("evstore_chain_height")
        # ...

# Dans SQLEventStore.commit :
@self.metrics.commit_duration.time()
def commit(self, prepared):
    rid = self._do_commit(prepared)
    self.metrics.commits.labels(prepared.issuer_id, prepared.event_type).inc()
    self.metrics.height.set(self.height())
    return rid
```

Logs structurés (avec `structlog`) :

```python
log = structlog.get_logger()

def _insert_one(self, conn, prepared, rebranch):
    log.info("commit.start", issuer=prepared.issuer_id, event_type=prepared.event_type, nonce=prepared.nonce)
    try:
        rid = self._do_insert(conn, prepared, rebranch)
        log.info("commit.ok", row_id=rid)
        return rid
    except EventStoreError as exc:
        log.warning("commit.error", error=type(exc).__name__, detail=str(exc))
        raise
```

Tracing OpenTelemetry :

```python
tracer = trace.get_tracer("event_store")

def commit(self, prepared):
    with tracer.start_as_current_span("evstore.commit") as span:
        span.set_attribute("issuer_id", prepared.issuer_id)
        span.set_attribute("event_type", prepared.event_type)
        # ...
```

## Alertes recommandées

| Alerte | Condition | Sévérité |
|---|---|---|
| **Falsification détectée** | `evstore_integrity_failures_total > 0` | P1 |
| **Pair silencieux** | `evstore_peer_last_seen_seconds > 300` | P2 |
| **Quorum souvent raté** | `rate(evstore_quorum_failures_total[5m]) > 0.1` | P2 |
| **Latence commit > 1s p99** | `histogram_quantile(0.99, evstore_commit_duration_seconds) > 1` | P3 |
| **Croissance anormale** | `delta(evstore_chain_height[1h]) > seuil` | P3 |

## Intégration au store actuel

- **Hooks plug-and-play** : le store accepte un `metrics: Optional[Metrics]` et un `logger: Optional[Logger]`. Si non fournis, no-op.
- **Pas de dépendance dure** : `prometheus_client`, `structlog`, `opentelemetry` sont en `extras_require=["observability"]`.
- **Tests** : un test vérifie que les métriques avancent comme attendu (pattern `assert metrics.commits._value._value == 1`).

## Limites / risques

- **Cardinalité** : `issuer_id` peut exploser si on enregistre des dizaines de milliers de pairs. Plafonner en regroupant les rares (`issuer_id="other"` au-delà du top N).
- **PII dans les logs** : `payload` ne doit jamais être logué brut. Logguer le `content_hash` à la place.
- **Coût des traces** : OpenTelemetry échantillonné à 1 % suffit pour le diagnostic ; 100 % pour les chaînes critiques.
- **Faux positifs** : les alertes sur latence faussent en bas trafic. Combiner avec un seuil minimum (ex. `rate() > X AND quantile > Y`).
- **Confiance dans les métriques** : un attaquant qui contrôle un pair peut taire ses propres métriques. Une fraction de la télémétrie doit venir d'un observateur indépendant (par ex. proxy en front).

## Voir aussi

- [WATERMARKS.md](../distribution/WATERMARKS.md) — métrique `peer_last_seen`
- [KEY_ROTATION.md](../security/KEY_ROTATION.md) — détection d'anomalie post-compromise
- [INCREMENTAL_AUDIT.md](../security/INCREMENTAL_AUDIT.md) — alerte critique sur falsification
- [BACKPRESSURE.md](../scale/BACKPRESSURE.md) — métriques de quota
- [FORKS.md](../distribution/FORKS.md) — alerte fork détecté
