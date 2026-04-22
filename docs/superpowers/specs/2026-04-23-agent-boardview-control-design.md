# Pilotage du boardview par l'agent — design spec

**Date :** 2026-04-23
**Scope :** câblage bout-en-bout permettant à l'agent de diagnostic (Opus 4.7 / Sonnet 4.6 / Haiku 4.5) de piloter le canvas PCB du workbench via tool calls. Famille d'outils `bv_*` (nouveau) + agrégation multi-source de `mb_get_component` + manifest dynamique par session + API publique sur le renderer existant + listener WebSocket côté frontend.
**Hors scope :** parsing du schematic PDF (`api/vision/` reste stub — `sch_*` est un hook prévu, pas livré ici). Renderer PCB (`web/brd_viewer.js` déjà livré). Pipeline knowledge (`api/pipeline/` inchangé).

---

## 1. Contexte

L'agent diagnostic connaît aujourd'hui 4 outils `mb_*` (lecture memory bank : `mb_get_component`, `mb_get_rules_for_symptoms`, `mb_list_findings`, `mb_record_finding`) mais ne peut pas **montrer** ce qu'il trouve sur le canvas PCB. Le tech doit relire un refdes dans le chat, puis cliquer lui-même dans `brd_viewer.js` pour le localiser. L'agent reste aveugle au contexte visuel partagé et muet à l'action.

Presque toute la tuyauterie est déjà en place :
- 12 handlers prêts dans `api/tools/boardview.py` (highlight, focus, annotate, dim_unrelated, …)
- 13 enveloppes Pydantic WS dans `api/tools/ws_events.py` avec `type = "boardview.<verb>"`
- `SessionState` (`api/session/state.py`) qui porte `board`, `highlights`, `annotations`, `arrows`, etc.
- Renderer canvas complet (`web/brd_viewer.js`, 1139 lignes) avec hit-test, zoom/pan, inspector
- WebSocket `/ws/diagnostic/{slug}` avec deux runtimes (managed + direct) et relai `tool_use` au frontend

Ce qui manque est du **câblage**, pas de l'invention. Ce spec liste exactement les 10 points de câblage, l'architecture cible, et les changements de règles.

---

## 2. Règles dures — mise à jour de la Hard Rule #5

Le `CLAUDE.md` actuel dit :

> **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the agent mentions must be validated against parsed board data *before* being shown to the user. Tools that cannot answer return structured null/unknown — never fake data.

