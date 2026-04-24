# Microsolder Evolve — Skill Design

**Date** : 2026-04-24
**Status** : Draft, awaiting user review
**Scope** : Conception d'un skill Claude Code dédié projet (`microsolder-evolve`) qui pilote une boucle nocturne autonome d'amélioration du simulateur diagnostic, sur le pattern *autoresearch* (karpathy/autoresearch) déjà éprouvé sur SolMind.

## 1. Goal

Permettre à un agent Opus de tourner toute une nuit sans supervision, en améliorant de façon mesurable le pipeline `simulator.py` + `hypothesize.py` du projet, avec discipline git stricte (commit chaque tentative, reset chaque régression).

## 2. Non-goals

- **Pas de généricité multi-projet.** Le skill est dédié microsolder. Une version portable (paramétrée par projet) est notée comme évolution future, pas dans ce design.
- **Pas la spec de l'infrastructure d'évaluation.** L'evaluator (`scripts/eval_simulator.py`), le golden set Opus (`benchmark/golden_opus.jsonl`), le seed curé manuel (`benchmark/seed_curated.jsonl`) et la métrique combinée font l'objet d'une spec séparée. Ce skill **consomme** cette infra, ne la définit pas.
- **Pas d'orchestration cloud.** L'orchestration tourne en local sur la machine de l'utilisateur via un script bash. Pas de Routines Anthropic-managed (incompatibles avec édition de code local + git).
- **Pas de validation humaine en boucle.** Aucun "flag pour revue", aucune confirmation. Autonomie totale, par design.

## 3. Architecture

```
hackathon-microsolder/
├── .claude/
│   └── skills/
│       └── microsolder-evolve/
│           └── SKILL.md              ← le skill (frontmatter + markdown)
├── scripts/
│   └── evolve-runner.sh              ← le runner bash (loop infinie)
├── evolve/
│   ├── results.tsv                   ← log keep/discard, append-only
│   ├── state.json                    ← baseline score, run counter, timestamps
│   └── reports/
│       └── YYYY-MM-DD-HHmm.md        ← mini-report par session (3 lignes)
├── api/pipeline/schematic/
│   ├── simulator.py                  ← EDITABLE par l'agent
│   ├── hypothesize.py                ← EDITABLE par l'agent
│   └── schemas.py                    ← READ-ONLY
├── scripts/
│   └── eval_simulator.py             ← READ-ONLY, contrat d'I/O ci-dessous
└── benchmark/
    ├── scenarios.jsonl               ← READ-ONLY, frozen oracle (~10-20 cas curés main)
    └── sources/                      ← READ-ONLY, archives des sources citées
```

## 4. Composants

### 4.1 Le skill (`.claude/skills/microsolder-evolve/SKILL.md`)

Markdown agent-side au sens Claude Code (frontmatter `name` + `description`, contenu = system prompt). Invoqué soit via la `Skill` tool dans une session, soit chargé en `--system-prompt-file` par le runner. Contient :

| Rubrique | Contenu |
|---|---|
| Frontmatter | `name: microsolder-evolve` / `description: Boucle d'amélioration nocturne autonome du simulateur microsolder` |
| Mission | Agent Opus autonome, goal = maximiser `score = 0.6·self_MRR + 0.4·cascade_recall`, NEVER STOP |
| Surface d'édition | **Editable** : `api/pipeline/schematic/simulator.py`, `hypothesize.py`. **Read-only** : tout le reste, en particulier `schemas.py`, `evaluator.py`, `eval_simulator.py`, `benchmark/scenarios.jsonl`, `benchmark/sources/`, `config/settings.json`, `.env`, tests fixtures. |
| Setup | Vérifie pré-requis au 1er run, abort proprement avec message clair si manquants |
| Boucle | 9 étapes (voir §5) |
| Schema results.tsv | Colonnes définies en §6 |
| Rules dures | NEVER STOP, one change at a time, always commit pré-édit, golden = sacré, pas de `--no-verify`, pas de `git push` |
| Dispatch optionnel | Si stuck (3+ discards consécutifs), peut invoquer `superpowers:dispatching-parallel-agents` |
| Garde-fous | Crash → status=crash sans reset, bench > 10 min → kill+reset, 5 discards → exploration mode |
| Output | Mini-report par session dans `evolve/reports/YYYY-MM-DD-HHmm.md` |

### 4.2 Le runner (`scripts/evolve-runner.sh`)

Script bash minimal calqué sur `runner.sh` de SolMind :

