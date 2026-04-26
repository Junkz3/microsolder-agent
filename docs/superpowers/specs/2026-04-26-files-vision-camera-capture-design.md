<!-- SPDX-License-Identifier: Apache-2.0 -->
# Files + Vision : Macro Upload + Camera Capture

**Date** : 2026-04-26
**Status** : Draft (pending Alexis review)
**Owner** : diagnostic runtime (`api/agent/`) + frontend chat panel (`web/js/llm.js`)

---

## Context

Aujourd'hui, l'agent diagnostic n'a aucun contexte visuel hors de l'overlay
boardview parsé. Pour un device exotique sans boardview ingérée, le tech
peut décrire à la voix mais l'agent reste aveugle.

Deux scénarios complémentaires motivent cette feature :

1. **Tech-initiated** — le tech voit un détail intéressant (à l'œil nu, via
   son propre soft, ou via une photo prise au téléphone) et veut le partager
   avec l'agent. Drag-drop ou bouton upload dans le panneau chat.

2. **Agent-initiated** — le tech a une caméra USB branchée (microscope
   typiquement, ou webcam) sélectionnée dans la metabar. L'agent décide
   qu'il a besoin de voir une zone précise pour avancer son diagnostic et
   appelle un tool dédié. Le tech a déjà cadré côté physique (zoom optique
   manuel) ; l'agent récupère juste un snapshot.

La vision native d'Opus 4.7 (haute-résolution 2576px long edge max,
~3× plus de détails que 4.6) rend l'analyse visuelle de boards exploitable
sans pipeline OCR / classification dédié.

## Goals

- Deux flows distincts mais convergeant vers la même session MA active :
  - **Flow A — Manual upload** : `user.message` avec block `image`
    référencant un `file_id` Files API.
  - **Flow B — Camera capture** : custom tool `cam_capture` →
    `user.custom_tool_result` avec block `image`.
- WS comme transport unique. Pas de nouveau registry de sessions actives.
- Persistance locale des bytes (`memory/{slug}/repairs/{repair_id}/macros/`)
  pour replay frontend sans re-upload Anthropic.
- Sélecteur de caméra permanent dans la metabar (cohérent avec le design
  language pro-tool).
- Nouveau préfixe de tool `cam_*`, distinct de `bv_*` (boardview-control)
  et `mb_*` (memory bank). Conditionnel sur `session.has_camera`.

## Non-goals

- Pas de pipeline visuel offline / batch (la vision arrive en stream
  pendant le tour de chat).
- Pas de OCR / classification automatique des marquages composants
  (Opus 4.7 lit les boîtiers en vision native, suffisant pour ce v1).
- Pas de live preview WebRTC streaming au backend (capture immédiate au
  snap, le tech a déjà cadré).
- Pas de modification du sanitizer refdes — la vision donne des
  boîtiers + positions, pas des refdes ; l'agent reste discipliné via
  prompt + sanitizer existant.
- Pas de feature flags ni de bascule legacy (couche neuve, on commit l'état
  final direct).

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                         Frontend (web/)                            │
│                                                                    │
│  metabar :                                                         │
│    📷 Caméra : [HD USB Camera ▾]  ← localStorage.cameraDeviceId    │
│                                                                    │
│  llm.js panel :                                                    │
│    [📁 Upload] btn + drag-drop zone                                │
│      → ws.send({type: "client.upload_macro", base64, mime, ...})   │
│                                                                    │
│    on "server.capture_request" event :                             │
│      getUserMedia({video: {deviceId}}) + canvas.toBlob('image/jpeg')│
│      → ws.send({type: "client.capture_response", request_id, ...}) │
│                                                                    │
│    capabilities frame at WS open :                                 │
│      → ws.send({type: "client.capabilities", camera_available, ...})│
└──────────────────────────┬─────────────────────────────────────────┘
                           │  WS /ws/diagnostic/{slug}
                           │
