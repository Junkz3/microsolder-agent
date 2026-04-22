# Pilotage du boardview par l'agent — design spec

**Date :** 2026-04-23
**Scope :** câblage bout-en-bout permettant à l'agent de diagnostic (Opus 4.7 / Sonnet 4.6 / Haiku 4.5) de piloter le canvas PCB du workbench via tool calls. Famille d'outils `bv_*` (nouveau) + agrégation multi-source de `mb_get_component` + manifest dynamique par session + API publique sur le renderer existant + listener WebSocket côté frontend.
**Hors scope :** parsing du schematic PDF (`api/vision/` reste stub — `sch_*` est un hook prévu, pas livré ici). Renderer PCB (`web/brd_viewer.js` déjà livré). Pipeline knowledge (`api/pipeline/` inchangé).

---

## 1. Contexte

L'agent diagnostic connaît aujourd'hui 4 outils `mb_*` (lecture memory bank : `mb_get_component`, `mb_get_rules_for_symptoms`, `mb_list_findings`, `mb_record_finding`) mais ne peut pas **montrer** ce qu'il trouve sur le canvas PCB. Le tech doit relire un refdes dans le chat, puis cliquer lui-même dans `brd_viewer.js` pour le localiser. L'agent reste aveugle au contexte visuel partagé et muet à l'action.

Presque toute la tuyauterie est déjà en place :
- 12 handlers prêts dans `api/tools/boardview.py` (highlight, focus, annotate, dim_unrelated, …)
- 14 enveloppes Pydantic WS dans `api/tools/ws_events.py` (12 `bv_*` actions + `BoardLoaded` + `UploadError`) avec `type = "boardview.<verb>"`
- `SessionState` (`api/session/state.py`) qui porte `board`, `highlights`, `annotations`, `arrows`, etc.
- Renderer canvas complet (`web/brd_viewer.js`, 1139 lignes) avec hit-test, zoom/pan, inspector
- WebSocket `/ws/diagnostic/{slug}` avec deux runtimes (managed + direct) et relai `tool_use` au frontend

Ce qui manque est du **câblage**, pas de l'invention. Ce spec liste exactement les 10 points de câblage, l'architecture cible, et les changements de règles.

---

## 2. Règles dures — mise à jour de la Hard Rule #5

Le `CLAUDE.md` actuel dit :

> **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the agent mentions must be validated against parsed board data *before* being shown to the user. Tools that cannot answer return structured null/unknown — never fake data.

**Deux constats :**

1. Le gate post-hoc sous-entendu par « validated *before* being shown » n'a jamais été implémenté.
2. La discipline « tool = seule source de refdes » **réduit** l'hallucination mais ne la **supprime** pas : rien n'empêche mécaniquement l'agent d'écrire « U999 est le PMIC » dans du **texte libre** (réponse finale) sans avoir appelé un tool. Le system prompt est une instruction, pas un garde-fou mécanique.

**Décision : on garde la règle dure ET on ajoute un vrai gate post-hoc léger.** La règle ne s'affaiblit pas — au contraire, sa mécanique devient effective pour la première fois.

**Nouvelle formulation retenue :**

> **No hallucinated component IDs.** Every refdes the agent surfaces must originate from a tool lookup. Defense in depth, enforced in two layers: (1) tools never fabricate — `mb_get_component` and `bv_*` return `{found: false, closest_matches: [...]}` for unknown refdes, and the system prompt instructs the agent to pick from `closest_matches` or ask the user; (2) a lightweight post-hoc sanitizer scans every outbound agent `message` text block for refdes-shaped tokens (regex `\b[A-Z]{1,3}\d{1,4}\b`) and, when a board is loaded in the session, validates each match against `session.board.part_by_refdes`. Unknown matches are wrapped as `⟨?U999⟩` in the delivered text and logged server-side. When no board is loaded, the sanitizer is a no-op (no ground truth to validate against).

**Implémentation du sanitizer** (nouveau module `api/agent/sanitize.py`, ~30 lignes) :

```python
import re
from api.board.model import Board
from api.board.validator import is_valid_refdes

REFDES_RE = re.compile(r"\b[A-Z]{1,3}\d{1,4}\b")

def sanitize_agent_text(text: str, board: Board | None) -> tuple[str, list[str]]:
    """Return (cleaned_text, unknown_refdes_list).
    If board is None, returns text unchanged (no ground truth)."""
    if board is None:
        return text, []
    unknown = []
    def _wrap(m: re.Match) -> str:
        tok = m.group(0)
        if is_valid_refdes(board, tok):
            return tok
        unknown.append(tok)
        return f"⟨?{tok}⟩"
    return REFDES_RE.sub(_wrap, text), unknown
```

Appliqué juste avant `ws.send_json({"type": "message", ...})` dans les deux runtimes. Le tech voit `⟨?U999⟩` dans le chat (signal visuel clair), le backend log un warning, et l'agent reçoit dans le prochain tour la version sanitisée si elle revient dans `messages`.

**Faux positifs attendus :** nets avec un format refdes-like (`HDMI_D0`, `GPIO12`) sont hors pattern (underscore / pas de lettre majuscule isolée en tête). Signaux comme `VDD_3V3` passent (pas de nombre terminal). Tokens comme `USB3`, `DDR4`, `HDMI2` matchent le pattern — acceptable risk, ils seront flaggés `⟨?USB3⟩` si absents du board, ce qui est un signal utile (l'agent ne devrait pas nommer des protocoles comme s'ils étaient des composants). Si le taux de faux positifs gêne, on pourra affiner le regex à `[A-Z]{1,2}\d{1,4}` ou whitelister.

