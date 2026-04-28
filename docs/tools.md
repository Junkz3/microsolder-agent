# Tools manifest (auto-généré, ne pas éditer à la main)

Source de vérité : `api/agent/manifest.py`. Ce fichier est régénéré par `make tools-inventory` (ou directement `.venv/bin/python scripts/dump_tools_inventory.py`).

Pas de timestamp embarqué : la sortie est déterministe pour rester diff-friendly entre deux régénérations à manifest constant. Si vous touchez un outil dans le manifest, régénérez ce fichier dans le même commit.

## Sommaire

| Famille | Outils | Quand exposé |
|---|---|---|
| Memory bank (MB) | 14 | always |
| Boardview (BV) | 13 | session has a board |
| Technician profile | 3 | always |
| Diagnostic protocol | 4 | always |
| Camera | 1 | session reports a camera |
| Consult specialist | 1 | Managed-Agents runtime only |

## Memory bank (MB) — 14 tool(s)

Always-on. Memory-bank lookups, board aggregation, the schematic deterministic engines (`mb_schematic_graph`, `mb_hypothesize`), the per-repair measurement journal and the canonical archival API (`mb_record_finding`, `mb_record_session_log`, `mb_validate_finding`, `mb_expand_knowledge`).

### `mb_get_component`

Look up a component by refdes on the current device.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string | yes | e.g. U7, C29, J3100 |

### `mb_get_rules_for_symptoms`

Find diagnostic rules matching a list of symptoms, ranked by symptom overlap + rule confidence.

| Param | Type | Required | Description |
|---|---|---|---|
| `symptoms` | array<string> | yes |  |
| `max_results` | integer | no |  |

### `mb_record_finding`

Persist a confirmed repair finding so future sessions see it.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string | yes |  |
| `symptom` | string | yes |  |
| `confirmed_cause` | string | yes |  |
| `mechanism` | string | no |  |
| `notes` | string | no |  |

### `mb_record_session_log`

Write a narrative summary of THIS conversation to the device's cross-repair log so future sessions on the same device can grep what was tested / hypothesised / concluded.

| Param | Type | Required | Description |
|---|---|---|---|
| `symptom` | string | yes | 1-line restatement of the user-reported symptom that drove this session. |
| `outcome` | enum(`resolved`, `unresolved`, `paused`, `escalated`) | yes | resolved = fix confirmed; unresolved = ended without conclusion; paused = user will resume; escalated = beyond bench scope (board-replace, vendor RMA). |
| `tested` | array<object> | no | Probes/inspections done. Empty list OK. |
| `hypotheses` | array<object> | no | Suspect refdes considered during the session, with verdict. |
| `findings` | array<string> | no | report_id values returned by mb_record_finding during this session — link them so the narrative cross-references the canonical findings. |
| `next_steps` | string | no | If outcome=unresolved or paused: what the next session should pick up. |
| `lesson` | string | no | One-line takeaway for future repairs on this device. Most useful field for grep-based recall. |

### `mb_schematic_graph`

Interrogate the compiled electrical graph (rails, ICs, enable signals, boot sequence).

| Param | Type | Required | Description |
|---|---|---|---|
| `query` | enum(`rail`, `component`, `downstream`, `boot_phase`, `list_rails`, `list_boot`, `critical_path`, `net`, `net_domain`) | yes |  |
| `label` | string | no | Rail or net label, e.g. '+5V', '+3V3', '24V_IN', 'HDMI_HPD'. Required for query=rail or query=net. |
| `refdes` | string | no | Component refdes, e.g. 'U7'. Required for query=component or query=downstream. |
| `domain` | string | no | Functional domain for query=net_domain. Canonical values: hdmi, usb, pcie, ethernet, audio, display, storage, debug, power_seq, power_rail, clock, reset, control, ground. Free-f... |
| `index` | integer | no | 1-based phase index. Required for query=boot_phase. |

### `mb_hypothesize`

Propose hypotheses (refdes, mode) that explain the observations.

| Param | Type | Required | Description |
|---|---|---|---|
| `state_comps` | object | no | Map refdes → mode. For an IC: 'dead', 'alive', 'anomalous', 'hot'. For a passive (R/C/D/FB): 'open', 'short', 'alive'. For a passive_q (MOSFET/BJT): 'open', 'short', 'stuck_on',... |
| `state_rails` | object | no | Map rail label → mode. Modes: 'dead' (0V), 'alive' (nominal), 'shorted' (short to GND or overvolt), 'stuck_on' (powered when it should be off — blown load switch downstream). |
| `metrics_comps` | object | no | Optional numeric measurements on components, refdes → {measured, unit, nominal?}. |
| `metrics_rails` | object | no | Optional numeric measurements on rails. |
| `max_results` | integer | no |  |
| `repair_id` | string | no | If set AND state/metrics dicts are empty, synthesise observations from the repair's measurement journal. |

