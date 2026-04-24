# Handoff prompt — Scout enrichi par documents optionnels du technicien

**Destiné à :** nouvelle session Claude Code, contexte vide.
**Consigne utilisateur :** pas de brainstorming, on a déjà itéré. Attaque directement spec → implémentation → audit. Work on `main` directement, pas de branche, pas de push.

---

## Qui tu es, où tu es

Tu es Claude Code dans le repo `microsolder-agent` à `/home/alex/Documents/hackathon-microsolder/`. Lire `CLAUDE.md` à la racine avant toute action — il contient les hard rules et le layout du projet. Branche actuelle : `main`. **L'evolve runner tourne en parallèle** sur `evolve/2026-04-24` ; il mute `api/pipeline/schematic/simulator.py` et `api/pipeline/schematic/hypothesize.py`. **Ne touche jamais** à cette liste :

```
api/pipeline/schematic/simulator.py
api/pipeline/schematic/hypothesize.py
api/pipeline/schematic/evaluator.py
benchmark/scenarios.jsonl
benchmark/sources/
evolve/*
api/pipeline/schematic/boot_analyzer.py
tests/pipeline/schematic/test_boot_analyzer.py
```

Tu peux importer depuis `evaluator.py` en read-only.

## Contexte : ce qui a été construit aujourd'hui (2026-04-24)

Les 4 derniers jours on a implémenté un **bench auto-generator** qui consomme un pack device (`memory/{slug}/*.json` + `raw_research_dump.md`) et produit des scenarios simulables dans `benchmark/auto_proposals/`. Les artefacts clés :

- `api/pipeline/bench_generator/` — module complet (schemas, prompts, validator V1-V5 + V2b guardrails, extractor Sonnet/Opus, scoring wrapper, atomic writer, composed orchestrator)
- `scripts/generate_bench_from_pack.py` — CLI, default `--model claude-opus-4-7`
- `api/agent/reliability.py` + injection `render_system_prompt` + extension `_SEED_FILES` dans `memory_seed.py`

État validé sur `mnt-reform-motherboard` (commit `011e024`) :

| Metric | Valeur |
|---|---|
| n_proposed | 6 |
| n_accepted | 4 (audit manuel scenario-par-scenario → 4/4 correct) |
| n_rejected | 2 (duplicates) |
| score | 0.728 |
| cascade_recall | 0.750 |

Les logs : `git log --oneline main | head -30` pour récapituler. Le dernier commit explicatif est `011e024`.

## Limitation structurelle identifiée

Le pipeline `api/pipeline/` fait du Scout **aveugle au schematic** :

```
POST /pipeline/generate {device_label}
  └── Scout (web_search, ne reçoit QUE le nom du device)
      └── produit raw_research_dump.md — quotes en langage fonctionnel
          "LPC controller won't wake up", "charge board dead"
          JAMAIS de refdes ni de rail labels
```

Le schematic pipeline est séparé (CLI `python -m api.pipeline.schematic.cli --pdf=...`). Les deux se croisent uniquement dans `memory/{slug}/` après coup. Le bench generator actuel compense via un bridge heuristique déterministe (`prompts.py::build_functional_candidate_map`) qui matche registry canonicals → refdes par token-overlap sur les noms de rails. Ça marche partiellement mais est **fragile** et **rate les entités qui n'ont pas de rail portant leur nom** (CPU SOM, charge board, eDP cable, etc.).

## Vision utilisateur à implémenter

Le technicien crée une **réparation** (entité déjà existante, voir `POST /pipeline/repairs`). Au moment de la création il peut **optionnellement** fournir :

- Schematic PDF (→ ingestion produit `electrical_graph.json`)
- Boardview (.brd / .kicad_pcb / etc., → parser `api/board/parser/` produit `Board`)
- Datasheets PDF (archives locales, nouveau)
- N'importe quel document que le tech juge utile (notes, logs, rapports de réparation passés)

Puis il déclenche le workflow memory bank. **Scout reçoit tout ce qui a été uploadé** et s'en sert pour :

1. **Cibler ses `web_search`** — exemples : MPN U7 = LM2677 (extrait du schematic) → « LM2677 failure modes site:ti.com » ; MNT Reform + U14 + LPC → recherches précises
2. **Attacher des refdes littéraux à ses quotes** — quand il cite une datasheet qui dit « LM2677 SIMPLE SWITCHER 5-A step-down regulator. Catastrophic failure... », il peut attacher cette quote à `cause.refdes=U7` avec provenance URL externe + preuve que U7 est un LM2677 dans le graph fourni
3. **Mentionner les rails** — le graph lui donne `+5V`, `+3V3`, `LPC_VCC`, `PCIE1_PWR` etc. ; quand une datasheet ou un forum post mentionne un de ces rails, Scout l'inclut dans la quote

Si l'utilisateur ne fournit rien → **comportement actuel inchangé** (Scout web_search aveugle, bench generator applique son bridge heuristique). Pas de régression.