L'esprit est préservé, la mécanique devient réellement effective. Le `SYSTEM_PROMPT_DIRECT` actuel (lignes 33-36 de `api/agent/runtime_direct.py`) reste valide comme instruction douce, à étendre pour mentionner l'agrégation multi-source de `mb_get_component`.

Les autres règles dures (Apache 2.0, deps permissives, open hardware only, all code from scratch) sont inchangées.

---

## 3. Scope

### In scope

- `api/agent/runtime_direct.py` + `runtime_managed.py` — manifest dynamique, dispatch `bv_*`, émission WS des events, application du sanitizer refdes avant `ws.send_json`
- `api/agent/tools.py` — `mb_get_component` **restructuré** : passage d'une forme plate à une forme à sections nommées (`{found, memory_bank: {...}, board: {...}}`). **Refactor breaking** assumé — les tests `tests/agent/test_mb_tools.py` seront migrés.
- `api/agent/sanitize.py` — **nouveau module**, ~30 lignes, regex + validator
- `api/agent/manifest.py` — **nouveau module**, `build_tools_manifest(session)` + `render_system_prompt(session, device_slug)` (utilisé **uniquement** par le runtime direct, cf. §4)
- `api/agent/dispatch_bv.py` — **nouveau module**, table `BV_DISPATCH` + wrapper `dispatch_bv(session, name, payload)`
- `api/tools/boardview.py` — aucun changement fonctionnel (handlers réutilisés tels quels)
- `api/session/state.py` — helper `SessionState.from_device(device_slug) -> SessionState` + ajout d'un attribut `schematic: Any = None` au dataclass (hook futur, pas alimenté ici)
- `web/js/llm.js` — listener `boardview.*` qui délègue à `window.Boardview.apply(payload)`
- `web/js/main.js` — installe un stub `window.Boardview = {__pending: [], apply(ev) { this.__pending.push(ev); }}` très tôt au load, drainé par `initBoardview`
- `web/brd_viewer.js` — API publique `window.Boardview` + split user/agent state + drain de `__pending` au premier run
- `CLAUDE.md` — réécriture Hard Rule #5
- Tests : `tests/agent/test_dispatch_bv.py`, `tests/agent/test_manifest_dynamic.py`, `tests/agent/test_mb_aggregation.py`, `tests/agent/test_sanitize.py`, `tests/agent/test_session_from_device.py`, `tests/agent/test_system_prompt.py`

### Découpage en commits

Ce chantier touche `api/`, `web/`, et `CLAUDE.md`. Par la règle de commit hygiene du `CLAUDE.md` (« ne jamais bundler deux domaines dans le même commit »), l'implémentation atterrit en **minimum 3 commits** :

1. `feat(agent): bv_* tools + dynamic manifest + mb_* aggregation + sanitizer` — tout le backend + tests unitaires
2. `feat(web): window.Boardview public API + agent state split` — tout le frontend
3. `docs: rewrite Hard Rule #5 (tool-boundary verification + post-hoc sanitizer)` — CLAUDE.md uniquement

Les tests d'intégration WS (`tests/agent/test_ws_flow.py`) peuvent atterrir dans le commit 1 ou en 4ᵉ commit séparé selon leur poids.

### Out of scope

- Parseur schematic PDF (`api/vision/`) — reste stub, spec future
- Famille `sch_*` — hook prévu (test de présence dans `build_tools_manifest`), pas livrée ici
- Ajout de nouveaux `bv_*` au-delà des 12 existants
- Refactor du renderer `brd_viewer.js` au-delà du split user/agent state et de l'API publique
- Nouveaux tools `mb_*` (les 4 existants sont suffisants)

---

## 4. Architecture cible

```
┌──────────────────────────────────────────────────────────────┐
│                api/agent/runtime_{direct,managed}.py         │
│                                                              │
│  on ws.accept():                                             │
│    session = SessionState.from_device(device_slug)           │
│    # charge le .kicad_pcb / .brd et peuple session.board     │
│                                                              │
│  build_tools_manifest(session) ──►                           │
│      [mb_*] ∪                                                │
│      [bv_*] si session.board is not None ∪                  │
│      [sch_*] si session.schematic is not None  (FUTUR)       │
│                                                              │
│  # DIRECT runtime seulement : prompt assemblé localement     │
│  system_prompt = render_system_prompt(session, device_slug)  │
│    # « For this device : memory bank ✅, boardview ✅ »      │
│  # MANAGED runtime : prompt porté côté Anthropic (agent MA). │
│  # Capabilities implicites via le manifest — si bv_* absent, │
│  # l'agent ne peut pas les appeler, point.                   │
│                                                              │
│  loop:                                                       │
│    response = client.messages.create(…, tools=manifest)      │
│    for block in response.content:   # ordre préservé         │
│      if block.type == "text":                                │
│        clean, unknown = sanitize_agent_text(block.text,      │
│                                             session.board)   │
│        if unknown: logger.warning("refdes inconnus: %s", …) │
│        ws.send_json({type: "message", text: clean})          │
│      if block.type == "tool_use":                            │
│        ws.send_json({type: "tool_use", name, input})         │
│        result = _dispatch(session, block.name, block.input)  │
│        if result.get("event"):                               │
│          ws.send_json(result["event"].model_dump(            │
│              by_alias=True))                                 │
│        # tool_result contient {ok, summary, reason?} — PAS event│
│        tool_results.append({                                 │
│          type: "tool_result", tool_use_id: block.id,         │
│          content: json.dumps({ok, summary, reason?})         │
│        })                                                    │
└───────────────────┬──────────────────────────────────────────┘
                    │ ws.send_json({type: "boardview.highlight",
                    │                refdes: ["U7"], color: "accent"})
                    ▼
┌──────────────────────────────────────────────────────────────┐
│                  web/js/llm.js (WS client)                   │
│                                                              │
│  on ws.message(payload):                                     │
│    if payload.type?.startsWith("boardview."):                │
│      window.Boardview.apply(payload)                         │
│    else switch payload.type:                                 │
│      "message" | "tool_use" | "thinking" | "error" | …       │
│      → existing chat log rendering                           │
└───────────────────┬──────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────┐
│              web/brd_viewer.js (renderer PCB)                │
│                                                              │
│  state.user = {selectedPart, selectedPinIdx}                 │
│  state.agent = {highlights: Set, focused: null, dimmed: …,   │
│                  annotations: Map, arrows: Map, …}           │
│                                                              │
│  window.Boardview = {                                        │
│    apply(ev) { switch(ev.type) { … } requestRedraw() }       │
│    highlight({refdes, color, additive}),                     │
│    focus({refdes, bbox, zoom, auto_flipped}),                │
│    reset(), flip({new_side}), annotate({refdes, label, id}),│
│    dim_unrelated(), highlight_net({net, pin_refs}),          │
│    show_pin({refdes, pin, pos}), draw_arrow({from, to, id}), │
│    filter({prefix}), measure({…}),                           │
│    layer_visibility({layer, visible}),                       │
│  }                                                           │
│                                                              │
│  draw() superpose user (stroke violet) + agent (stroke cyan) │
└──────────────────────────────────────────────────────────────┘
```