### `mb_record_measurement`

Record an electrical measurement from the tech into the repair-session journal.

| Param | Type | Required | Description |
|---|---|---|---|
| `target` | string | yes |  |
| `value` | number | yes |  |
| `unit` | enum(`V`, `A`, `W`, `°C`, `Ω`, `mV`) | yes |  |
| `nominal` | number \| null | no |  |
| `note` | string \| null | no |  |

### `mb_list_measurements`

Re-read the repair-session measurement journal, optionally filtered by target and/or timestamp.

| Param | Type | Required | Description |
|---|---|---|---|
| `target` | string \| null | no |  |
| `since` | string \| null | no |  |

### `mb_compare_measurements`

Before/after diff of a given target (oldest measurement vs latest by default).

| Param | Type | Required | Description |
|---|---|---|---|
| `target` | string | yes |  |
| `before_ts` | string \| null | no |  |
| `after_ts` | string \| null | no |  |

### `mb_observations_from_measurements`

Synthesise an Observations payload (state + metrics) from the measurement journal — latest event per target.

_no parameters_

### `mb_set_observation`

Force an observation mode for a target without recording a value (useful when the tech says 'U7 is dead' without a measurement).

| Param | Type | Required | Description |
|---|---|---|---|
| `target` | string | yes |  |
| `mode` | enum(`dead`, `alive`, `anomalous`, `hot`, `shorted`, `stuck_on`, `stuck_off`, `open`, `short`) | yes |  |

### `mb_clear_observations`

Clear the visual observation state on the UI side (the journal is preserved).

_no parameters_

### `mb_validate_finding`

Record the culprit component(s) confirmed by the tech at the end of a repair.

| Param | Type | Required | Description |
|---|---|---|---|
| `fixes` | array<object> | yes | List of components fixed during the repair. |
| `tech_note` | string \| null | no |  |
| `agent_confidence` | enum(`high`, `medium`, `low`) | no |  |

### `mb_expand_knowledge`

Grow this device's memory bank around a focus symptom area.

| Param | Type | Required | Description |
|---|---|---|---|
| `focus_symptoms` | array<string> | yes | Symptom phrases to target, e.g. ['no sound', 'earpiece dead']. |
| `focus_refdes` | array<string> | no | Optional refdes to probe specifically (e.g. ['U3101', 'U3200']). |

## Boardview (BV) — 13 tool(s)

Boardview rendering controls. Stripped from the manifest when the session has no board loaded (see `build_tools_manifest`).

### `bv_scene`

Compose a diagnostic scene on the board in ONE call: reset, highlights, annotations, arrows, focus, dim.

| Param | Type | Required | Description |
|---|---|---|---|
| `reset` | boolean | no | Clear all overlays before applying the scene. |
| `highlights` | array<object> | no |  |
| `annotations` | array<object> | no |  |
| `arrows` | array<object> | no | Directional arrows refdes→refdes. Include them EVERY TIME the scene describes a directed relation: boot order, signal path, power propagation, fault cascade, upstream→downstream... |
| `focus` | object | no |  |
| `dim_unrelated` | boolean | no |  |

### `bv_highlight`

Highlight one or more components on the PCB canvas.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string \| array<string> | yes |  |
| `color` | enum(`accent`, `warn`, `mute`) | no |  |
| `additive` | boolean | no |  |

### `bv_focus`

Pan/zoom the PCB canvas to a specific component.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string | yes |  |
| `zoom` | number | no |  |

### `bv_reset_view`

Reset the PCB canvas: clear all highlights, annotations, arrows, dim, filter.

_no parameters_

### `bv_flip`

Flip the visible PCB side (top ↔ bottom).

| Param | Type | Required | Description |
|---|---|---|---|
| `preserve_cursor` | boolean | no |  |

### `bv_annotate`

Attach a text label to a component on the canvas.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string | yes |  |
| `label` | string | yes |  |

### `bv_dim_unrelated`

Visually dim all components not currently highlighted — focuses the technician's attention.

_no parameters_

### `bv_highlight_net`

Highlight every pin on a given net (rail/signal tracing).

| Param | Type | Required | Description |
|---|---|---|---|
| `net` | string | yes |  |

### `bv_show_pin`