## Garde-fous anti-hallucination (critiques)

Ces règles sont non-négociables — elles définissent ce qui distingue Scout enrichi vs Scout qui invente :

1. **Provenance URL externe obligatoire** — même quand Scout voit le schematic, chaque quote doit avoir sa `source_url` qui pointe vers un document externe vérifiable. Le schematic fourni sert de **targeting** des recherches, pas de ground truth pour les quotes.
2. **MPN-based search only** — Scout peut voir « U7 has MPN=LM2677 » dans le graph/BOM et faire `web_search "LM2677 failure"`. Il ne peut pas voir « U7 sources +5V » et écrire « Source-X says U7 failure causes +5V to die » sans trouver Source-X littéralement. Le graph topologie ne peut pas être présenté comme quote.
3. **refdes_candidates doivent être justifiés** — si le registry phase émet `{"canonical_name": "LPC controller", "refdes_candidates": [{"refdes": "U14", ...}]}`, l'evidence doit être soit (a) une quote externe qui lie LPC controller à U14 via MPN/datasheet, soit (b) « inference from BOM MPN match » (BOM étant un document fourni par le tech donc ground truth local).
4. **Pas de fallback sur le graph si aucune source externe** — si Scout ne trouve aucune source qui cite le refdes, il ne crée pas de scenario dessus. Le graph n'est jamais une source en lui-même.

## Spec concrète à implémenter

### A. Modifier `POST /pipeline/repairs` (ou créer un nouvel endpoint upload)

Le tech uploade 0-N documents typés :

```http
POST /pipeline/repairs/{repair_id}/documents
  multipart/form-data:
    - file: <binary>
    - kind: "schematic_pdf" | "boardview" | "datasheet" | "notes" | "other"
    - description: "free text"
```

Stockage : `memory/{slug}/uploads/{timestamp}-{kind}-{filename}`. Pas d'auto-processing — le tech déclenche ensuite `POST /pipeline/generate`. (Current endpoint can be extended or a new one created ; you decide from reading the existing code.)

### B. Modifier `POST /pipeline/generate`

Accepte optional `uploaded_documents_dir` (défaut `memory/{slug}/uploads/`). Avant de lancer Scout :

1. Si un `schematic_pdf` est uploadé → lancer `ingest_schematic()` d'abord (existing pipeline)
2. Si un boardview est uploadé → parser via `api/board/parser/parser_for(path)` (existing)
3. Collecter tous les `datasheet` PDFs dans une liste

Ces artefacts sont ensuite passés à Scout.

### C. Modifier `api/pipeline/scout.py::run_scout`

Nouvelle signature :

```python
async def run_scout(
    *,
    client: AsyncAnthropic,
    device_label: str,
    model: str,
    graph: ElectricalGraph | None = None,   # NEW optional
    board: Board | None = None,              # NEW optional
    datasheet_paths: list[Path] | None = None,  # NEW optional
    min_symptoms: int = ...,
    ...existing args...
) -> str:
```

Si aucun arg optional n'est fourni → comportement actuel (aveugle). Si `graph` fourni → `SCOUT_USER_TEMPLATE` inclut un bloc graph_summary + MPN list (quand boardview fournit MPNs). Si `datasheet_paths` fournis → Scout peut les citer via `source_url = "local://datasheets/{filename}"` et `source_archive = "path/to/local/copy"`.

### D. Modifier `api/pipeline/prompts.py::SCOUT_SYSTEM`

Ajouter une section "WHEN YOU HAVE LOCAL DOCUMENTS" qui codifie les garde-fous anti-hallucination ci-dessus. Important : même avec le graph fourni, la structure de `raw_research_dump.md` reste la même (symptomes, causes, components mentionnés, sources). Le graph sert à **cibler** pas à **remplacer**.

### E. Modifier `api/pipeline/registry.py`

La phase Registry reçoit maintenant le raw_dump ET optionnellement le graph (via orchestrator). Elle émet un `registry.json` enrichi avec un nouveau champ par canonical :

```json
{
  "canonical_name": "LPC controller",
  "aliases": [...],
  "kind": "ic",
  "description": "...",
  "refdes_candidates": [
    {
      "refdes": "U14",
      "confidence": 0.95,
      "evidence": "Forum post at https://... describes the LPC as U14 in the board revision 2.0 schematic"
    }
  ]
}
```

Si pas de graph → pas de `refdes_candidates` (legacy shape). Pydantic schema dans `api/pipeline/schemas.py` à étendre avec `refdes_candidates: list[RefdesCandidate] | None = None`.

### F. Simplifier `api/pipeline/bench_generator/prompts.py` et `validator.py`