### Invariants

- **Un seul tool call `bv_*` = un `tool_result` (texte court) + un event WS (payload visuel)**, dans cet ordre. Le `tool_result` ne contient jamais le `event` — sinon on double le coût en tokens et l'agent n'a pas besoin du payload binaire.
- **L'agent ne voit jamais un tool qu'il ne peut pas appeler avec succès.** Si `session.board is None`, la famille `bv_*` est absente du manifest. Pas de `{ok: false, reason: "no-board"}` inutiles.
- **Sélection user ≠ highlights agent.** Le tech clique pour inspecter une puce ; l'agent surligne ses suspects ; les deux coexistent visuellement. Le rendu superpose. Un `bv_reset_view` appelé par l'agent ne touche pas `state.user.selectedPart`.
- **Ordre d'envoi sur le WS garanti par l'itération Python.** Pour un tour donné, la boucle `for block in response.content` (runtime direct) parcourt les blocs dans l'ordre produit par le modèle. Chaque bloc `tool_use` émet `{type: "tool_use"}` **puis** son event `boardview.*` avant de passer au suivant. Pour le runtime managed, l'ordre est celui d'itération sur `stop.event_ids` dans le handler `requires_action` (liste Python, ordre préservé). Donc si l'agent émet `bv_focus(U7)` + `bv_highlight(U7)` dans la même réponse, le frontend reçoit : `tool_use(focus)` → `boardview.focus` → `tool_use(highlight)` → `boardview.highlight`. Pas d'entrelacement.
- **Texte agent toujours passé par `sanitize_agent_text`** avant `ws.send_json`. C'est le second filet de sécurité contre l'hallucination (cf. §2). No-op quand `session.board is None`.
- **`window.Boardview` n'est jamais absent côté frontend** — un stub `{__pending: [], apply(ev) { this.__pending.push(ev) }}` est installé au chargement de `main.js`, avant même l'ouverture du panneau agent. `initBoardview` draine `__pending` au premier run. Donc un `bv_highlight` envoyé avant que le tech ait navigué sur `#pcb` est simplement mis en attente — quand il ouvre `#pcb`, les events bufférisés s'appliquent. Pas de perte silencieuse.

---

## 5. Tools exposés à l'agent

### 5.1 Famille `mb_*` (toujours exposée)

| Tool | Input | Retour |
|---|---|---|
| `mb_get_component` | `refdes: str` | **RESTRUCTURÉ (breaking)** — voir forme complète + 4 cas ci-dessous |
| `mb_get_rules_for_symptoms` | `symptoms: [str], max_results?: int=5` | inchangé |
| `mb_list_findings` | `limit?: int=20, filter_refdes?: str` | inchangé |
| `mb_record_finding` | `refdes, symptom, confirmed_cause, mechanism?, notes?` | inchangé |

**Forme cible `mb_get_component` :**

```python
{
  "found": bool,
  "canonical_name": str,           # présent si found
  "memory_bank": {                 # None si absent de memory bank
    "role": str, "package": str, "aliases": [str],
    "typical_failure_modes": [str], "description": str,
  } | None,
  "board": {                       # None si absent du board parsé
    "side": "top" | "bottom",
    "pin_count": int,
    "bbox": [[x1, y1], [x2, y2]],  # en mils
    "nets": [str],                 # noms des nets connectés
  } | None,
  # schematic: intentionnellement absent (pas de clé) tant que api/vision/
  # est stub — ajouté par le hook sch_* le jour où le parseur atterrit.
  "closest_matches": [str],        # présent si not found (union memory + board)
}
```

**Les 4 cas de présence explicites :**

| # | Memory bank | Board chargé et refdes dedans | Retour |
|---|---|---|---|
| 1 | ✅ trouvé | ✅ résolu | `{found: true, canonical_name, memory_bank: {...}, board: {...}}` |
| 2 | ✅ trouvé | ❌ (pas de board, ou refdes absent) | `{found: true, canonical_name, memory_bank: {...}, board: null}` |
| 3 | ❌ absent | ✅ résolu (pièce physique sans entrée memory bank) | `{found: true, canonical_name, memory_bank: null, board: {...}}` |
| 4 | ❌ absent | ❌ | `{found: false, closest_matches: [...]}` — pas de `memory_bank` ni `board` |