La formulation « validated *before* being shown » évoque un **gate post-hoc** (middleware qui intercepte la réponse de l'agent avant envoi au frontend). Ce gate n'a jamais été implémenté et n'est plus nécessaire dans l'architecture cible : les tools sont **la seule source de refdes** pour l'agent, et chaque tool de lecture retourne `{found: false, closest_matches: [...]}` pour l'inconnu. L'hallucination devient impossible par construction.

**Nouvelle formulation retenue :**

> **No hallucinated component IDs.** Every refdes the agent surfaces must originate from a tool lookup (`mb_get_component`, or a `bv_*` tool that cross-checks the parsed board). These tools never fabricate — they return `{found: false, closest_matches: [...]}` for unknown refdes, and the agent is instructed (system prompt) to pick from `closest_matches` or ask the user for clarification, never invent. No post-hoc gate: verification is enforced at the tool boundary.

L'esprit est préservé (pas de refdes inventé), la mécanique change (tool discipline au lieu de middleware). Le `SYSTEM_PROMPT_DIRECT` actuel contient déjà cette instruction (lignes 33-36 de `api/agent/runtime_direct.py`), à étendre pour mentionner l'agrégation multi-source de `mb_get_component`.

Les autres règles dures (Apache 2.0, deps permissives, open hardware only, all code from scratch) sont inchangées.

---

## 3. Scope

### In scope

- `api/agent/runtime_direct.py` + `runtime_managed.py` — manifest dynamique, dispatch `bv_*`, émission WS des events
- `api/agent/tools.py` — `mb_get_component` enrichi (agrégation memory bank + board)
- `api/tools/boardview.py` — aucun changement fonctionnel (handlers réutilisés tels quels), potentiellement quelques corrections d'import / signatures
- `api/session/state.py` — éventuellement ajout d'un helper `SessionState.from_device(device_slug)` qui charge le board parsé
- `web/js/llm.js` — listener `boardview.*` qui délègue à `window.Boardview.apply(payload)`
- `web/brd_viewer.js` — API publique `window.Boardview` + split user/agent state
- `CLAUDE.md` — réécriture Hard Rule #5
- Tests : `tests/agent/test_dispatch_bv.py`, `tests/agent/test_manifest_dynamic.py`, `tests/agent/test_mb_aggregation.py`

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
│  system_prompt = render_capabilities(session) + BASE_PROMPT  │
│    # « Tu disposes de : memory bank ✅ | boardview ✅ »      │
│                                                              │
│  loop:                                                       │
│    response = client.messages.create(…, tools=manifest)      │
│    for block in response.content:                            │
│      if block.type == "tool_use":                            │
│        ws.send_json({type: "tool_use", name, input})         │
│        result = _dispatch(session, block.name, block.input)  │
│        if result.get("event"):                               │
│          ws.send_json(result["event"].model_dump())          │
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
- **Ordre d'envoi sur le WS : `tool_use` → `boardview.<verb>` → (prochaine itération agent).** Le frontend voit d'abord l'intention (chat log : « → bv_highlight U7 »), puis la mutation du canvas.

---

## 5. Tools exposés à l'agent

### 5.1 Famille `mb_*` (toujours exposée)

| Tool | Input | Retour |
|---|---|---|
| `mb_get_component` | `refdes: str` | **AGRÉGÉ** : `{found, canonical_name, memory_bank: {role, package, aliases, typical_failure_modes, description}, board?: {side: "top"\|"bottom", pin_count: int, bbox: [[x1,y1],[x2,y2]], nets: [str]}}`. Clé `board` présente ssi `session.board is not None` et refdes résolu. Clé `schematic` jamais présente tant que `api/vision/` est stub. Si le refdes est introuvable dans les deux sources : `{found: false, closest_matches: [...]}`. |
| `mb_get_rules_for_symptoms` | `symptoms: [str], max_results?: int=5` | inchangé |
| `mb_list_findings` | `limit?: int=20, filter_refdes?: str` | inchangé |
| `mb_record_finding` | `refdes, symptom, confirmed_cause, mechanism?, notes?` | inchangé |

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

On appelle `event.model_dump(by_alias=True)` pour **tous** les events, uniformément. Ça garantit que `DrawArrow.from_` est sérialisé avec la clé `from` attendue côté frontend (cf. `ws_events.py:85` — `alias="from"` sur le champ `from_`). Les autres events n'ayant pas d'alias, `by_alias=True` est neutre pour eux ; une seule ligne de code dans le runtime couvre les 12 tools.

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

### Backend (6 points)

1. **Schemas JSON `bv_*`** — construire la liste `BV_TOOLS = [...]` (12 entries, chacun avec `name`, `description`, `input_schema`). Module dédié `api/agent/manifest.py` proposé.
2. **Dispatch `bv_*`** — module `api/agent/dispatch_bv.py` avec `BV_DISPATCH` (cf. § 5.3). Appelé depuis `_dispatch_tool` des deux runtimes si `name.startswith("bv_")`.
3. **Émission `event` WS** — dans les deux runtimes, après chaque dispatch `bv_*` réussi, `ws.send_json(event.model_dump(by_alias=True))`.
4. **`SessionState` wiring** — nouveau helper `SessionState.from_device(device_slug) -> SessionState` qui cherche un fichier board dans `board_assets/{slug}.*` (le dossier existe, contient `.brd` + `.kicad_pcb` + `.pdf` pour `mnt-reform-motherboard`). Priorité d'extensions à trancher au plan d'implémentation — probablement `.kicad_pcb` d'abord (plus riche), fallback `.brd`. Parse via `api/board/parser/parser_for(path)` ; si aucun fichier board n'est trouvé, retourne `SessionState()` (board = None) sans lever — l'agent n'aura tout simplement pas les `bv_*` dans son manifest. Appelé en début de runtime (`run_diagnostic_session_{direct,managed}`).
5. **`mb_get_component` agrégé** — réécrire la fonction pour accepter `session: SessionState | None` et ajouter la section `board` si board chargé. Signature : `mb_get_component(*, device_slug, refdes, memory_root, session: SessionState | None = None)`. Le dispatch dans les runtimes passe la session.
6. **Manifest dynamique** — `build_tools_manifest(session: SessionState) -> list[dict]`. Toujours inclut `MB_TOOLS`. Ajoute `BV_TOOLS` si `session.board is not None`. Le `SYSTEM_PROMPT_DIRECT` est remplacé par une fonction `render_system_prompt(session, device_slug)` qui liste les capabilities.

### Frontend (3 points)

7. **Listener WS** — dans `web/js/llm.js`, avant le `switch (payload.type)`, tester `if (payload.type?.startsWith("boardview.")) { window.Boardview?.apply(payload); return; }`. Le `return` évite de logger ces events dans le chat (le chat a déjà vu le `tool_use` correspondant).
8. **API publique `window.Boardview`** — dans `web/brd_viewer.js`, après le `initBoardview` export, ajouter :
   ```js
   window.Boardview = {
     apply(ev) { /* switch sur ev.type, route vers méthode correspondante */ },
     highlight({refdes, color, additive}) { /* mute state.agent.highlights, requestRedraw */ },
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
   ```
9. **Split user/agent state** — dans `brd_viewer.js`, renommer le state global actuel :
   - `state.selectedPart` → `state.user.selectedPart`
   - `state.selectedPinIdx` → `state.user.selectedPinIdx`
   - Ajouter `state.agent = {highlights: new Set(), focused: null, dimmed: false, annotations: new Map(), arrows: new Map(), net: null, filter: null}`
   - `draw()` superpose : stroke violet (`--violet`) pour `state.user.selectedPart`, stroke cyan (`--cyan`) pour `state.agent.highlights`. Si un refdes est dans les deux, la couleur user gagne (le tech reste maître de sa sélection).

### Doc (1 point)

10. **`CLAUDE.md` Hard Rule #5** — remplacer la formulation par celle du § 2 de ce spec.

---

## 8. Error handling

| Situation | Traitement |
|---|---|
| `session.board is None` au moment d'un `bv_*` | **Ne peut pas arriver** : si board absent, le manifest n'expose pas la famille `bv_*`. L'agent ne voit pas le tool, ne l'appelle pas. Defense-in-depth : le handler renvoie `{ok: false, reason: "no-board-loaded"}` si appelé malgré tout (code existant, ligne `boardview.py:14`). |
| Refdes inconnu (`bv_highlight("U99")`) | Handler renvoie `{ok: false, reason: "unknown-refdes", suggestions: [...]}` (Levenshtein via `suggest_similar`). `tool_result` contient ces champs, l'agent choisit : asks user, ou retente avec un `suggestion`. **Aucun event WS émis** (pas de mutation visuelle pour un échec). |
| Net inconnu (`bv_highlight_net("VGA_NOT_EXIST")`) | Idem, `reason: "unknown-net"` (handler existant retourne `suggestions: []`, TODO potentielle pour symétrie mais non bloquant). |
| Handler lève une exception | Runtime catche au niveau dispatch, envoie un `tool_result` avec `{ok: false, reason: "handler-exception", error: str(exc)}`. Log côté backend. Pas d'event WS. |
| WS déconnecté pendant un tour | Comportement existant : `WebSocketDisconnect` capturé, session MA archivée. Les events `boardview.*` en cours sont simplement jetés — pas de retry (le tech reconnecte = nouvelle session). |
| Frontend reçoit un `boardview.*` avec refdes absent du board côté front | Scenario : le board côté backend et frontend ont divergé (rare, mais possible si hot-reload). `window.Boardview.apply` log un warning console et ignore l'event. Pas de crash. |
| `window.Boardview` pas encore défini quand un event arrive | `llm.js` teste `window.Boardview?.apply(payload)`. Si le renderer n'est pas initialisé (tech n'a jamais ouvert la section `#pcb`), l'event est silencieusement perdu. **Décision** : acceptable — le tech doit être sur la section PCB pour que le pilotage ait du sens. Alternative (non retenue) : buffer les events jusqu'à `initBoardview`. |

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
  - `bv_focus` avec `auto_flipped` : board chargé sur face top, refdes sur bottom → `event.auto_flipped == True`
  - Dispatch d'un nom de tool inconnu → `{ok: false, reason: "unknown-tool"}`