┌──────────────────────────▼─────────────────────────────────────────┐
│              Backend (api/agent/runtime_managed.py)                │
│                                                                    │
│  on capabilities frame :                                           │
│    session.has_camera = frame.camera_available                     │
│    → manifest_for(session) gates cam_capture exposure              │
│                                                                    │
│  Flow A — _handle_client_upload_macro :                            │
│    persist locally → client.beta.files.upload(purpose="agent")     │
│      → sessions.events.send(user.message + image block)            │
│                                                                    │
│  Flow B — agent.custom_tool_use[cam_capture] :                     │
│    push WS server.capture_request → await pending_captures[id].set │
│    persist locally → client.beta.files.upload                       │
│      → sessions.events.send(user.custom_tool_result + image block) │
└────────────────────────────────────────────────────────────────────┘
```

### Flow A — Manual upload (tech-initiated)

1. Tech drop un fichier ou clique le bouton upload (PNG/JPEG, cap 5 MB raw
   pre-encoding, enforced client-side).
2. Frontend lit le fichier en base64, envoie une frame WS :
   ```json
   {"type": "client.upload_macro",
    "base64": "...", "mime": "image/png", "filename": "macro_001.png"}
   ```
3. Backend `runtime_managed._handle_client_upload_macro` :
   - décode → persiste sous
     `memory/{slug}/repairs/{repair_id}/macros/{ts}_manual.{ext}`
     (ex: `1745704812_manual.png`)
   - `await client.beta.files.upload(file=(filename, bytes, mime), purpose="agent")` → récupère `file_id`
   - injecte dans la session MA :
     ```python
     await client.beta.sessions.events.send(
         session_id=session.id,
         events=[{
             "type": "user.message",
             "content": [
                 {"type": "image",
                  "source": {"type": "file", "file_id": file_id}},
                 {"type": "text",
                  "text": "Photo macro envoyée par le tech."}
             ]
         }]
     )
     ```
4. Agent stream sa réponse via `agent.message` events comme un tour normal.
5. Frontend optimistic-render la photo dans une bulle user (thumbnail 200px,
   click → fullscreen modal). Append au `messages.jsonl` avec `image_ref`.

### Flow B — Camera capture (agent-initiated)

1. Agent décide qu'il a besoin de voir un détail → appelle
   `cam_capture(reason?: string)`.
2. Backend reçoit `agent.custom_tool_use` event dans la boucle MA.
3. Backend push WS event au frontend :
   ```json
   {"type": "server.capture_request",
    "request_id": "cap_abc123", "tool_use_id": "sevt_xyz", "reason": "..."}
   ```
   Et stocke un `asyncio.Future` dans `session.pending_captures[request_id]`.
4. Frontend utilise le device sélectionné dans le picker
   (`localStorage.cameraDeviceId`) :
   ```js
   const stream = await navigator.mediaDevices.getUserMedia({
     video: {deviceId: {exact: deviceId}}
   });
   const track = stream.getVideoTracks()[0];
   const settings = track.getSettings();
   // Capture single frame via canvas.toBlob('image/jpeg', 0.92)
   ```
5. Frontend envoie une frame WS :
   ```json
   {"type": "client.capture_response",
    "request_id": "cap_abc123",
    "base64": "...", "mime": "image/jpeg", "device_label": "HD USB Camera"}
   ```
6. Backend résout le Future, persiste sous
   `memory/{slug}/repairs/{repair_id}/macros/{ts}_capture.{ext}`,
   upload Files API, et envoie le tool result :
   ```python
   await client.beta.sessions.events.send(
       session_id=session.id,
       events=[{
           "type": "user.custom_tool_result",
           "custom_tool_use_id": tool_use_id,
           "content": [
               {"type": "image",
                "source": {"type": "file", "file_id": file_id}},
               {"type": "text",
                "text": f"Capture acquise depuis {device_label}."}
           ]
       }]
   )
   ```
7. Agent reçoit l'image dans son tool result, continue le diagnostic.

#### Conditions d'exposition

`cam_capture` n'est pas dans le manifest si la session a déclaré
`camera_available: false` (ou n'a pas envoyé de capabilities frame).
Mêmes mécaniques que `bv_*` qui disparaissent quand `session.board is None`
(`api/agent/manifest.py::build_tools_manifest`).

#### Garde-fou anti-deadlock

Si le frontend ne répond jamais à un `server.capture_request` (tab fermé,
caméra débranchée, perm révoquée), un timeout (default 30s) résout le Future
en erreur et renvoie un `user.custom_tool_result` avec `is_error: true` :
```json
{"type": "user.custom_tool_result",
 "custom_tool_use_id": "...", "is_error": true,
 "content": [{"type": "text",
              "text": "Capture timeout: le frontend n'a pas répondu."}]}