Point to a specific pin of a component (e.g.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes` | string | yes |  |
| `pin` | integer | yes |  |

### `bv_draw_arrow`

Draw a directional arrow on the PCB from one refdes to another.

| Param | Type | Required | Description |
|---|---|---|---|
| `from_refdes` | string | yes |  |
| `to_refdes` | string | yes |  |

### `bv_measure`

Return the physical distance (mm) between two components' centers.

| Param | Type | Required | Description |
|---|---|---|---|
| `refdes_a` | string | yes |  |
| `refdes_b` | string | yes |  |

### `bv_filter_by_type`

Show only components whose refdes starts with a given prefix.

| Param | Type | Required | Description |
|---|---|---|---|
| `prefix` | string | yes |  |

### `bv_layer_visibility`

Toggle visibility of a PCB layer (top or bottom).

| Param | Type | Required | Description |
|---|---|---|---|
| `layer` | enum(`top`, `bottom`) | yes |  |
| `visible` | boolean | yes |  |

## Technician profile — 3 tool(s)

Always-on. Read/check/track the technician's skills + tool inventory.

### `profile_get`

Read the technician's profile: identity, current level, verbosity preference, list of available and missing tools, and summary of mastered/practiced/learning skills with usage counts.

_no parameters_

### `profile_check_skills`

Given a list of candidate skill ids from the catalogue (e.g.

| Param | Type | Required | Description |
|---|---|---|---|
| `candidate_skills` | array<string> | yes |  |

### `profile_track_skill`

Record that the technician has executed an action requiring this skill, with evidence.

| Param | Type | Required | Description |
|---|---|---|---|
| `skill_id` | string | yes |  |
| `evidence` | object | yes |  |

## Diagnostic protocol — 4 tool(s)

Always-on. Emit and steer a typed, stepwise diagnostic protocol rendered as floating cards on the board + side wizard.

### `bv_propose_protocol`

Emit an ordered, typed diagnostic protocol that the UI renders visually (floating cards on the board + side wizard, or inline cards when no board).

| Param | Type | Required | Description |
|---|---|---|---|
| `title` | string | yes |  |
| `rationale` | string | yes |  |
| `rule_inspirations` | array<string> | no |  |
| `steps` | array<object> | yes |  |

### `bv_update_protocol`

Modify the active protocol: insert (new step after an anchor), skip (the tech lacks the tool or you decide to pass), replace_step (a pending step that no longer makes sense), reorder (the pending steps — the active st...

| Param | Type | Required | Description |
|---|---|---|---|
| `action` | enum(`insert`, `skip`, `replace_step`, `reorder`, `complete_protocol`, `abandon_protocol`) | yes |  |
| `reason` | string | yes |  |
| `step_id` | string \| null | no |  |
| `after` | string \| null | no |  |
| `new_step` | object \| null | no |  |
| `new_order` | array \| null | no |  |
| `verdict` | string \| null | no |  |

### `bv_record_step_result`

Persist a step result yourself (useful when the tech reports the value in chat rather than via the UI: 'VBUS = 4.8V').

| Param | Type | Required | Description |
|---|---|---|---|
| `step_id` | string | yes |  |
| `value` | any | no |  |
| `unit` | string \| null | no |  |
| `observation` | string \| null | no |  |
| `skip_reason` | string \| null | no |  |

### `bv_get_protocol`

Read the full active protocol (steps, statuses, results, history).

_no parameters_

## Camera — 1 tool(s)

Conditional. Exposed only when the frontend reported a camera available on session open.

### `cam_capture`

Acquire a still frame from the technician's selected camera (microscope, webcam, etc.).

| Param | Type | Required | Description |
|---|---|---|---|
| `reason` | string | no | Brief reason for the capture (logged, not shown to the tech). |

## Consult specialist — 1 tool(s)

Managed-Agents only. Cross-tier escalation; absent from the DIRECT-mode manifest because direct mode runs a single `messages.create` loop with no peer tiers to dispatch to.

### `consult_specialist`

Delegate a focused question to a specialist sub-agent on a different model tier.

| Param | Type | Required | Description |
|---|---|---|---|
| `tier` | enum(`fast`, `normal`, `deep`) | yes | Specialist tier. `fast`=Haiku 4.5 (cheap quick lookups), `normal`=Sonnet 4.6 (balanced), `deep`=Opus 4.7 (best multi-step reasoning). Don't pick your own tier — the dispatcher w... |
| `query` | string | yes | The focused question for the specialist. |
| `context` | string | no | Self-contained briefing: device, symptoms, prior measurements, hypotheses already ruled out. The sub-agent has no access to your tools or memory. |