```bash
#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")/.."

LOCKFILE="/tmp/microsolder-evolve.lock"
LOGFILE="/tmp/microsolder-evolve.log"
INTERVAL=60  # sleep entre tentatives, en secondes

trap "rm -f $LOCKFILE; exit 0" EXIT INT TERM

while true; do
  if [ -f "$LOCKFILE" ]; then
    sleep "$INTERVAL"
    continue
  fi
  echo $$ > "$LOCKFILE"
  echo "=== EVOLVE SESSION $(date) ===" >> "$LOGFILE"

  echo "Execute one evolve session." | claude -p \
    --dangerously-skip-permissions --max-turns 100 \
    --system-prompt-file .claude/skills/microsolder-evolve/SKILL.md \
    >> "$LOGFILE" 2>&1 || true

  echo "=== EVOLVE EXIT $(date) ===" >> "$LOGFILE"
  rm -f "$LOCKFILE"
  sleep "$INTERVAL"
done
```

Lancement utilisateur : `nohup ./scripts/evolve-runner.sh 2>&1 &`.

Différences vs SolMind runner.sh :
- Pas de triggers événementiels (pas de "5 trades closed", pas de check WR drop). Pour ce cas, l'évaluation est synchrone et déterministe — chaque session est une expérience complète.
- Pas de `check_watchdog_health` — pas de circuit breaker autre que le lockfile.

### 4.3 L'infrastructure d'évaluation (hors scope, contrat seulement)

Le skill assume que `python -m scripts.eval_simulator` existe et émet sur stdout **une seule ligne JSON** conforme au pydantic `Scorecard` défini dans la spec axes 2/3 (`docs/superpowers/specs/2026-04-24-schematic-simulator-axes-2-3-design.md` §7) :

```json
{"score": 0.7421, "self_mrr": 0.8123, "cascade_recall": 0.6320, "n_scenarios": 18, "per_scenario": [...]}
```