- `tests/agent/test_mb_aggregation.py`
  - `mb_get_component(session=None)` → seulement clé `memory_bank`
  - `mb_get_component(session=with_board)` → clés `memory_bank` + `board` si refdes dans le board
  - `mb_get_component(session=with_board)` avec refdes dans memory bank mais pas dans board → seulement `memory_bank`
  - Jamais de clé `schematic` (stub absent)
- `tests/agent/test_system_prompt.py`
  - `render_system_prompt(session_no_board)` mentionne « memory bank ✅, boardview ❌ »
  - `render_system_prompt(session_with_board)` mentionne « memory bank ✅, boardview ✅ »

### Intégration (pytest-asyncio)

- `tests/agent/test_ws_flow.py` (nouveau)
  - Mock AsyncAnthropic qui force un tool_use `bv_highlight(refdes="U1")` sur un board fixture
  - Vérifier ordre : `{type: "tool_use"}` → `{type: "boardview.highlight", refdes: ["U1"], …}` → prochaine itération
  - Mock qui force `bv_highlight(refdes="U999")` (inconnu) → vérifier qu'**aucun event `boardview.*`** n'est émis, mais `tool_result` contient `reason: "unknown-refdes"`

### Manuel (browser)

**Obligatoire avant commit (cf. memory `feedback_visual_changes_require_user_verify`) :**
- Ouvrir `/#pcb`, lancer panneau agent avec `⌘+J`, device `mnt-reform-motherboard`
- Taper « highlight U1 » → vérifier : chat log montre `→ bv_highlight U1`, canvas PCB surligne U1 en cyan
- Taper « focus C29 » → canvas pan/zoom sur C29
- Cliquer manuellement sur un autre composant (ex. R5) → vérifier stroke violet sur R5 **coexiste** avec stroke cyan sur U1/C29
- Taper « reset view » → agent state clear, user state (R5 sélectionné) **préservé**