```
Cohérent avec la stream watchdog landed dans `b4d2591`.

### Capabilities frame

Au moment de l'ouverture du WS, le frontend envoie immédiatement (avant le
premier `user.message`) :
```json
{"type": "client.capabilities",
 "camera_available": true,
 "available_devices": [{"deviceId": "abc...", "label": "HD USB Camera"}, ...]}
```

Backend met à jour `session.has_camera` (default `False`). Le manifest
re-build à chaque tour utilise cette flag pour gater `cam_capture`.

Note pratique : `enumerateDevices()` retourne des `label` vides tant que
l'utilisateur n'a pas accordé `getUserMedia` au moins une fois. Le picker
affiche "Caméra inconnue" pour les devices sans label, et le label est mis
à jour après la première capture (qui débloque la perm).

### Persistance + replay

Layout disque sous `memory/{slug}/repairs/{repair_id}/macros/` :
```
1745704812_manual.png       ← Flow A (tech-uploaded)
1745704933_capture.jpg      ← Flow B (agent-acquired)
1745705121_manual.png
```

Format dans `messages.jsonl` (chat history) :
```json
{
  "role": "user",
  "content": [
    {"type": "image_ref",
     "path": "macros/1745704812_manual.png",
     "source": "manual"},
    {"type": "text",
     "text": "Photo macro envoyée par le tech."}
  ],
  "ts": "2026-04-26T14:00:12Z"
}
```

`image_ref` est un type local non-Anthropic qui permet au frontend de
réafficher l'image au resume sans dépendre du `file_id` Anthropic (qui
peut expirer ou être inaccessible). Au resume :
- L'agent voit la session MA déjà compactée — pas besoin de réinjecter les
  vieilles images.
- Le frontend lit le `messages.jsonl` et résout `image_ref.path` via une
  nouvelle route `GET /api/macros/{slug}/{repair_id}/{filename}` qui sert
  le fichier (Content-Type from extension, served from
  `settings.memory_root / slug / "repairs" / repair_id / "macros" /`).

### Backend modules

- **`api/agent/runtime_managed.py`** :
  - Nouveaux handlers `_handle_client_upload_macro`,
    `_handle_client_capture_response`, `_handle_client_capabilities`.
  - Nouveau dispatch `agent.custom_tool_use[cam_capture]` qui push
    `server.capture_request` et await le Future.
  - `session.pending_captures: dict[str, asyncio.Future]` ajouté au state.

- **`api/agent/manifest.py`** : nouveau `cam_capture` tool, conditionnel
  sur `session.has_camera`. Définition :
  ```python
  CAM_CAPTURE_TOOL = {
      "type": "custom",
      "name": "cam_capture",
      "description": (
          "Acquire a still frame from the technician's selected camera "
          "(microscope, webcam, etc.). Use when you need a fresh visual "
          "on a specific component or anomaly. The tech has already "
          "framed and focused — no parameters needed beyond an optional "
          "reason for traceability."
      ),
      "input_schema": {
          "type": "object",
          "properties": {
              "reason": {"type": "string",
                         "description": "Brief reason (logged, not shown)."}
          },
          "additionalProperties": False
      }
  }
  ```

- **`api/session/state.py`** : nouveau champ `has_camera: bool = False`,
  populé via la capabilities frame. (Pas dans `from_device` — vient
  toujours du frontend après l'ouverture WS.)

- **`api/agent/macros.py`** (nouveau ~80 LOC) : helpers de persistance.
  - `persist_macro(memory_root, slug, repair_id, source, bytes, mime) -> Path`
  - `append_image_ref_to_messages_jsonl(memory_root, slug, repair_id, conv_id, image_ref, text)`

- **`api/main.py`** : nouvelle route
  `GET /api/macros/{slug}/{repair_id}/{filename}` (StreamingResponse, valide
  le path via `Path.resolve()` + `is_relative_to(macros_dir)` pour bloquer
  path traversal).

- **`scripts/bootstrap_managed_agent.py`** : `SYSTEM_PROMPT` étendu avec le
  bloc VISION (cf ci-dessous). Re-runnable pour pousser aux 3 agents
  tier-scoped (`fast` / `normal` / `deep`).

### Frontend modules

- **`web/index.html`** : nouvel élément dans la metabar
  ```html
  <div class="meta-camera">
    <svg class="icon" ...><!-- camera SVG inline 16×16 --></svg>
    <select id="camera-picker" class="meta-select">
      <option value="">-- aucune --</option>
    </select>
  </div>
  ```
  Style cohérent avec les autres `.meta-*` (small font, JetBrains Mono pour
  le label device).

- **`web/js/main.js`** ou **nouveau `web/js/camera.js`** (à choisir lors de
  l'impl, probablement `camera.js` pour isoler) : initialisation du picker
  au boot.
  ```js
  async function initCameraPicker() {
    try {
      // Trigger perm prompt to unlock device labels
      const probe = await navigator.mediaDevices.getUserMedia({video: true});
      probe.getTracks().forEach(t => t.stop());
    } catch (e) {
      // Perm denied — devices listed but with empty labels
    }
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cams = devices.filter(d => d.kind === 'videoinput');
    populateSelect(cams);
    restoreSelectionFromLocalStorage();
  }
  ```

- **`web/js/llm.js`** :
  - Bouton "📁" (icon SVG inline 16×16, `currentColor` stroke 1.6,
    cohérent avec les autres icons chrome) à côté de l'input texte du chat.
  - Drag-drop zone overlay sur le panneau LLM (highlight en hover via
    border-style + accent color).
  - Handler WS `server.capture_request` : `getUserMedia` + capture frame.
  - Render bulles user avec image (thumbnail 200px clickable pour
    fullscreen via modal existant ou nouveau modal léger).
  - Capabilities frame envoyée à l'open du WS.

### System prompt — bloc VISION

Ajouté dans `bootstrap_managed_agent.SYSTEM_PROMPT` :

```
**VISION** — Le tech a (parfois) une caméra branchée et sélectionnée dans
la metabar.