- `score` ∈ [0, 1] : métrique scalaire combinée, `0.6 × self_mrr + 0.4 × cascade_recall`. Plus haut = meilleur.
- `self_mrr` : Mean Reciprocal Rank du problème inverse (cause connue → hypothesize la retrouve dans le top-K).
- `cascade_recall` : recall moyen sur le frozen oracle `benchmark/scenarios.jsonl`.
- `n_scenarios` : taille du benchmark, pour sanity check (alerter si chute brutale).
- `per_scenario` : breakdown détaillé (utilisé par l'agent au step 2 pour identifier les scénarios qui ratent).

Si `eval_simulator.py` n'existe pas ou ne respecte pas ce contrat, le skill abort au setup avec message explicite pointant vers la spec axes 2/3.

## 5. Boucle (cœur du skill)

À chaque invocation par le runner, l'agent exécute **une seule itération** :

1. **Read state.** Lire `evolve/state.json` (baseline_score, total_runs, last_status, last_5_statuses). Lire les 10 dernières lignes de `evolve/results.tsv`. Lire le dernier mini-report.
2. **Analyse.** Identifier la faiblesse la plus actionnable :
   - Si `last_5_statuses` contient ≥ 3 `discard` consécutifs → switch en mode exploration (lire en profondeur `simulator.py`, `hypothesize.py`, schémas, datasheets si présents).
   - Sinon, identifier 1-2 axes : modes de panne sous-représentés dans cascade_recall, faux positifs dans self_MRR, latence anormale, etc.
3. **Dispatch optionnel.** Si l'agent juge qu'un audit multi-angle débloquerait, il peut invoquer `superpowers:dispatching-parallel-agents` avec 2-4 audit-agents (chacun avec un angle différent : "trouve un mode de panne manquant pour les PMICs", "trouve un scénario du golden où cascade_recall rate", etc.). Synthèse → 1 hypothèse.
4. **Propose.** Formuler **une seule** hypothèse claire en 1-2 phrases. Pas de stack de modifs.
5. **Commit pré-édit.** S'assurer qu'on est sur une branche `evolve/<YYYY-MM-DD>` (créer la branche du jour si elle n'existe pas, sinon rester sur celle en cours même si la date a changé pendant la nuit — le passage de minuit ne crée pas de nouvelle branche). Vérifier working tree clean : pas de modifs sur fichiers tracked. Untracked OK. Si tracked dirty → abort avec message clair.
6. **Édit.** Modifier `simulator.py` et/ou `hypothesize.py`. Pas d'autre fichier. Si l'hypothèse demande de modifier autre chose, l'agent abort et log "out-of-scope".
7. **Mesure.** `python -m scripts.eval_simulator > /tmp/score.json 2>&1`. Timeout : 10 minutes (kill + reset si dépassement).
8. **Décide.**
   - Si `score_new >= baseline_score` → **keep** : `git add` + `git commit -m "evolve: <description> (+<delta_score>)"`, mettre à jour `baseline_score` dans `state.json`, append `results.tsv` avec `status=keep`.
   - Si `score_new < baseline_score` → **discard** : `git reset --hard HEAD` (annule l'édit non-committée), append `results.tsv` avec `status=discard` et `commit=<hash_baseline>`.
   - Si bench a crashé → **crash** : `git reset --hard HEAD`, append `results.tsv` avec `status=crash` et un extrait de stderr dans `description`. Pas de commit.
9. **Mini-report.** Écrire `evolve/reports/YYYY-MM-DD-HHmm.md` (3-5 lignes : hypothèse, avant/après, status). Mettre à jour `state.json` (incrément run counter, last_status, append last_5_statuses). Fin de session — l'agent quitte, le runner relance dans 60s.

## 6. Contrats

### 6.1 `evolve/results.tsv`

Tab-separated, header obligatoire :

```
timestamp	commit	score	self_mrr	cascade_recall	status	description
```

- `timestamp` : ISO 8601 UTC
- `commit` : SHA court (7 chars). Pour `discard`/`crash`, c'est le SHA de la baseline (puisque l'édit a été reset)
- `score`, `self_mrr`, `cascade_recall` : floats à 6 décimales. Pour `crash`, mettre `0.000000`.
- `status` : `keep` | `discard` | `crash`
- `description` : texte court (< 200 chars, no tab, no newline). Pour `crash`, inclure le type d'erreur.

### 6.2 `evolve/state.json`

```json
{
  "baseline_score": 0.6421,
  "baseline_commit": "abc1234",
  "total_runs": 47,
  "last_run_at": "2026-04-25T03:14:22Z",
  "last_status": "keep",
  "last_5_statuses": ["keep", "discard", "discard", "keep", "discard"],
  "branch": "evolve/2026-04-24"
}
```

### 6.3 Conventions git

- Branche : `evolve/<YYYY-MM-DD>` (créée au 1er run de la nuit).
- Commits keep : message `evolve: <description> (score: X.XXXXXX, +0.XXXX)`
- Pas de `git push` — la branche reste locale jusqu'à validation humaine au matin.
- Pas de `git tag`, pas de `git merge` automatique.

## 7. Pré-requis (bootstrap)

Avant que la boucle puisse démarrer, **doivent exister** :

- [ ] `scripts/eval_simulator.py` respectant le contrat §4.3 (spec axes 2/3)
- [ ] `api/pipeline/schematic/evaluator.py` avec `compute_self_mrr`, `compute_cascade_recall`, `compute_score` (spec axes 2/3)
- [ ] `benchmark/scenarios.jsonl` non-vide, ≥ 10 scénarios sourcés (spec axes 2/3)
- [ ] `benchmark/sources/` cache local des sources citées
- [ ] Working tree clean sur la branche source (master ou autre)
- [ ] `evolve/` directory créé (le skill peut l'init au 1er run)
- [ ] `evolve/state.json` avec `baseline_score` = score initial mesuré sur master

Si l'un manque, le skill abort proprement au setup avec message indiquant quoi créer.

**Bootstrap initial** (à faire par l'humain une fois, avant la 1ʳᵉ nuit) :

```bash
mkdir -p evolve/reports
git checkout -b evolve/$(date +%Y-%m-%d)
python -m scripts.eval_simulator > /tmp/baseline.json
# Puis créer evolve/state.json avec baseline_score extrait
echo "timestamp	commit	score	self_mrr	cascade_recall	status	description" > evolve/results.tsv
```

(Cette étape sera scriptée dans un `make evolve-bootstrap`, défini dans le plan d'implémentation.)

## 8. Garde-fous

- **Crash bench** → status=crash, `git reset --hard HEAD` (annule l'édit non-committée). L'humain peut reproduire au matin via le SHA baseline + l'extrait de stderr loggé dans `description`. Raison du reset : sans ça, le working tree reste dirty et la session suivante abort sur le check "dirty tree" — la nuit s'arrête au premier crash, ce qui contredit l'autonomie totale.
- **Bench > 10 min** → kill (timeout shell), `git reset --hard HEAD`, status=crash, description="bench timeout 10min".
- **Working tree dirty au démarrage de session** → abort avec message, rien n'est touché. Sécurité contre les conflits avec un humain qui édite en parallèle.
- **5 discards consécutifs** → l'agent passe automatiquement en mode exploration (lecture profonde du code + datasheets) avant la prochaine hypothèse. Pas un mode séparé, juste un trigger dans la boucle §5 step 2.
- **`git push` ou `git tag`** → interdit. Le SKILL.md le dit explicitement.
- **Modification hors `simulator.py` / `hypothesize.py`** → l'agent abort et log "out-of-scope" plutôt que d'élargir la surface. Si une vraie amélioration nécessite de toucher à autre chose, c'est une tâche pour l'humain au matin.
- **Désactivation de tests** ou ajout de `pytest.skip` → interdit. Si un test casse à cause d'une modif, c'est un signal de régression, pas un obstacle à contourner.

## 9. Tests / validation du skill lui-même

Avant de laisser tourner toute une nuit, validation manuelle :

1. **Smoke test single-run** : lancer le runner avec `--max-turns 30` et `INTERVAL=10`, observer 3-5 itérations. Vérifier que :
   - `results.tsv` est correctement appended
   - Les commits sont propres et reviewable
   - Les discards font bien `git reset --hard`
   - Les mini-reports sont écrits
   - L'agent ne sort jamais de la surface autorisée
2. **Test de crash bench** : casser temporairement `eval_simulator.py` (raise Exception), lancer 1 itération, vérifier que status=crash et que rien n'est resetté.
3. **Test de timeout** : forcer `eval_simulator.py` à `time.sleep(700)`, vérifier kill + reset + status=crash.
4. **Test de dirty tree** : laisser une modif non-commitée dans `simulator.py`, lancer 1 itération, vérifier abort propre sans destruction.

Ces tests sont manuels, pas automatisés. Pour un hackathon, ROI insuffisant pour mocker tout l'environnement Claude.

## 10. Risques

| Risque | Mitigation |
|---|---|
| Agent triche en simplifiant `simulator.py` pour faire monter self_MRR artificiellement | Cascade_recall sur le frozen oracle `benchmark/scenarios.jsonl` sert de garde-fou. Si l'agent dégrade le simulateur, cascade_recall chute, score baisse, discard auto. Le bench étant frozen et sourcé manuellement, l'agent ne peut pas le tuner pour tricher. |
| Coût en tokens Opus sur une nuit | À mesurer. Pour ~30 sessions × ~50k tokens = ~1.5M tokens/nuit en Opus. À monitorer après 1ʳᵉ nuit. Si trop cher, switch sur Sonnet ou réduire intervalle. |
| L'agent boucle sur les mêmes hypothèses ratées | Le SKILL.md instruit de lire `results.tsv` au step 1 et d'éviter les hypothèses déjà testées. Garde-fou faible (l'agent peut ignorer), mais le mode exploration au bout de 5 discards force un reset cognitif. |
| Branche `evolve/<date>` accumule beaucoup de commits non-pushés | OK pour l'usage : revue humaine au matin via `git log evolve/<date>` puis cherry-pick / squash / merge selon. |
| Conflit avec une session humaine en parallèle (édit manuel + boucle nocturne) | Le check "working tree dirty" abort proprement. L'humain doit committer ou stash avant de lancer le runner. |
| API Anthropic down quelques minutes | Le runner tourne en `\|\| true`, donc une session échouée ne tue pas le runner. Prochaine itération dans 60s. |

## 11. Évolution future (hors scope)

- **Portabilisation multi-projet** (l'option B du brainstorming) : extraire le SKILL.md en template paramétré par args ou config YAML. Pour quand l'utilisateur voudra appliquer le pattern à d'autres projets sans dupliquer.
- **Routines cloud** : si Anthropic baisse l'intervalle minimum sous 1h, envisager une variante cloud pour ne pas dépendre de la machine allumée.
- **Métrique évolutive** : quand microsolder aura des findings réels (repairs complétées avec ground truth), incorporer un `repair_MRR` dans le score combiné, comme prévu dans la spec evaluator.
- **Branche-parallèle pour discarded "interesting"** : si l'agent flag certains discards comme architecturalement intéressants, les pusher sur `evolve/discarded/<tag>` pour fouille humaine au matin. Pas critique pour MVP.