Quand registry contient `refdes_candidates` sur ses entries :
- `build_functional_candidate_map` consomme ces candidates directement au lieu de son heuristique (plus robuste, car LLM-produit avec provenance)
- V2b.1 vérifie que `cause.refdes` est dans les `refdes_candidates` du canonical cité, pas via le scorer heuristique
- Si `refdes_candidates` absent (legacy packs) → fallback sur l'heuristique actuelle

### G. Tests

- `tests/pipeline/test_scout_with_graph.py` — vérifie que run_scout avec graph produit un dump qui mentionne les refdes (mock web_search avec réponses canned)
- `tests/pipeline/test_registry_refdes_candidates.py` — vérifie que registry phase émet refdes_candidates quand graph est fourni
- `tests/pipeline/bench_generator/test_validator.py` — ajouter un cas qui vérifie la consommation de `refdes_candidates` du registry

### H. Runtime validation

Re-run sur `mnt-reform-motherboard` qui a déjà `electrical_graph.json` disponible. Comparer :

- Avant (main au commit `011e024`) : 4 accepted, score 0.728
- Après : attendu ≥ 8 accepted, score ≥ 0.85, cascade_recall ≥ 0.80

Audit scenario-par-scenario comme fait pour le commit précédent (voir git log pour la méthode).

## Règles opérationnelles

- **Work on main directly**, pas de branche (instruction explicite Alexis)
- **Commit direct avec paths explicites** (`git commit -- <paths>` à cause de l'evolve runner parallèle)
- **Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>** sur tous tes commits
- **Pas de push origin** sans autorisation explicite d'Alexis
- **TDD quand pertinent** mais pas obligatoire — l'important c'est le test de bout-en-bout réel sur mnt-reform à la fin
- **Pas de brainstorming** — skip le skill superpowers:brainstorming, vas directement à writing-plans si tu veux structurer, ou spec+implement en un seul pass
- **Test de non-régression** avant de toucher Scout : vérifier que le CLI actuel `python scripts/generate_bench_from_pack.py --slug mnt-reform-motherboard` continue de produire le même output (score 0.728) quand aucun document n'est uploadé. Le nouveau code doit être strictement additif côté comportement sans uploads.
- **Evolve runner** : ignorer completement, rester hors de ses tabous
- **Si tu trouves une contradiction ou un blocage**, stop et pose la question — ne pas improviser

## Ordre de travail suggéré

1. Lire `CLAUDE.md`, `api/pipeline/scout.py`, `api/pipeline/prompts.py`, `api/pipeline/orchestrator.py`, `api/pipeline/schemas.py`, `api/pipeline/registry.py`, `api/pipeline/bench_generator/{prompts,validator}.py` pour avoir le layout en tête
2. Écrire une courte spec dans `docs/superpowers/specs/2026-04-25-scout-with-optional-documents-design.md` (ou date du jour si > 24/04) — 1 seul commit, pas besoin de brainstorm formel
3. Extend the Pydantic schemas (registry refdes_candidates)
4. Implement upload endpoint (simple file copy to memory/{slug}/uploads/)
5. Wire graph + datasheets into Scout via orchestrator + scout.run_scout signature
6. Extend SCOUT_SYSTEM prompt with the anti-hallucination contracts
7. Extend registry.py prompt to emit refdes_candidates when graph is provided
8. Simplify bench_generator to prefer registry.refdes_candidates over heuristic
9. Tests unit (mock client) au fur et à mesure
10. **Non-regression run** sans documents → doit matcher score 0.728 à l'epsilon près
11. **Enriched run** avec schematic fourni → mesurer le gain
12. Audit scenario-par-scenario du run enriched
13. Commit final avec message qui explique le gain mesuré, artefacts dans `benchmark/auto_proposals_v5_*/`

## Fichiers de référence pour comprendre l'historique

- `docs/superpowers/specs/2026-04-24-bench-auto-generator-design.md` — spec originale du bench-gen
- `docs/superpowers/plans/2026-04-24-bench-auto-generator.md` — plan TDD 23 tâches
- `benchmark/auto_proposals/` — premier run (score 0.507, 40% fabriqué, pré-V2b)
- `benchmark/auto_proposals_v2b_opus/` — run « 11 accepted » qui s'est avéré être 10/11 fabriqué (V2b bypass par editable install)
- `benchmark/auto_proposals_bridge_opus/` — run avec bridge strict (1 accepted, score 0.428)
- `benchmark/auto_proposals_v3_opus/` — run après V2b.2 removal + V2b.3 strict (4/4 correct, score 0.728)

## Bonne chance

L'audit scenario-par-scenario est la seule vraie validation. Ne te fie pas aux scores seuls — ils peuvent être trompeurs si les scenarios sont fabriqués de façon cohérente. Lire chaque quote, cross-référencer avec `memory/mnt-reform-motherboard/raw_research_dump.md` et `electrical_graph.json`, et ne pas hésiter à recommencer si les scenarios ne tiennent pas la route.

Alexis est disponible mais préfère être peu interrompu. Pose des questions seulement sur les blocages structurels.