1. Si le tech upload une photo (block `image` dans son `user.message`) :
   identifie composants par boîtier (SOT-23, SO-8, QFN, BGA, MELF, etc.),
   signale anomalies visibles (décoloration, soudure cassée, condo gonflé,
   brûlure), propose mapping role probable → composant ("le BGA central
   c'est probablement le SoC ; le SO-8 près du connecteur USB-C, un load
   switch ou une protection"). Demande au tech ce qu'il a vu de son côté
   avant de proposer un plan.

2. Si tu as besoin de voir un détail spécifique et que `cam_capture` est
   exposé dans tes tools : appelle-le. Le tech a déjà cadré côté physique
   (zoom optique manuel). Pas de paramètres requis — `reason` est juste
   pour les logs.

3. Pas de capture spéculative : appelle `cam_capture` quand ça apporte une
   info diagnostique précise, pas par réflexe ou pour "voir si c'est
   intéressant".

4. Discipline anti-hallucination maintenue : la vision te donne des
   boîtiers et positions, jamais des refdes. Si tu mentionnes un refdes, il
   doit venir d'un `mb_get_component` ou `bv_*` lookup, pas d'une lecture
   visuelle.
```

### Re-bootstrap MA

`scripts/bootstrap_managed_agent.py` est idempotent. Re-run après le merge
pour pousser le nouveau `SYSTEM_PROMPT` et le manifest avec `cam_capture`
aux 3 agents tier-scoped.

## Hardening précédant Files+Vision

Avant d'attaquer Files+Vision, 3 actions de durcissement sur la couche
scribe (MA layered architecture landed lors de la session précédente) à
faire pour valider le pattern et fermer une dette technique :

1. **Smoke E2E multi-session** (~30 min) — étendre
   `scripts/smoke_layered_memory.py` :
   - Session 1 : agent écrit explicitement `state.md` + `decisions/{slug}.md`
     au mount repair (kickoff explicite : "écris ton état avant de partir").
   - Session 2 sur le même `repair_id` : vérifier que l'agent grep le mount
     + cite explicitement quelque chose de la session précédente
     (asserter sur du contenu reconnu, ex un nom de composant ou une
     décision spécifique).
   - C'est le seul vrai validateur du pattern scribe.

2. **Test anti-régression `conv_id`** (~30 min) —
   `tests/agent/test_runtime_conv_id_dispatch.py` : test ciblé qui simule
   un dispatch `bv_*` dans `_forward_session_to_ws` et asserte que
   `save_board_state` est appelé avec le bon `conv_id` (verrou contre le
   NameError fix par `6bd6628`).

3. **Audit `resolved_conv_id` scope** (~10 min) — `grep -n
   "resolved_conv_id" api/`, vérifier scope par scope qu'il n'y a pas
   d'autre closure nested qui réfère à la variable hors de son scope. Si
   un site suspect, écrire un test ciblé.

Ces 3 actions précèdent Files+Vision dans l'ordre d'implémentation.

## Tests

### Unit (no API, fast)

- `tests/agent/test_runtime_macro_upload.py` : mock WS handler reçoit
  `client.upload_macro`, asserts persistence path correct + Files API call
  (mocked) avec bons args + `sessions.events.send` avec le bon payload.
- `tests/agent/test_runtime_camera_capture.py` : mock dispatch
  `agent.custom_tool_use[cam_capture]`, asserts WS push
  `server.capture_request`, simule `client.capture_response` async, asserts
  `user.custom_tool_result` payload + persistence.
- `tests/agent/test_runtime_camera_timeout.py` : ne pas envoyer de
  `client.capture_response`, asserter qu'après 30s on envoie un
  `user.custom_tool_result` avec `is_error: true`.
- `tests/agent/test_manifest_cam_conditional.py` : asserter que
  `cam_capture` est dans le manifest seulement quand
  `session.has_camera == True`.
- `tests/agent/test_capabilities_frame.py` : asserter que
  `client.capabilities` met à jour `session.has_camera`.
- `tests/agent/test_macros_persistence.py` : asserter que
  `persist_macro` écrit au bon path avec bonne extension dérivée du mime.
- `tests/api/test_macros_route.py` : asserter que la route
  `GET /api/macros/{slug}/{repair_id}/{filename}` sert le fichier et bloque
  les path traversal (`../../etc/passwd`).

### Smoke E2E live (slow, manuel)

- `scripts/smoke_files_vision.py` : démarre une session WS contre iphone-x
  (déjà seedée), envoie une `client.capabilities {camera_available: true}`
  factice + une `client.upload_macro` avec une image PCB de test
  (`tests/fixtures/macro_iphonex_test.png` à committer ; image clean-room
  d'une board de dev, pas du contenu propriétaire). Assert que l'agent
  stream une analyse contenant des mots-clés visuels (boîtier, BGA, SoC,
  composant, etc.).
- Test live in browser : `make run`, ouvrir le frontend, sélectionner
  webcam intégrée du laptop dans le picker, lancer un diag avec kickoff
  "regarde ma board avec ta caméra", vérifier que l'agent appelle
  `cam_capture` au moins une fois et analyse la frame retournée.

## Hard rules — respect

- **#2 Apache 2.0** : tous nouveaux fichiers ont le SPDX header.
- **#3 Permissive deps only** : pas de nouvelle dépendance — Files API
  via `anthropic` SDK déjà pinned, `getUserMedia` natif, `<canvas>` natif.
- **#4 Open hardware only** : la fixture image
  `tests/fixtures/macro_iphonex_test.png` doit être une **photo
  clean-room** d'une board de dev (Arduino, Raspberry Pi, custom dev
  board), pas une vraie photo iPhone. À sourcer/produire au moment du
  test.
- **#5 No hallucinated refdes** : le bloc VISION du SYSTEM_PROMPT rappelle
  explicitement la discipline. Le sanitizer existant
  (`api/agent/sanitize.py`) continue de wrapper les tokens refdes-shaped
  non résolus dans le texte agent.
- **Frontend rules** :
  - SVG icons inline 16×16, `currentColor` stroke 1.6.
  - Tokens OKLCH (pas de hex hardcodé pour les colors), bouton upload =
    accent neutre `--text-2`.
  - Vanilla JS, pas de framework.
  - UI strings en français.
  - Glass overlay pour le drag-drop zone (cohérent avec inspector / legend
    / tweaks / tooltip).
- **Commit hygiene** : split en commits cohésifs, paths explicites
  systématiques. Probable :
  1. Hardening : smoke multi-session + test conv_id + audit grep.
  2. Backend Files+Vision : `runtime_managed.py` + `manifest.py` +
     `state.py` + nouveau `macros.py` + route `/api/macros/...`.
  3. Frontend Files+Vision : metabar picker + `camera.js` + `llm.js`
     upload + drag-drop + capture handler + replay rendering.
  4. SYSTEM_PROMPT + bootstrap re-run.

## Open questions

- **Format de capture** : PNG (lossless, plus gros) ou JPEG (plus léger,
  ~85% qualité) ? **Default JPEG** car les boards photo ont déjà du bruit
  capteur, et le gain en taille (5-10× sur du 2576px) est significatif.
  Le frontend décide via `canvas.toBlob('image/jpeg', 0.92)`. PNG
  uniquement si le tech upload manuellement un PNG (Flow A passe-through
  le mime original).
- **Limite stockage `macros/`** : pas de cap pour l'instant (`memory/` est
  déjà gitignored, et un repair fait probablement < 50 photos). Si le
  disque devient un souci, ajouter une rotation TTL ou un cap par repair
  dans une future itération.
- **HTTPS et `getUserMedia`** : `getUserMedia` exige HTTPS sauf sur
  `localhost`. Pour le dev (port 8000 sur localhost), OK. Si le serveur
  est exposé sur un LAN ou un domain plus tard, prévoir reverse proxy
  HTTPS ou flag `--insecure-allow-camera` côté browser pour test.

## Future work (non-scope cette itération)

- **Live preview WebRTC** avant capture pour que le tech vérifie le
  cadrage de l'agent (besoin streaming WebRTC, plus lourd).
- **OCR sur les marquages composants** (ex: "U2 = APL3514", lecture de
  caps printing). Opus 4.7 le fait en vision native déjà ; un dedicated
  pass via `vision/` module pourrait booster sur les pages sombres.
- **Auto-classification visuelle des anomalies** (heatmap, segmentation
  via vision-only model dédié).
- **Multi-frame capture** (`cam_capture_burst`) pour comparer 2 angles ou
  un avant/après. Probable extension du tool dans une itération future.