Les clés `memory_bank` et `board` sont **toujours présentes** quand `found: true` (valeur `null` si la source ne contient pas le refdes). Ça simplifie la logique côté agent : il teste `result["memory_bank"] is None` plutôt que `"memory_bank" in result`. Quand `found: false`, ces clés sont omises (l'agent n'a que `closest_matches` à traiter).

**`closest_matches` en cas 4** : union des candidats Levenshtein de la memory bank (`dictionary.json` entries) ET du board parsé (via `suggest_similar`), dédoublonnée, top 5.

**Signature Python mise à jour** :

```python
def mb_get_component(
    *, device_slug: str, refdes: str, memory_root: Path,
    session: SessionState | None = None,  # nouveau
) -> dict[str, Any]:
```

### 5.2 Famille `bv_*` (exposée ssi `session.board is not None`)

| Tool public | Input schema | Handler backend | WS event émis |
|---|---|---|---|
| `bv_highlight` | `{refdes: str \| [str], color?: "accent"\|"warn"\|"mute"="accent", additive?: bool=false}` | `boardview.highlight_component` | `boardview.highlight` |
| `bv_focus` | `{refdes: str, zoom?: float=2.5}` | `boardview.focus_component` | `boardview.focus` |
| `bv_reset_view` | `{}` | `boardview.reset_view` | `boardview.reset_view` |
| `bv_flip` | `{preserve_cursor?: bool=false}` | `boardview.flip_board` | `boardview.flip` |
| `bv_annotate` | `{refdes: str, label: str}` | `boardview.annotate` | `boardview.annotate` |
| `bv_dim_unrelated` | `{}` | `boardview.dim_unrelated` | `boardview.dim_unrelated` |
| `bv_highlight_net` | `{net: str}` | `boardview.highlight_net` | `boardview.highlight_net` |
| `bv_show_pin` | `{refdes: str, pin: int}` | `boardview.show_pin` | `boardview.show_pin` |
| `bv_draw_arrow` | `{from_refdes: str, to_refdes: str}` | `boardview.draw_arrow` | `boardview.draw_arrow` |
| `bv_measure` | `{refdes_a: str, refdes_b: str}` | `boardview.measure_distance` | `boardview.measure` |
| `bv_filter_by_type` | `{prefix: str}` | `boardview.filter_by_type` | `boardview.filter` |
| `bv_layer_visibility` | `{layer: "top"\|"bottom", visible: bool}` | `boardview.layer_visibility` | `boardview.layer_visibility` |

### 5.3 Mapping dispatch

```python
# api/agent/dispatch_bv.py (nouveau module)
from api.tools import boardview as bv

BV_DISPATCH = {
    "bv_highlight":        bv.highlight_component,
    "bv_focus":            bv.focus_component,
    "bv_reset_view":       bv.reset_view,
    "bv_flip":             bv.flip_board,
    "bv_annotate":         bv.annotate,
    "bv_dim_unrelated":    bv.dim_unrelated,
    "bv_highlight_net":    bv.highlight_net,
    "bv_show_pin":         bv.show_pin,
    "bv_draw_arrow":       bv.draw_arrow,
    "bv_measure":          bv.measure_distance,
    "bv_filter_by_type":   bv.filter_by_type,
    "bv_layer_visibility": bv.layer_visibility,
}

def dispatch_bv(session, name, payload) -> dict:
    handler = BV_DISPATCH.get(name)
    if handler is None:
        return {"ok": False, "reason": "unknown-tool"}
    return handler(session, **payload)
```

### 5.4 Protocole de retour unifié

Tous les handlers `bv_*` renvoient déjà `{ok: bool, summary?: str, event?: _BVEvent, reason?: str, suggestions?: list}`. On **scinde** ce retour en deux flux au niveau du runtime :

```python
# dans runtime_direct.py / runtime_managed.py
result = dispatch_bv(session, block.name, block.input)

# 1. tool_result → agent
tool_result_payload = {k: v for k, v in result.items() if k != "event"}
tool_results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": json.dumps(tool_result_payload),
})

# 2. event WS → frontend (uniquement si ok et event présent)
if result.get("ok") and result.get("event") is not None:
    await ws.send_json(result["event"].model_dump(by_alias=True))
```

On appelle `event.model_dump(by_alias=True)` pour **tous** les events, uniformément. Note technique : `DrawArrow` a déjà `serialize_by_alias=True` dans son `model_config` (cf. `ws_events.py:85`) — donc `model_dump()` sans argument y sérialise déjà `from_` → `from`. L'argument `by_alias=True` est **redondant** pour ce cas-là, et **neutre** pour les autres events (aucun alias). On le garde explicite dans le code du runtime pour : (a) une seule ligne uniforme couvre les 12 tools, (b) robustesse si un futur event ajoute un alias sans re-configurer `serialize_by_alias`.

---

## 6. Flux bout-en-bout — un tour réel

**Scénario :** tech ouvre l'app, device `mnt-reform-motherboard`, tape « J'ai pas d'image HDMI ».

