# Flux d'un événement

Diagramme vertical du parcours complet d'un événement, de la préparation par l'émetteur à la persistance dans SQLite.

## Parcours principal

```mermaid
flowchart TB
    Start([Émetteur souhaite publier un événement]) --> Prep[Client.prepare<br/>event_type + payload]
    Prep --> Tick[HLCClock.tick<br/>physical_ms, logical]
    Tick --> Head[Lecture de la tête<br/>N derniers row_hashes]
    Head --> Nonce[Calcul du prochain nonce<br/>MAX nonce + 1]
    Nonce --> Canon[Sérialisation canonique<br/>JSON tri des clés]
    Canon --> CH[content_hash<br/>SHA-256 du corps]
    CH --> RH[row_hash<br/>SHA-256 content_hash + parents]
    RH --> Sign[Signature Ed25519<br/>de l'émetteur sur content_hash]
    Sign --> Prepared[[PreparedEvent]]

    Prepared --> Loop{Pour chaque pair}
    Loop --> Attest[Client.attest<br/>re-dérive tout]
    Attest --> Check{Tout valide ?}
    Check -- non --> Reject([HashChainError /<br/>IssuerError / NonceError])
    Check -- oui --> Sig[Signature du pair<br/>sur content_hash]
    Sig --> Loop
    Loop -- quorum atteint --> Commit[SQLEventStore.commit]

    Commit --> Lock[[Verrou Python +<br/>BEGIN EXCLUSIVE SQLite]]
    Lock --> Verify[Re-vérifications<br/>hash, sigs, nonce, parents]
    Verify --> Rebranch[Rebranch :<br/>relire la tête vivante,<br/>recalculer row_hash]
    Rebranch --> Quorum{Quorum<br/>de signatures<br/>valides ?}
    Quorum -- non --> QErr([QuorumError])
    Quorum -- oui --> Insert[(INSERT events)]
    Insert --> CommitTx[COMMIT]
    CommitTx --> End([row id retourné])
```

## Détection des falsifications

```mermaid
flowchart TB
    Audit([verify_integrity déclenché]) --> Read[Lecture séquentielle<br/>de la table events]
    Read --> Iter{Pour chaque événement}
    Iter --> Parents[Re-dériver parent_hashes<br/>depuis la chaîne en cours]
    Parents --> ParentsOK{Match ?}
    ParentsOK -- non --> Fail1([IntegrityError<br/>parents divergents])
    ParentsOK -- oui --> Content[Re-calculer content_hash]
    Content --> ContentOK{Match ?}
    ContentOK -- non --> Fail2([IntegrityError<br/>corps modifié])
    ContentOK -- oui --> Row[Re-calculer row_hash]
    Row --> RowOK{Match ?}
    RowOK -- non --> Fail3([IntegrityError<br/>chaîne cassée])
    RowOK -- oui --> Issuer[Vérifier signature<br/>de l'émetteur]
    Issuer --> IssuerOK{Valide ?}
    IssuerOK -- non --> Fail4([IntegrityError<br/>signature émetteur])
    IssuerOK -- oui --> Peers[Vérifier chaque<br/>signature de pair]
    Peers --> PeerQuorum{Quorum atteint<br/>et toutes valides ?}
    PeerQuorum -- non --> Fail5([IntegrityError<br/>quorum / pair invalide])
    PeerQuorum -- oui --> Iter
    Iter -- fin de la table --> OK([Audit OK])
```
