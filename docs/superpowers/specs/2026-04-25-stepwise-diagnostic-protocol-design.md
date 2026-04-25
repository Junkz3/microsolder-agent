# Stepwise Diagnostic Protocol — Design

**Status** · Draft 2026-04-25 · author: Alexis + agent  
**Supersedes / depends on** · `2026-04-23-agent-boardview-control-design.md` (bv_* tool family), `2026-04-23-fault-modes-and-measurement-memory-design.md` (measurement persistence)

## 1. Problem

Today the agent presents diagnostics as plain prose in the chat panel. The tech reads, picks up a probe, comes back, types the result, the agent replies again. Three frictions:

1. **No structured plan.** The tech doesn't see the diagnostic *trajectory* — what's coming next, why, when it ends. They can't pre-fetch tools or anticipate.
2. **No visual anchor on the board.** The agent says "probe VIN at R49"; the tech still has to find R49 on the boardview manually.
3. **Result handoff is verbose.** Typing "VIN at R49 = 24.5V, adapter plugged in, batteries removed" each turn is friction the tech absorbs in muscle memory but loses precision under fatigue.

The agent already has the *knowledge* to drive a precise diagnostic (rules, knowledge graph, simulator, hypothesize, measurement memory). What's missing is a **structured surface** to render that diagnostic stepwise, anchored to the board, with typed result capture.

## 2. Goals

- The agent can emit a **typed, ordered diagnostic protocol** as a first-class artifact (not as prose embedded in a chat message).
- Each step is **anchored to the board** when a board is loaded — the tech sees a numbered badge ① ② ③ on the relevant component, the current step pulses, the rest is discreet.
- The tech submits results via **typed inputs** (numeric / boolean / observation / ack), with a universal "skip — j'ai pas l'outil" escape.
- The agent observes results and **adapts the plan live** — insert / skip / reorder / conclude — without restarting from scratch.
- Without a board (no `partByRefdes`), the same protocol renders **inline in the chat** as step cards. Same data, different surface.
- All measurements flow into the existing `mb_record_measurement` / `mb_set_observation` plumbing → free benefit: simulator auto-observation, audit log, before/after compare.

## 3. Non-goals

- **Photo / vision input.** Defer. Step type vocabulary leaves room for a `photo` type later.
- **Auto-rendering rules.json `diagnostic_steps` directly.** The current schema (`{action, expected}` free text) is incompatible with typed inputs. The agent reads it as inspiration but emits the typed plan itself. Schema-extension is a V2 path.
- **Protocol library / cross-repair reuse.** Each protocol lives within its repair. Aggregation across repairs is a future concern.
- **Multi-protocol-per-repair.** One active protocol at a time. Replacing one archives the prior.
- **Tech-driven protocol creation.** The tech doesn't compose steps in the UI; the agent owns plan emission. Tech may comment, skip, or ask the agent to revise via chat.

## 4. User flows

### 4.1 Happy path with boardview

1. Tech opens repair `c25f8bc32cd9` on `mnt-reform-motherboard`. Board loads. Agent receives the device + symptom (via the existing ctx tag).
2. Agent runs its usual rule lookup, finds `rule-vin-dead-001`. After a short conversational opening it calls `bv_propose_protocol(steps=[…])` with 5 typed steps.
3. Frontend receives the `protocol_proposed` WS event:
   - **Wizard panel** (right side, replacing or coexisting with the chat panel — see §6.3) lists the 5 steps with status badges and instructions.
   - **Floating instruction card** appears above the current step's component (`R49`), with the input field for `numeric V`, nominal `24.0`, pass range `[9, 32]`.
   - **Numbered badges** ① ② ③ ④ ⑤ appear on each component in the plan; current pulses cyan.