1. **WS handshake** : client connecte `/ws/diagnostic/mnt-reform-motherboard?tier=fast`
2. **Backend** instancie `SessionState.from_device("mnt-reform-motherboard")` → charge `.kicad_pcb` → `session.board = Board(parts=[…], nets=[…], …)`
3. **Manifest construit** : 4 `mb_*` + 12 `bv_*` = 16 tools (pas de `sch_*`, schematic stub)
4. **System prompt** : *« Pour ce device : memory bank ✅, boardview ✅, schematic ❌. Quand tu identifies un composant, appelle `mb_get_component` pour obtenir le rôle et la topologie, puis `bv_highlight` / `bv_focus` pour le montrer au tech. »*
5. **User** envoie `{type: "message", text: "J'ai pas d'image HDMI"}`
6. **Agent tour 1** : appelle `mb_get_rules_for_symptoms(symptoms=["no HDMI output", "black display"])` → reçoit une règle pointant U7 (HDMI framer) comme suspect principal
7. **Agent tour 2** : appelle `mb_get_component(refdes="U7")` → reçoit `{found: true, memory_bank: {role: "HDMI framer TFP410", package: "TQFP-64", typical_failure_modes: [...]}, board: {side: "top", pin_count: 64, bbox: [[…]], nets: ["HDMI_CLK_P", "HDMI_D0_P", …]}}`
8. **Agent tour 3** : appelle `bv_focus(refdes="U7", zoom=3.0)` — backend :
   - retour handler : `{ok: true, summary: "Focused on U7 (top).", event: Focus(refdes="U7", bbox=…, zoom=3.0)}`
   - WS envoie `{type: "tool_use", name: "bv_focus", input: {refdes: "U7", zoom: 3.0}}`
   - WS envoie `{type: "boardview.focus", refdes: "U7", bbox: [[…]], zoom: 3.0, auto_flipped: false}`
   - `tool_result` → agent : `{"ok": true, "summary": "Focused on U7 (top)."}`
9. **Frontend** :
   - `llm.js` voit `"boardview.focus"` → appelle `window.Boardview.apply(payload)` → `state.agent.focused = "U7"` + pan/zoom animé
   - `llm.js` affiche en parallèle dans le chat log : *« → bv_focus U7 »*
10. **Agent tour 4** : appelle `bv_highlight(refdes="U7", color="warn")` — même pattern, stroke ambre sur U7
11. **Agent tour 5** (fin) : réponse texte *« J'ai mis en avant U7, le framer TFP410. Les modes d'échec typiques : alim 3V3 qui chute sous charge, clock HDMI masquée, joints de BGA fatigués. Commence par mesurer 3V3 sur la broche 1 — tu veux que je te la pointe ? »*

Temps total : ~4-6 s (tier `fast` = Haiku). Le tech voit U7 surligné et zoomé avant même d'avoir fini de lire la réponse texte.

---

## 7. Points de câblage (gap analysis)

### Backend (8 points)

1. **Schemas JSON `bv_*`** — construire la liste `BV_TOOLS = [...]` (12 entries, chacun avec `name`, `description`, `input_schema`). Module dédié `api/agent/manifest.py`.
2. **Dispatch `bv_*`** — module `api/agent/dispatch_bv.py` avec `BV_DISPATCH` (cf. § 5.3). Appelé depuis `_dispatch_tool` des deux runtimes si `name.startswith("bv_")`.
3. **Émission `event` WS** — dans les deux runtimes, après chaque dispatch `bv_*` réussi, `ws.send_json(event.model_dump(by_alias=True))`.
4. **`SessionState` wiring** — nouveau helper `SessionState.from_device(device_slug) -> SessionState` qui cherche un fichier board dans `board_assets/{slug}.*`. **Priorité d'extensions fixée : `.kicad_pcb` > `.brd`** (plus riche, plus moderne, aligné avec le commit `web/boards/` qui contient les deux). Parse via `api/board/parser/parser_for(path)` ; si aucun fichier board n'est trouvé OU si le parse lève, retourne `SessionState()` (board = None) sans propager — l'agent n'aura tout simplement pas les `bv_*` dans son manifest. L'exception est loggée côté serveur avec `logger.warning("board load failed for %s: %s", slug, exc)`. Appelé en début de `run_diagnostic_session_{direct,managed}`.
5. **`mb_get_component` restructuré + agrégé** — réécriture avec la nouvelle signature `mb_get_component(*, device_slug, refdes, memory_root, session: SessionState | None = None)`. Retour dans la forme §5.1 (4 cas explicites). **Refactor breaking** : les tests `tests/agent/test_mb_tools.py` qui lisent `result["role"]` passent à `result["memory_bank"]["role"]`. Le dispatch dans les runtimes passe la session.
6. **Manifest dynamique** — `build_tools_manifest(session: SessionState) -> list[dict]` dans `api/agent/manifest.py`. Toujours inclut les 4 `MB_TOOLS`. Ajoute les 12 `BV_TOOLS` ssi `session.board is not None`.
7. **`render_system_prompt` — runtime DIRECT uniquement.** Fonction `render_system_prompt(session: SessionState, device_slug: str) -> str` qui remplace la constante `SYSTEM_PROMPT_DIRECT`. Liste les capabilities actives (« memory bank ✅, boardview ✅/❌, schematic ❌ ») et rappelle la discipline tool-first. **Non utilisée par `runtime_managed`** : le prompt de l'agent MA est porté côté Anthropic via `managed_ids.json` et ne peut pas être remplacé par session. Cette asymétrie est assumée — le manifest reste authoritative pour les deux runtimes (si `bv_*` absent du manifest, l'agent ne peut pas les appeler, point). Le plan d'implémentation pourra proposer d'aligner le prompt MA via un `user.message` synthétique en début de session (non-bloquant, stretch).
8. **Sanitizer refdes post-hoc** — module `api/agent/sanitize.py`, fonction `sanitize_agent_text(text, board) -> (clean_text, unknown_list)` (cf. §2). Appliqué dans les deux runtimes juste avant chaque `ws.send_json({"type": "message", ...})`. Les refdes inconnus sont wrapped `⟨?U999⟩` et loggés. No-op si `board is None`.

### Frontend (4 points)

9. **Stub précoce `window.Boardview`** — dans `web/js/main.js`, en tout début de fichier (avant même le router init), installer :
   ```js
   if (!window.Boardview) {
     window.Boardview = {
       __pending: [],
       apply(ev) { this.__pending.push(ev); },
     };
   }
   ```
   Ça garantit que n'importe quel `payload.type.startsWith("boardview.")` reçu sur le WS avant que `initBoardview` n'ait tourné soit mis en buffer, pas perdu.

