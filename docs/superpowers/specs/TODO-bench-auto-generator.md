# TODO — Auto-generator de scenarios bench depuis le knowledge factory

**Statut :** brainstorm à faire
**Date idée :** 2026-04-24
**Source :** discussion Alexis pendant evolve nuit 1

## Idée

Réutiliser ce que le pipeline knowledge factory produit déjà sur chaque device pour **auto-générer des scenarios bench** :

```
memory/{slug}/raw_research_dump.md     ← Scout (forums, datasheets, web_search sourced)
memory/{slug}/knowledge_graph.json     ← Cartographe (causes/effets structurés)
memory/{slug}/rules.json                ← Clinicien (symptom → cause → action)
memory/{slug}/dictionary.json           ← Lexicographe (glossaire device)
              ↓
     [generate_bench_from_pack.py]
              ↓
benchmark/auto_proposals/{slug}-{date}.jsonl
              ↓
     [humain valide]
              ↓
benchmark/scenarios.jsonl  (multi-device)
              ↓
     [eval_simulator --device <slug>]
              ↓
benchmark/per-device-scores.json   ← reliability score per device
              ↓
[agent runtime injecte dans son context]
"Simulator reliability for {slug}: score X.XX, basé sur N scenarios"
```

## Bénéfices

1. **Multi-device facile** — chaque pack généré → bench généré → score device-spécifique
2. **Honnêteté épistémique** — l'agent peut dire au tech *« sur ce device le simulateur est fiable à 0.78, mes top-3 hypothèses sont à prendre avec prudence »*
3. **Le Scout deepsearch est déjà fait** — pas de coût additionnel d'API
4. **Bench grandit organiquement** — chaque device qu'Alexis ajoute enrichit la cible

## Pièges à anticiper (à trancher en brainstorm)

### 1. Provenance — préserver les sources

Hard rule du bench : `source_url + source_quote + source_archive` obligatoires. Le générateur doit **extraire les quotes verbatim** du raw_research_dump (qui est sourcé par Scout via `web_search`), pas inventer.

### 2. Tautologie — éviter la self-référence

`expected_dead_rails` doit venir du **texte des sources** (ce que disent les forums / datasheets), PAS d'une exécution préalable du simulator. Sinon le bench valide ce que le simulator a déjà fait → score artificiel à 1.0.

### 3. Gaming — la génération reste sous contrôle humain

Si le générateur tourne en auto la nuit ET nourrit le bench que Opus optimise contre, on perd l'oracle figé (Goodhart's Law). **Règle proposée :** génération = batch HUMAIN-déclenché, puis bench frozen pour toute la nuit.

### 4. Qualité du Scout — diverse et complète ?

Le Scout actuel dump du markdown libre. Est-ce que ses sorties sont structurées assez pour qu'un sub-agent extracteur produise des `(cause, expected_*)` cohérents ? Probablement oui pour les failure modes courants, plus dur pour les cas edge.

## Décisions de design à brainstormer

- **Extraction LLM-driven vs regex** sur raw_dump ?
- **Ground-truth annotation** : qui décide que `expected_dead_rails=["+5V"]` est correct ? Le Scout / Clinicien output, ou une vérification humaine ?
- **Multi-device folder structure** dans benchmark/ : par device ou un seul fichier multi-slug ?
- **Quel modèle** pour générer ? Sonnet (rapide, suffit pour extraction) ou Opus (plus précis, plus cher) ?
- **Fréquence du run** : à chaque pack généré (auto) ou batch hebdomadaire (manuel) ?
- **Per-device score format** : un fichier ou inject dans state.json ?
- **Comment l'agent runtime accède au score** : via un nouveau tool `mb_simulator_reliability(device_slug)` ?

## Référence à la conversation initiale

Voir conversation 2026-04-24 ~20h00 — Alexis évoque l'idée pendant la nuit evolve 1. La validation de Claude :

> *« L'idée tient la route — Le pipeline knowledge factory a déjà fait 80% du travail (Scout extrait des failure modes, Cartographe les structure, Clinicien produit symptom→cause). Reste à les transformer en scenarios benchables. »*

## Prochaine étape

Quand Alexis a 2-3h dispo : invoquer le skill `superpowers:brainstorming`, ouvrir cette idée, trancher les questions de design, écrire la spec dans `docs/superpowers/specs/2026-XX-XX-bench-auto-generator-design.md`.

Idéalement avant la nuit evolve N+5 — sinon le bench MNT Reform va saturer en gain et Opus va commencer à griller du token sans progression.
