# Outillage CLI

## Problème

Aujourd'hui, toute interaction avec le store passe par du code Python : ouvrir une REPL, importer `event_store`, instancier `SQLEventStore`, appeler les méthodes. Conséquences :

- pas d'outil pour un opérateur qui doit inspecter une chaîne en production ;
- pas de moyen rapide de lancer un audit, exporter, vérifier l'état d'un peer ;
- pas de scripting Bash possible (CI, monitoring, ops jobs) ;
- la documentation des commandes courantes ([CLAUDE.md](../../CLAUDE.md)) reste théorique.

Un CLI est le ciment opérationnel manquant.

## Options et tradeoffs

| Option | Idée | Effort | Maintenabilité |
|---|---|---|---|
| **Scripts ad-hoc** | Un script par usage, dans `scripts/` | Faible | Drift inévitable |
| **Module `__main__`** | `python -m event_store inspect ...` | Moyen | Centralisé |
| **CLI dédiée** (Click, Typer) | `evstore inspect`, `evstore audit`… | Moyen | Idiomatique |
| **CLI compilée** | binaire statique (PyOxidizer, shiv) | Fort | Distribution simple |

## Recommandation

**CLI dédiée avec Typer** : moderne, intégré à Python, type hints natifs, `--help` automatique, sous-commandes simples. Pas de compilation — distribution via `pip install`.

Sous-commandes minimales :

| Commande | Description |
|---|---|
| `evstore init` | Crée la base et applique le schéma |
| `evstore peer add <id> <pubkey>` | Enregistre un peer |
| `evstore peer list` | Liste les peers connus |
| `evstore inspect [--from N] [--to M]` | Affiche les events dans une tranche |
| `evstore audit [--incremental]` | Lance `verify_integrity()` |
| `evstore export <path> --from N --to M` | Exporte une tranche en JSON-Lines / Parquet |
| `evstore diff <db_a> <db_b>` | Compare deux bases pour détecter un fork |
| `evstore head` | Affiche le row_hash de la tête, la hauteur, les derniers parents |
| `evstore stats` | Statistiques par event_type, issuer, période |

## Schéma proposé

Structure du module :

```
event_store/
  cli/
    __init__.py
    main.py          # entry point Typer
    inspect.py
    audit.py
    export.py
    diff.py
    peer.py
```

Skeleton :

```python
# event_store/cli/main.py
import typer
from . import inspect, audit, export, diff, peer

app = typer.Typer(no_args_is_help=True)
app.command("inspect")(inspect.run)
app.command("audit")(audit.run)
app.command("export")(export.run)
app.command("diff")(diff.run)
app.add_typer(peer.app, name="peer")

if __name__ == "__main__":
    app()
```

Exemple `audit` :

```python
def run(
    db: Path = typer.Argument(..., exists=True),
    incremental: bool = typer.Option(False, "--incremental"),
    fail_fast: bool = typer.Option(True),
) -> None:
    store = SQLEventStore(str(db))
    try:
        if incremental:
            store.verify_integrity_incremental()
        else:
            store.verify_integrity()
        typer.echo(f"OK — {store.height()} events vérifiés")
    except IntegrityError as exc:
        typer.echo(f"KO — {exc}", err=True)
        raise typer.Exit(1)
```

`pyproject.toml` :

```toml
[project.scripts]
evstore = "event_store.cli.main:app"
```

## Intégration au store actuel

- **Aucune modification du core** — la CLI consomme l'API publique de `event_store`.
- **Nouvelle dépendance** : `typer` (léger, pas de runtime exotique).
- **Tests** : Typer fournit `CliRunner`, identique à Click. Un test par sous-commande, exécuté contre une base temporaire.
- **Docs** : `evstore --help` doit produire un texte assez bon pour servir de référence rapide.

## Limites / risques

- **Sécurité** : un opérateur qui a accès au CLI a accès au quorum (s'il a les clés privées). Distinguer **CLI lecture seule** (audit, inspect, export) de **CLI écriture** (peer add). La séparation peut passer par un flag `--read-only` ou par deux binaires distincts.
- **Performance sur grands journaux** : `inspect` doit paginer (`--from`, `--to`, `--limit`) pour ne pas charger des millions d'events en mémoire.
- **Dérive avec l'API** : si une méthode change de signature, la CLI casse. Tests d'intégration smoke-tests à chaque PR.
- **Localisation** : messages d'erreur en anglais ou en français ? Cohérence avec les autres composants — actuellement la doc est en français mais le code en anglais. Choix politique.
- **Sortie machine-friendly** : prévoir `--json` sur les commandes de lecture pour piper vers `jq`, monitoring, etc.