10. **Listener WS** — dans `web/js/llm.js`, avant le `switch (payload.type)`, tester `if (payload.type?.startsWith("boardview.")) { window.Boardview.apply(payload); return; }`. Le `return` évite de logger ces events dans le chat (le chat a déjà vu le `tool_use` correspondant).

11. **API publique `window.Boardview` réelle** — dans `web/brd_viewer.js`, après le `initBoardview` export, **remplacer le stub** par l'implémentation complète, puis drainer `__pending` :
    ```js
    const pending = window.Boardview?.__pending || [];
    window.Boardview = {
      apply(ev) { /* switch sur ev.type, route */ requestRedraw(); },
      highlight({refdes, color, additive}) { /* mute state.agent.highlights */ },
      focus({refdes, bbox, zoom, auto_flipped}) { /* anime pan/zoom */ },
      reset() { /* clear state.agent.*, preserve state.user */ },
      flip({new_side}) { /* mute state.side */ },
      annotate({refdes, label, id}) { /* state.agent.annotations.set(id, …) */ },
      dim_unrelated() { /* state.agent.dimmed = true */ },
      highlight_net({net, pin_refs}) { /* state.agent.net = net */ },
      show_pin({refdes, pin, pos}) { /* anime un pulse sur la broche */ },
      draw_arrow({from, to, id}) { /* state.agent.arrows.set(id, …) */ },
      filter({prefix}) { /* state.agent.filter = prefix */ },
      measure({from_refdes, to_refdes, distance_mm}) { /* overlay temporaire */ },
      layer_visibility({layer, visible}) { /* state.layer_visibility[layer] = visible */ },
    };
    // Drain anything queued before we were ready
    for (const ev of pending) window.Boardview.apply(ev);
    ```

12. **Split user/agent state** — dans `brd_viewer.js`, renommer le state global actuel. **Risque de migration** : ~15 call sites à mettre à jour (`state.selectedPart`, `state.selectedPinIdx` référencés dans `draw()`, `attachInteraction`, `mountCanvas`, `updateInspector`, `hitTestPart`, etc.). Le plan d'implémentation devra lister exhaustivement les lignes impactées avant d'attaquer, sinon risque de casser la sélection user. Concrètement :
    - `state.selectedPart` → `state.user.selectedPart`
    - `state.selectedPinIdx` → `state.user.selectedPinIdx`
    - Ajouter `state.agent = {highlights: new Set(), focused: null, dimmed: false, annotations: new Map(), arrows: new Map(), net: null, filter: null}`
    - `draw()` superpose : stroke violet (`--violet`) pour `state.user.selectedPart`, stroke cyan (`--cyan`) pour `state.agent.highlights`. Si un refdes est dans les deux, la couleur user gagne (le tech reste maître de sa sélection).

### Doc (1 point)

13. **`CLAUDE.md` Hard Rule #5** — remplacer la formulation par celle du § 2 de ce spec (tool discipline + sanitizer post-hoc).

---

## 8. Error handling

| Situation | Traitement |
|---|---|
| `session.board is None` au moment d'un `bv_*` | **Ne peut pas arriver** : si board absent, le manifest n'expose pas la famille `bv_*`. L'agent ne voit pas le tool, ne l'appelle pas. Defense-in-depth : le handler renvoie `{ok: false, reason: "no-board-loaded"}` si appelé malgré tout (code existant, ligne `boardview.py:14`). |
| Refdes inconnu (`bv_highlight("U99")`) | Handler renvoie `{ok: false, reason: "unknown-refdes", suggestions: [...]}` (Levenshtein via `suggest_similar`). `tool_result` contient ces champs, l'agent choisit : ask user, ou retente avec un `suggestion`. **Aucun event WS émis** (pas de mutation visuelle pour un échec). |
| Net inconnu (`bv_highlight_net("VGA_NOT_EXIST")`) | Idem, `reason: "unknown-net"` (handler existant retourne `suggestions: []`). Le plan d'implémentation peut ajouter `suggest_similar_net` pour symétrie — non bloquant. |
| Handler lève une exception | Runtime catche au niveau dispatch, envoie un `tool_result` avec `{ok: false, reason: "handler-exception", error: str(exc)}`. Log côté backend. Pas d'event WS. |
| WS déconnecté pendant un tour | Comportement existant : `WebSocketDisconnect` capturé, session MA archivée. Les events `boardview.*` en cours sont simplement jetés — pas de retry (le tech reconnecte = nouvelle session). |
| Frontend reçoit un `boardview.*` avec refdes absent du board côté front | Scenario : le board côté backend et frontend ont divergé (rare, mais possible si hot-reload). `window.Boardview.apply` log un warning console et ignore l'event. Pas de crash. |
| `window.Boardview` pas encore monté quand un event arrive | **Couvert par le stub précoce** (cf. §7 point 9). Le stub installé dans `main.js` collecte les events dans `__pending` ; `initBoardview` les rejoue au premier run. Aucun event perdu. **Limite** : si l'app est rechargée sans jamais visiter `#pcb`, le buffer est perdu avec le refresh — comportement attendu (nouvelle session). |
| `SessionState.from_device` échoue (parse, I/O, fichier absent) | `SessionState()` (board=None) retourné, warning loggé. Agent démarre avec `mb_*` seulement, pas de `bv_*` au manifest. Le tech voit en session_ready que le board n'est pas chargé (à surfacer dans le payload). |
| Sanitizer flag un refdes-shaped token hors board (faux positif type `USB3`) | Acceptable by design — le token est wrapped `⟨?USB3⟩`. Signal visuel au tech que l'agent a utilisé un identifiant qu'il ne peut pas valider. Le tech reste juge. Si le taux de faux positifs devient gênant, ajuster le regex (§2). |