4. Tech probes, types `24.5`, hits Enter. Frontend POSTS the result over the existing diagnostic WS as a `protocol_step_result` event. Backend:
   - Calls `mb_record_measurement(target=R49, value=24.5, unit=V, nominal=24.0, source="protocol")`.
   - Updates `protocol.json` to mark s_1 `done`, advances `current_step_id` to s_2, appends to `history`.
   - Synthesizes a `user.message` for the MA agent: `[step_result] step=s_1 target=R49 value=24.5V outcome=pass · plan: 5 steps, current=s_2`.
   - Echoes a `protocol_updated` WS event back to the frontend so the floating card hops to the next component.
5. Agent reads the synthetic message, sees `outcome=pass`, doesn't need to re-plan. Either silent ACK (no message back) or one-line acknowledgment in chat ("VIN nominal, on enchaîne sur F1.").
6. Tech proceeds. At step 3 (`Diode-mode VIN to GND`) result is `0.02V` — out of pass range. Frontend submits, backend marks `failed`, agent reads the synthetic message, decides "F1 looks intact but VIN bus is shorted", inserts step `s_3b` ("Reflow then re-measure", target `C42`) via `bv_update_protocol(action="insert", after="s_3", new_step={…})`. Frontend updates wizard + badges.
7. Eventually the protocol resolves (final step is type `ack` — "remplace F1 par 1A SMD, hot air 350°C"). On submit, agent calls `mb_validate_finding` as today.

### 4.2 Without boardview

Same agent contract. Frontend renders the protocol **inline in chat** — each step is a step card embedded in the agent's message stream, with the same input fields. No badges, no floating card, no board-side anchor. The wizard panel is hidden.

Coexistence: when the boardview becomes available mid-session (tech uploads a `.brd`), the inline cards transition to the panel-driven surface. Existing step state preserved (read from `protocol.json`).

### 4.3 Tech answers in chat instead of via the input field

If the tech types "VIN at R49 = 24.5V" in chat, the agent must call `bv_record_step_result(step_id="s_1", value=24.5, unit="V")` itself rather than just narrate. System prompt is updated to enforce this. The chat path and the UI path converge on the same persistence + state machine.

### 4.4 Tech declines protocol