### Tests non requis

- Test du renderer (`brd_viewer.js`) — pas de framework de test JS dans le repo, validation visuelle via browser suffit
- Test du parseur `.kicad_pcb` — déjà couvert par les tests existants

---

## 10. Impact `CLAUDE.md`

Diff minimal attendu :

```diff
-5. **No hallucinated component IDs.** Every refdes (e.g. `U7`, `C29`) the
-   agent mentions must be validated against parsed board data *before* being
-   shown to the user. Tools that cannot answer return structured
-   null/unknown — never fake data.
+5. **No hallucinated component IDs.** Every refdes the agent surfaces must
+   originate from a tool lookup (`mb_get_component` for memory bank + board
+   aggregation, or a `bv_*` tool that cross-checks the parsed board). These
+   tools never fabricate — they return `{found: false, closest_matches: [...]}`
+   for unknown refdes, and the system prompt instructs the agent to pick from
+   `closest_matches` or ask the user, never invent. Verification is enforced
+   at the tool boundary, not by a post-hoc gate.
```

Aucune autre ligne de `CLAUDE.md` ne bouge. La règle reste listée dans "Hard rules — NEVER violate" (l'esprit est intact, seule la mécanique change).

---

## 11. Non-objectifs explicites

Ce spec **ne couvre pas** :

- **Streaming token-par-token de la réponse agent** — le CLAUDE.md dit « Streaming over polling » mais le runtime actuel envoie la réponse complète par bloc. C'est un chantier séparé.
- **Un tool `bv_upload_board`** qui permettrait à l'agent de charger un board à la volée — hors scope, c'est une action utilisateur (drag-drop).
- **Un tool `bv_query_connectivity`** pour que l'agent interroge « quels composants sont sur le net VDD ? » sans avoir à tout résoudre via `mb_get_component` chaîné. Candidat pertinent pour v2 si l'usage montre que l'agent fait beaucoup d'allers-retours pour cartographier la topologie. À la place, on peut enrichir `mb_get_component.board.nets` avec aussi les pin_refs des nets pour donner le contexte d'un coup.
- **Un tool `bv_export_annotations`** pour figer les annotations posées par l'agent dans un rapport — future étape « journal de diagnostic ».

---

## 12. Décisions techniques validées lors du brainstorming

| # | Décision | Justification |
|---|---|---|
| 1 | Familles `mb_*` / `bv_*` séparées, pas fusionnées | `mb_*` = lecture toujours dispo ; `bv_*` = mutation UI dépendante du board. Séparation source/action. |
| 2 | Manifest dynamique (exposition conditionnelle par famille) | L'agent ne tente pas un tool qui échouerait toujours ; system prompt liste les capabilities au début du tour. |
| 3 | `mb_get_component` agrégateur multi-source | Un seul appel retourne tout ce qui existe (memory bank + board si dispo + schematic si dispo plus tard). Pas d'allers-retours de fetch atomique. |
| 4 | Retrait du « gate post-hoc » d'anti-hallucination | Inutile : les tools sont la seule source de refdes, retour `{found: false}` par construction. Règle #5 réécrite (cf. § 2). |
| 5 | Noms publics `bv_*` distincts des noms de handlers | Clarté du namespace agent (`bv_highlight`) indépendante de l'organisation interne (`highlight_component`). Mapping dans `BV_DISPATCH`. |
| 6 | Retour scindé : `tool_result` texte vs `event` WS visuel | Économise des tokens côté agent (pas besoin du payload binaire), respecte la séparation « l'agent sait que ça a marché / le frontend sait quoi muter ». |
| 7 | Split `state.user` vs `state.agent` dans le renderer | Le tech garde sa sélection ; l'agent ne l'écrase pas. Rendu superpose (violet user, cyan agent). |
| 8 | Pas de buffering des events si `window.Boardview` pas prêt | Acceptable : le pilotage n'a de sens que si le tech est sur `#pcb`. Si pas prêt, events silencieusement perdus. |
| 9 | Hook `sch_*` présent dans `build_tools_manifest` mais non livré | Le jour où `api/vision/` parse les PDF, on branche sans impact sur l'agent. |