---

## 9. Tests

### Unit (pytest)

- `tests/agent/test_manifest_dynamic.py`
  - `build_tools_manifest(session_no_board)` → 4 `mb_*`, 0 `bv_*`
  - `build_tools_manifest(session_with_board)` → 4 `mb_*`, 12 `bv_*`
  - Chaque entrée du manifest a `name`, `description`, `input_schema` valides (schéma JSON Schema)
- `tests/agent/test_dispatch_bv.py`
  - Pour chaque tool `bv_*` : appel avec payload valide → `{ok: true, summary: str, event: _BVEvent}`
  - `bv_highlight` avec refdes inconnu → `{ok: false, reason: "unknown-refdes", suggestions: [...]}`, `event` absent
  - `bv_focus` avec `auto_flipped` : session initialisée avec `session.layer = "bottom"` et fixture part dont `Layer` est `TOP` → appel `focus_component(session, refdes=part.refdes)` → `event.auto_flipped is True` et `session.layer == "top"` après l'appel
  - Dispatch d'un nom de tool inconnu → `{ok: false, reason: "unknown-tool"}`
- `tests/agent/test_mb_aggregation.py` — les 4 cas de §5.1 :
  - Cas 1 (les deux présents) → clés `memory_bank` et `board` non-nulles
  - Cas 2 (memory bank seule) → `memory_bank: {...}, board: null`
  - Cas 3 (board seul) → `memory_bank: null, board: {...}`
  - Cas 4 (ni l'un ni l'autre) → `{found: false, closest_matches: [...]}`, pas de clé `memory_bank` ni `board`
  - Jamais de clé `schematic` (stub absent)
- `tests/agent/test_sanitize.py`
  - `sanitize_agent_text("Check U7 and U999 please", board_with_U7)` → `("Check U7 and ⟨?U999⟩ please", ["U999"])`
  - `sanitize_agent_text("Any refdes here", None)` → `("Any refdes here", [])` (no-op sans board)
  - Tokens non-refdes (`HDMI_D0`, `VDD_3V3`, `GPIO12`) → non flaggés
  - Tokens refdes-shaped mais absents (`USB3` si absent du board) → flaggés
- `tests/agent/test_session_from_device.py`
  - Slug avec `.kicad_pcb` dans `board_assets/` → `SessionState.board is not None`
  - Slug sans fichier → `SessionState.board is None`, pas d'exception
  - Slug avec fichier corrompu → `SessionState.board is None`, warning loggé
  - Priorité `.kicad_pcb` > `.brd` si les deux existent
- `tests/agent/test_system_prompt.py` (runtime DIRECT seulement)
  - `render_system_prompt(session_no_board)` mentionne explicitement l'absence du boardview
  - `render_system_prompt(session_with_board)` mentionne la disponibilité du boardview
  - Test de présence par substring (« memory bank » / « boardview ») plutôt que texte littéral complet — moins fragile

### Intégration (pytest-asyncio)

- `tests/agent/test_ws_flow.py` (nouveau)
  - Mock AsyncAnthropic qui force un tool_use `bv_highlight(refdes="U1")` sur un board fixture → vérifier séquence WS : `{type: "tool_use"}` puis `{type: "boardview.highlight", refdes: ["U1"], …}` puis prochaine itération
  - Mock qui force `bv_highlight(refdes="U999")` (inconnu) → vérifier qu'**aucun event `boardview.*`** n'est émis, mais `tool_result` contient `reason: "unknown-refdes"`
  - Mock qui force une réponse texte avec `"U999 is suspect"` → vérifier que le message WS émis contient `"⟨?U999⟩"`, pas `"U999"` brut
  - Mock qui force `bv_focus` puis `bv_highlight` dans la même `response.content` → vérifier ordre strict `tool_use(focus), boardview.focus, tool_use(highlight), boardview.highlight`
- Vérification que le `tool_result` envoyé à l'agent **ne contient JAMAIS la clé `event`** (garant du design §5.4, cœur du scindage texte/visuel) — assertion directe sur `messages[-1]["content"]`

### Manuel (browser)