Tech types "non, pas de protocole, on bavarde". Agent does NOT call `bv_propose_protocol`. Conversation continues as today. No surface change. (The agent's prompt makes the protocol opt-in by default — emit only when the tech is in measurement mode and a clear plan helps.)

## 5. Data shape

### 5.1 Protocol artifact

```jsonc
{
  "protocol_id": "p_8f3a1c2e",
  "repair_id": "c25f8bc32cd9",
  "device_slug": "mnt-reform-motherboard",
  "title": "Diagnostic VIN dead — fuse + short hunt",
  "rationale": "Symptôme 'pas de boot, écran noir' + D9 dark — F1 prio puis short-hunt VIN bus",
  "rule_inspirations": ["rule-vin-dead-001"],   // optional, traceability
  "current_step_id": "s_1",                      // null when fully done
  "status": "active",                            // active | completed | abandoned | replaced
  "created_at": "2026-04-25T20:45:00Z",
  "completed_at": null,
  "steps": [ /* see §5.2 */ ],
  "history": [ /* see §5.3 */ ]
}
```

### 5.2 Step

```jsonc
{
  "id": "s_1",
  "type": "numeric",                  // numeric | boolean | observation | ack
  "target": "R49",                    // refdes for board anchor; null when N/A
  "test_point": null,                 // free-form alternative to target (e.g. "TP3")
  "instruction": "Probe VIN avec adapter branché, batteries retirées",
  "rationale": "Si <1V, F1 ou short en aval — on enchaîne F1 puis short-hunt",
  "unit": "V",                        // numeric only — V | mV | A | mA | Ω | kΩ
  "nominal": 24.0,                    // numeric only — reference value
  "pass_range": [9.0, 32.0],          // numeric only — UI marks ✓/✗ at submit
  "status": "active",                 // pending | active | done | skipped | failed
  "result": null                      // see §5.4
}
```

Type-specific fields:

| `type` | UI input | Optional fields |
|---|---|---|
| `numeric` | number + unit picker | `unit`, `nominal`, `pass_range` |
| `boolean` | Oui / Non buttons | `expected: true \| false` (UI marks pass when match) |
| `observation` | textarea | none |
| `ack` | "Fait" button | none — used for *actions* like "reflow U7", not measurements |

### 5.3 History entries

Append-only audit trail. The tech sees the human-readable `reason` strings; the agent reads it on session resume to reconstruct intent.

```jsonc
[
  {"action": "proposed", "step_count": 5, "ts": "2026-04-25T20:45:00Z"},
  {"action": "step_completed", "step_id": "s_1", "outcome": "pass", "ts": "2026-04-25T20:46:14Z"},
  {"action": "step_inserted", "step_id": "s_3b", "after": "s_3",
   "reason": "VIN à 0V — short-hunt avant remplacement de F1", "ts": "2026-04-25T20:48:02Z"},
  {"action": "step_skipped", "step_id": "s_4", "reason": "tech: pas de scope", "ts": "..."},
  {"action": "completed", "verdict": "F1 remplacé, boot OK", "ts": "..."}
]
```

Action vocabulary: `proposed`, `step_completed`, `step_inserted`, `step_skipped`, `step_replaced`, `step_failed`, `replaced_protocol`, `completed`, `abandoned`.

### 5.4 Step result shape

```jsonc
// numeric
{"value": 24.5, "unit": "V", "outcome": "pass", "note": null, "ts": "..."}
// boolean
{"value": true, "outcome": "pass", "note": null, "ts": "..."}
// observation
{"value": "joint visiblement sec sur U7 broche 4", "ts": "..."}
// ack
{"value": "done", "ts": "..."}
// skipped
{"value": null, "outcome": "skipped", "skip_reason": "pas de scope", "ts": "..."}
```

`outcome` is computed by the **frontend at submit time** when `pass_range` / `expected` is present (numeric, boolean) — purely UI-side green/red signal. Backend recomputes server-side as ground truth before persisting (defense in depth: the LLM later trusts the server value, never the frontend's).

## 6. Frontend surfaces

The frontend has three coexisting render modes for the same protocol artifact, gated by board availability and a tech-toggleable preference.

### 6.1 Mode A — Floating instruction card (with board)

A glass card (per CLAUDE.md "glass overlay" rule — `rgba(panel, .92)`, `backdrop-filter: blur(10px)`, 1px `--border`) anchored above the current step's component on the canvas. Position: above-right of the bbox by 12px, with collision avoidance against the canvas edges.

Contents:
- Header: badge `①` + step type chip (mono, uppercase)
- Instruction text (Inter 13px)
- Input field appropriate to step type
- Submit button + secondary "Skip" button

When the active step's `target` is offscreen, the card pins to the canvas edge nearest the bbox with a hairline pointer-line back to the component.

### 6.2 Mode B — Wizard panel (with board)

The right slide-in column is split horizontally when a protocol is active. Top **40 %** (clamped 220–360 px, with a vertical splitter the tech can drag) is the wizard, bottom is the existing chat panel scrolling normally. When no protocol is active, the wizard is unmounted and the chat takes the full column as today — no layout shift on protocol absence.

Wizard contents, top to bottom:
- Header: protocol title (Inter 13 px) + "abandonner" link (text-link style, no button).
- **Plan** — vertical list of steps. Each row: `① done · R49 · 24.5V ✓` (mono for value, Inter for label). Past rows compact, future rows dim (`--text-3`). Active row expanded with the same controls as the floating card (inputs are bidirectionally bound between A and B — typing in one updates the other; only one submit fires the event).
- **History** — collapsible foldout below the plan. Reads `protocol.history` rendered as one line per entry with relative timestamps. Default collapsed.

### 6.3 Mode C — Inline step card (no board)

When no board is loaded, Modes A (floating card) and B (wizard panel) are not mounted. The chat panel takes the full right column as today, and the protocol surfaces as **chat-stream "step card" bubbles**:

- A step card is rendered as a synthetic bubble (role: `protocol`, distinct from `user` / `assistant`) immediately after each `protocol_proposed` (one bubble for the active step) or `protocol_updated` event whose `current_step_id` changes (one bubble for the new active step).
- Past steps are summarized inline as compact text rows: `① ✓ R49 — 24.5V` (Inter + mono mix). They scroll with the chat history.
- Only the most recent active step bubble is interactive; older bubbles render their submitted result.
- Submitting from the active bubble fires the same `protocol_step_result` event as Mode A/B.

This keeps the no-board path coherent with conversation flow: the tech reads the agent's prose, then sees the step card immediately below it, fills it in, and the agent's next narration arrives below — all in one scrolling stream.

### 6.4 Board-side indicators

When a board is loaded, each step's `target` (refdes) gets a 14 px numbered circular badge ① ② … rendered in `web/brd_viewer.js` alongside the existing agent-highlight render path. The badge does **not** replace the current cyan halo for ad-hoc highlights — the two coexist (halo for ad-hoc agent attention, badge for protocol membership).

### 6.5 Semantic colors — no new tokens, no repurposing

Per CLAUDE.md §"Design tokens": `--cyan` = component, `--amber` = symptom/warn, `--emerald` = net/rail (locked to that meaning), `--violet` = action (likewise). The badge uses **only `--cyan` and `--amber`**, with state encoded by **fill/glyph**, not new colors:

| Step status | Badge appearance |
|---|---|
| `pending` | Hollow cyan outline (1.5 px stroke `--cyan`, fill `--panel-2`), number in mono |
| `active` | Filled `--cyan`, number in `--bg-deep`, pulse animation reused from §AGENT highlight (3.2 s envelope, 0.005 wave freq, single 8 px outer ring — not the 4-ring halo, which would stack ugly across many badges) |
| `done` | Filled `--cyan`, glyph `✓` in mono replacing the number, no pulse, alpha 0.7 (gently fades into past) |
| `skipped` | Filled `--amber`, glyph `·` in mono |
| `failed` | Filled `--amber`, glyph `✗` in mono |

`done` deliberately stays cyan (still a component-anchor) but de-saturated via alpha rather than recolored. `--emerald` is **not** used — it would conflict with its locked "net/rail" meaning when the same board renders both badges and net highlights at once.

## 7. Backend tools

Three new custom tools added to the bv_* family. They live in `api/tools/protocol.py` (new module — keep `boardview.py` focused on display ops) and dispatch from both runtimes via the existing `dispatch_bv` / direct dispatch pattern. Manifest entries added to `api/agent/manifest.py`. Bootstrap script (`scripts/bootstrap_managed_agent.py`) needs `--refresh-tools` after the change.

### 7.1 `bv_propose_protocol`

```python
{
  "title": str,
  "rationale": str,
  "rule_inspirations": list[str] | None,
  "steps": list[StepInput]    # see §5.2 minus id/status/result — server assigns
}
```

Behavior:
1. Generates a fresh `protocol_id`; assigns sequential `s_N` step IDs.
2. If a prior `active` protocol exists for this repair, marks it `replaced` (history entry on the OLD protocol). The new one becomes `active`.
3. Sets `current_step_id` to the first step; that step's `status` to `active`.
4. Persists to `memory/{slug}/repairs/{rid}/protocol.json` (canonical) and `protocols/{protocol_id}.json` (archive — replaced protocols stay readable).
5. Emits `protocol_proposed` WS event (envelope below).
6. Returns `{ok: true, protocol_id, step_count}`.

Validation:
- `target` must be either a known refdes in the board (when board loaded) **or** null/test point. Unknown refdes → tool returns `{ok: false, reason: "unknown-refdes", closest_matches: [...]}` (mirror `mb_get_component`'s anti-hallucination contract).
- Type-specific required fields enforced by Pydantic (numeric requires `unit`, etc.).
- Step count cap 12 per protocol — agents that want more should land them via `insert` after observing earlier results.

### 7.2 `bv_update_protocol`

```python
{
  "action": "insert" | "skip" | "replace_step" | "reorder" | "complete_protocol" | "abandon_protocol",
  "step_id": str | None,        # required for skip/replace_step
  "after": str | None,          # required for insert
  "new_step": StepInput | None, # required for insert/replace_step
  "new_order": list[str] | None,# required for reorder (full ordered ids)
  "reason": str,                # required — appended to history
  "verdict": str | None         # required for complete_protocol
}
```

Behavior: mutate the active protocol, append history entry, persist, emit `protocol_updated`. Always returns `{ok, protocol_id, current_step_id}` or `{ok: false, reason}` on illegal transition.

Illegal transitions (returned as soft errors, not exceptions): inserting a step after a `done` step, reordering to drop the current step, etc. The tool returns the reason; agent re-plans.

### 7.3 `bv_record_step_result`

```python
{
  "step_id": str,
  "value": float | bool | str | None,
  "unit": str | None,
  "observation": str | None,
  "skip_reason": str | None,
  "submitted_by": "agent" | "tech"   # default "agent"
}
```

Behavior:
1. Routes to existing measurement plumbing based on step type:
   - `numeric` → `mb_record_measurement(target, value, unit, nominal, source=submitted_by)` (which already auto-classifies sim observation when applicable). The measurement `target` argument is `step.target` when set; otherwise `tp:` + `step.test_point` (prefix disambiguates refdes-shaped vs test-point identifiers in the measurement log). A numeric step with neither `target` nor `test_point` is rejected at `bv_propose_protocol` validation, so this branch never sees `null`-on-both.
   - `boolean` → `mb_set_observation(target, mode={alive|dead|unknown})` based on `value` and step's `expected`. Same `target` / `tp:` rule as numeric.
   - `observation` → no measurement plumbing call; persist on the step only.
   - `ack` → no plumbing.
2. Marks the step `done` / `skipped` / `failed` server-side (recomputes outcome from `pass_range` / `expected`).
3. Advances `current_step_id` to the next `pending` step. If none remain, sets it to null.
4. Appends history entry.
5. Emits `protocol_updated` WS event with full new state.
6. **When `submitted_by="tech"`** (i.e. user submitted via UI, not the agent narrating), also synthesizes a `user.message` to the MA session:
   ```
   [step_result] step=s_1 target=R49 value=24.5V outcome=pass · plan: 5 steps, current=s_2
   ```
   so the agent reacts on its next turn.

Tools are listed in the MA manifest with **single-action** descriptions (CLAUDE.md cap = 1024 chars, easy budget here).

## 8. Frontend → backend event protocol

New WS events on the existing `/ws/diagnostic/{slug}` channel:

| Direction | Event | Payload |
|---|---|---|
| server → client | `protocol_proposed` | `{protocol_id, title, rationale, steps, current_step_id}` |
| server → client | `protocol_updated` | `{protocol_id, action, current_step_id, steps, history_tail}` |
| client → server | `protocol_step_result` | `{protocol_id, step_id, value, unit?, observation?, skip}` |
| client → server | `protocol_abandon` | `{protocol_id, reason: "tech_dismiss"}` |
| server → client | `protocol_completed` | `{protocol_id, verdict, summary}` |

`protocol_updated` carries only the **history_tail** (last 1-3 entries) to keep the WS frame small; the full history is fetchable via a new `GET /pipeline/repairs/{rid}/protocol` endpoint when the panel needs it (e.g. reopen).

The client-to-server `protocol_step_result` is dispatched server-side as if the agent had called `bv_record_step_result` itself with `submitted_by="tech"`.

## 9. State machine

```
proposed ──► active ──┬──► step done ──► (next step active OR completed)
                      ├──► step skipped ──► (next step active OR completed)
                      ├──► step failed ──► (next step active OR completed; agent typically inserts a step here)
                      ├──► insert/reorder/replace_step ──► active (mutated)
                      └──► abandoned (tech-driven OR agent-driven)

active ──► replaced (when a new protocol replaces the active one)
```

Invariants (server-enforced):
- Exactly one step has `status="active"` while protocol is `active`.
- `current_step_id` points to the active step.
- `done` / `skipped` / `failed` is terminal for that step; no return to `active` (the agent inserts a fresh step instead).
- `pending` is the only non-terminal non-active state.
- A `replaced` or `abandoned` protocol is read-only.

## 10. Persistence

```
memory/{slug}/repairs/{rid}/
  protocol.json                  # canonical pointer to the active protocol (may be empty if none)
  protocols/
    p_8f3a1c2e.json              # full artifact, append-only history
    p_4d22aa1e.json              # archived prior protocol
```

`protocol.json` shape:
```json
{
  "active_protocol_id": "p_8f3a1c2e",
  "history": [{"protocol_id": "p_4d22aa1e", "status": "replaced", "ts": "..."}, {"protocol_id": "p_8f3a1c2e", "status": "active", "ts": "..."}]
}
```

A repair always has at most one active protocol. The pointer file is small and fast to read on session open. The `protocols/` directory keeps replaced protocols for audit / future reuse.

### 10.1 Reopen flow

On WS open for a repair that has an active protocol:
1. `runtime_managed.py` (and `runtime_direct.py`) loads `protocol.json`, then the active artifact.
2. Sends a `protocol_proposed` event to the frontend right after `session_ready`, so the wizard panel + board badges hydrate before the chat replay.
3. The synthetic intro for the agent is unchanged. The agent's tools include `bv_get_protocol` (read-only) so it can re-read current state without us having to inflate the system prompt.

### 10.2 `bv_get_protocol` (fourth tool, read-only)

```python
{}
```

Returns the active protocol artifact (same shape as §5.1) or `{active: false}`. Used by the agent on resume or whenever it suspects state drift.

## 11. Agent prompt changes

Updates to both the MA bootstrap (`scripts/bootstrap_managed_agent.py::SYSTEM_PROMPT`) and the direct runtime (`api/agent/manifest.py::render_system_prompt`):

- Add a **PROTOCOL** section after the existing tool list, explaining the four tools, when to call `bv_propose_protocol`, when to call `bv_update_protocol` (insert when results force, skip when tech says no tool, abandon when the tech rejects), and when to call `bv_record_step_result` (when the tech mentions a measurement in chat instead of the UI).
- Reinforce: **agent emits the typed plan, even when inspired by `rules.json` `diagnostic_steps`** — the rules' free-text is input, not output.
- Trigger guidance: emit a protocol **after** the agent has either (a) matched a rule with confidence ≥ 0.6, OR (b) identified ≥ 2 plausible likely_causes from `mb_hypothesize`. Don't emit on first turn unless the symptom is unambiguous.
- Tech opt-out: if the tech has said in this conversation "pas de protocole" / "on bavarde" / "no steps", do not emit. Persist this preference in the synthetic state? V1 simpler: agent tracks via conversation history. V2 may add a flag.

The emit-on-results rule keeps the protocol from feeling pushy.

## 12. Error handling

| Error | Where | Behavior |
|---|---|---|
| Agent emits unknown refdes in a step | `bv_propose_protocol` | Tool returns `{ok: false, reason: "unknown-refdes", closest_matches: [...]}`; agent picks a match or asks the tech |
| Agent emits >12 steps | `bv_propose_protocol` | Tool returns `{ok: false, reason: "step_count_cap"}`; agent re-emits a tighter plan |
| Agent calls `bv_update_protocol` with illegal transition | `bv_update_protocol` | Soft error returned, agent corrects |
| Frontend submits result for a non-active step | `protocol_step_result` handler | Backend returns `{ok: false, reason: "step_not_active"}`; frontend shows a toast, no state change |
| Frontend submits malformed payload (e.g. numeric value but step type is boolean) | Server-side Pydantic | 400 to client, frontend re-syncs state via a pulled `GET /pipeline/repairs/{rid}/protocol` |
| WS dropped mid-step-submit | Frontend retry | Frontend keeps the un-submitted value in local state, retries on reconnect; server is idempotent on the same `(step_id, ts)` |
| Sanitizer wraps an unknown refdes in the agent's narration **about** a step | Existing `api/agent/sanitize.py` | Unchanged — protocol artifact is structured (no sanitizer pass needed); only narration prose is sanitized |

## 13. Testing

- **Backend unit tests** (`tests/tools/test_protocol.py` — new) :
  - State machine: propose, advance, insert, skip, replace_step, reorder, complete, abandon. One test per transition.
  - Refdes validation: unknown refdes → soft error; known refdes → accepted.
  - Reuse of `mb_record_measurement`: a numeric step with a known refdes results in a `MeasurementEvent` in the measurement log.
  - Persistence: on disk shape matches §10. Reopen reads back identical state.
- **Backend integration test** (`tests/agent/test_protocol_e2e.py` — new):
  - Mocked agent fires `bv_propose_protocol` then `bv_record_step_result` then `bv_update_protocol(insert)`. Assert WS events emitted in order.
  - Synthetic `user.message` injection is verified.
- **Frontend** : we don't have a JS test runner per CLAUDE.md, so coverage is **manual browser verification**. Per memory `feedback_visual_changes_require_user_verify`, every visual landing requires Alexis to validate before commit.
- **No agent calls in tests** — the slow tests already gated `@pytest.mark.slow` are out of scope; the protocol logic is deterministic and testable cheaply.

## 14. Phasing

Per CLAUDE.md commit hygiene "one cohesive feature = one commit, never bundle web/ + api/ in the same commit". This spec lands as **3 commits** :

1. `docs(spec): stepwise diagnostic protocol design` — this file alone, lands first so subsequent code commits can reference it.
2. `feat(protocol): backend (schemas + tools + WS + prompts + tests)` — `api/tools/protocol.py`, manifest registration, dispatcher routing in both runtimes, system-prompt updates in `manifest.py` + `bootstrap_managed_agent.py`, full `tests/tools/test_protocol.py` + `tests/agent/test_protocol_e2e.py`. Bootstrap refresh required after merge (`python scripts/bootstrap_managed_agent.py --refresh-tools`).
3. `feat(web): protocol UI surfaces (wizard + board badges + floating card + inline fallback)` — single frontend commit covering Modes A, B, C since they share state (a `protocolState` module in `web/js/`) and gating logic. Splitting them would create temporary half-rendered states.

If commit 2 grows past ~600 lines or touches both runtime files heavily, consider splitting into 2a (`feat(protocol): schemas + persistence + state machine`) and 2b (`feat(protocol): tools + WS + prompts`). Decision deferred to plan-writing phase based on actual line count.

## 15. V2 / future

- Extend `rules.json` `diagnostic_steps` schema with typed fields, migrate existing rules. Auditor enforces the new shape. Once enough protocols have been authored manually, freeze the schema.
- Photo / vision step type. Opus reads the photo, classifies "joint sec / propre / arrache", advances.
- Cross-repair protocol library — surface "this protocol resolved 3 prior repairs on the same symptom" as a starting point.
- Tech-editable protocol — let the tech reorder via drag, add a step ad-hoc, with the agent informed.
- Compare across protocols on the same device (which step broke most often, average time per step).

## 16. Open questions parked

These were considered and **deliberately punted**:

- *Should the protocol take over the chat panel, or coexist?* Coexist (§6). Tech may always type free-form; agent bridges.
- *Should the agent be able to mark a protocol "completed" without an explicit verdict?* No — `complete_protocol` requires `verdict`, history-traceable.
- *Does `mb_validate_finding` get called automatically on protocol complete?* No. It remains a separate explicit call by the agent or a tech-triggered "Marquer fix" UI action. Protocols and findings have different lifetimes and validators.
- *Concurrent protocols across tiers (fast/normal/deep) on the same repair?* No — one active protocol per repair regardless of tier. Tier change reuses the same active protocol on the new agent.