**Obligatoire avant commit (cf. memory `feedback_visual_changes_require_user_verify`) :**
- Ouvrir `/#pcb`, lancer panneau agent avec `⌘+J`, device `mnt-reform-motherboard`
- Taper « highlight U1 » → vérifier : chat log montre `→ bv_highlight U1`, canvas PCB surligne U1 en cyan
- Taper « focus C29 » → canvas pan/zoom sur C29
- Cliquer manuellement sur R5 → stroke violet sur R5 **coexiste** avec stroke cyan sur U1/C29 (superposition user+agent)
- Taper « highlight R5 » **pendant** que R5 est encore sélectionné user → vérifier que stroke violet (user) l'emporte visuellement sur stroke cyan (agent) — règle de résolution §7.12
- Taper « reset view » → agent state clear, user state (R5 sélectionné) **préservé**
- Buffer test : ouvrir l'app directement sur `#home` (pas `#pcb`), ouvrir panneau agent `⌘+J`, taper « highlight U1 » (déclenche un `bv_highlight` avant init du PCB). Naviguer vers `#pcb`. Vérifier que U1 est bien surligné quand le canvas monte (drain du buffer `__pending`).
- Hallucination test : demander à l'agent une question qui l'amène à improviser un refdes non-existent (ex. « quel est le rôle de U42 ? » si U42 n'existe pas). Vérifier que le chat affiche `⟨?U42⟩` (sanitizer a bien wrapped), pas `U42` brut.

### Tests non requis

- Test du renderer (`brd_viewer.js`) — pas de framework de test JS dans le repo, validation visuelle via browser suffit
- Test du parseur `.kicad_pcb` — déjà couvert par les tests existants

---

## 10. Impact `CLAUDE.md`

Diff attendu :

```diff
-5. **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the
-   agent mentions must be validated against parsed board data *before* being
-   shown to the user. Tools that cannot answer return structured
-   null/unknown — never fake data.
+5. **No hallucinated component IDs.** Defense in depth, two layers.
+   (1) Tool discipline: every refdes the agent surfaces must originate from
+   a tool lookup (`mb_get_component` for memory bank + board aggregation, or
+   a `bv_*` tool that cross-checks the parsed board). These tools never
+   fabricate — they return `{found: false, closest_matches: [...]}` for
+   unknown refdes, and the system prompt instructs the agent to pick from
+   `closest_matches` or ask the user. (2) Post-hoc sanitizer: every outbound
+   agent `message` text is scanned for refdes-shaped tokens (regex
+   `\b[A-Z]{1,3}\d{1,4}\b`) and, when a board is loaded, validated against
+   `session.board.part_by_refdes`. Unknown matches are wrapped as
+   `⟨?U999⟩` in the delivered text and logged server-side.
```

Aucune autre ligne de `CLAUDE.md` ne bouge. La règle reste listée dans "Hard rules — NEVER violate" — son esprit est préservé et sa mécanique devient réellement effective pour la première fois (l'ancienne formulation promettait un gate jamais implémenté).

---

## 11. Non-objectifs explicites

Ce spec **ne couvre pas** :

- **Streaming token-par-token de la réponse agent** — le CLAUDE.md dit « Streaming over polling » mais le runtime actuel envoie la réponse complète par bloc. C'est un chantier séparé.
- **Un tool `bv_upload_board`** qui permettrait à l'agent de charger un board à la volée — hors scope, c'est une action utilisateur (drag-drop).
- **Un tool `bv_query_connectivity`** pour que l'agent interroge « quels composants sont sur le net VDD ? » sans avoir à tout résoudre via `mb_get_component` chaîné. Candidat pertinent pour v2 si l'usage montre que l'agent fait beaucoup d'allers-retours pour cartographier la topologie. À la place, on peut enrichir `mb_get_component.board.nets` avec aussi les pin_refs des nets pour donner le contexte d'un coup.
- **Un tool `bv_export_annotations`** pour figer les annotations posées par l'agent dans un rapport — future étape « journal de diagnostic ».

---

## 12. Décisions techniques validées (brainstorming + review)

| # | Décision | Justification |
|---|---|---|
| 1 | Familles `mb_*` / `bv_*` séparées, pas fusionnées | `mb_*` = lecture toujours dispo ; `bv_*` = mutation UI dépendante du board. Séparation source/action. |
| 2 | Manifest dynamique (exposition conditionnelle par famille) | L'agent ne tente pas un tool qui échouerait toujours ; le manifest est la source authoritative — le system prompt est secondaire. |
| 3 | `mb_get_component` agrégateur multi-source, **forme restructurée breaking** | Un seul appel retourne ce qui existe (memory bank + board + schematic futur). Forme `{memory_bank: {...} \| null, board: {...} \| null}` nommée plutôt que plate — évolutivité + non-ambiguïté pour les 4 cas explicites §5.1. Breaking change assumé (tests existants migrés). |
| 4 | **Hard Rule #5 conservée en 2 couches** (tool discipline + sanitizer post-hoc) | Option B3 (β) : la règle reste dure, et sa mécanique devient réellement effective. Regex léger ~30 lignes, faux positifs acceptables comme signal visuel au tech. Alternative rejetée : assumer l'affaiblissement. |
| 5 | Noms publics `bv_*` distincts des noms de handlers | Clarté du namespace agent (`bv_highlight`) indépendante de l'organisation interne (`highlight_component`). Mapping dans `BV_DISPATCH`. |
| 6 | Retour scindé : `tool_result` texte vs `event` WS visuel | Économise des tokens côté agent (pas besoin du payload binaire), respecte la séparation « l'agent sait que ça a marché / le frontend sait quoi muter ». Garant testé en §9 (assertion que `tool_result` n'a jamais de clé `event`). |
| 7 | Split `state.user` vs `state.agent` dans le renderer | Le tech garde sa sélection ; l'agent ne l'écrase pas. Rendu superpose (violet user, cyan agent). Migration ~15 call sites, listée au plan d'implémentation. |
| 8 | **Buffer `window.Boardview.__pending`** (stub précoce + drain à l'init) | Corrige le trou de la v1 du spec où les events étaient perdus si `#pcb` pas monté. Scénario réel : tech ouvre panneau agent avant de naviguer `#pcb`. 10 lignes, aucun event perdu. |
| 9 | `render_system_prompt` pour le runtime DIRECT uniquement | Option B1 (α) : le runtime MA porte son prompt côté Anthropic, on ne le remplace pas par session. Asymétrie assumée — le manifest reste authoritative. Alignement MA via `user.message` synthétique possible en stretch, non bloquant. |
| 10 | Priorité parser `.kicad_pcb` > `.brd` dans `SessionState.from_device` | Par défaut le plus riche ; fallback automatique. Aligné avec `board_assets/` qui stocke les deux pour `mnt-reform-motherboard`. |
| 11 | Ordre `tool_use` → event WS dans une même réponse agent préservé par itération Python | Documenté en §4 invariants. La boucle `for block in response.content` (direct) et `for eid in stop.event_ids` (managed) garantissent l'ordre. |
| 12 | Hook `sch_*` présent dans `build_tools_manifest` (non exposé tant que `schematic is None`) | Le jour où `api/vision/` parse les PDF, on branche sans toucher l'agent ni les 2 runtimes. L'attribut `schematic: Any = None` est ajouté au `SessionState` dès ce chantier pour fixer l'interface. |
