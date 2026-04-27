# Architecture

Reference for the `wrench-board` codebase. Read this before any structural
change to the pipeline, the diagnostic runtime, the deterministic engines,
the boardview parsers, or the tool registry. Pairs with `CLAUDE.md` (rules
and conventions) which is the authoritative source for project policy.

The product is an agent-native diagnostic workbench for board-level
microsoldering repair. All code is original (Apache 2.0). Open hardware
only in the repository; the runtime is brand-agnostic by design.

---

## TL;DR

Four orthogonal AI workflows produce and consume one shared on-disk corpus
under `memory/{device_slug}/`. Two pure-sync deterministic engines sit at
the core of the diagnostic stack. One overnight self-improvement loop
optimises the engines and the agent prompts against frozen oracle scoring.

| # | Workflow | Cadence | Entry point |
|---|----------|---------|-------------|
| **A** | Knowledge Factory | offline, per device | `POST /pipeline/generate` |
| **B** | Schematic Ingestion | offline, per device | `POST /pipeline/ingest-schematic` |
| **C** | Diagnostic Runtime | live, per session | `WS /ws/diagnostic/{slug}` |
| **D** | microsolder-evolve | overnight, autonomous | bash + Claude Code skill |

Plus one offline calibration step (Bench Generator) bridging A+B and C.

```
               ┌────────────────────────────────────────────────────────┐
               │             memory/{device_slug}/  (canonical)         │
               │   pack JSONs, schematic graph, repairs/, macros, …     │
               └───┬───────────────┬────────────────┬──────────────┬────┘
   device_label  ──┘   PDF schema ─┘  WS sessions ──┘   eval data ─┘
       │                │                  │                │
       ▼                ▼                  ▼                │
┌────────────┐  ┌──────────────────┐  ┌────────────────┐    │
│ Workflow A │  │ Workflow B       │  │ Workflow C     │    │
│ Knowledge  │  │ Schematic        │  │ Diagnostic     │    │
│ Factory    │  │ Ingestion        │  │ Runtime        │    │
│ Scout +    │  │ Render + Vision  │  │ Managed agent  │    │
│ Registry + │  │ + Merge +        │  │ + custom tools │    │
│ Writers ×3 │  │ Compile + …      │  │ + 4-store mem  │    │
│ + Auditor  │  │  → Electrical    │  │ + sanitizer    │    │
│            │  │    Graph         │  │                │    │
└────────────┘  └──────────────────┘  └────────┬───────┘    │
       │                │                      │            │
       └─────► Bench Generator ─────► reliability score      │
                (calibration)                  │             │
                                               ▼             ▼
                                  Workflow D microsolder-evolve
                                  (mutates simulator / hypothesize /
                                   compiler / vision / agent prompts;
                                   keeps via git commit `evolve:` or
                                   reverts based on oracle score)
```

A and B are independent; B can run while A is queued. C consumes whatever
artefacts exist and degrades gracefully when one is missing (the agent
loses `mb_schematic_graph` and `mb_hypothesize` if there is no
`electrical_graph.json`, but everything else still works). D runs against
the same artefacts and oracle that C reads from.

---

## Workflow A — Knowledge Factory

**Goal.** Turn a device label (free text, e.g. "iPhone X") into a verified
on-disk knowledge pack the agent can query during diagnosis.

**Entry point.** `api/pipeline/orchestrator.py::generate_knowledge_pack(device_label, documents=None)`
is the async function that runs all phases sequentially and writes each
artefact under `memory/{slug}/`. HTTP wrapper: `POST /pipeline/generate` in
`api/pipeline/__init__.py`.

### Phases

| Phase | Module | Model | Forced tool | Output (memory/{slug}/) |
|-------|--------|-------|-------------|-------------------------|
| 1 Scout | `scout.py` | Sonnet 4.6 | native `web_search` (not forced) | `raw_research_dump.md` |
| 2 Registry | `registry.py` | Sonnet 4.6 | `submit_registry` | `registry.json` |
| 2.5 Mapper | `mapper.py` | Sonnet 4.6 | `submit_refdes_mapping` | mapping registry → graph refdes (intermediate) |
| 3 Writers ×3 | `writers.py` | Opus 4.7 (Cartographe + Clinicien) + Sonnet 4.6 (Lexicographe) | `submit_knowledge_graph`, `submit_rules`, `submit_dictionary` | `knowledge_graph.json`, `rules.json`, `dictionary.json` |
| 4 Auditor | `auditor.py` | Opus 4.7 | `submit_audit_verdict` | `audit_verdict.json` |

Auditor verdicts: `APPROVED` ends the run, `NEEDS_REVISION` triggers
`_apply_revisions()` which re-runs the flagged writers (capped by
`pipeline_max_revise_rounds`), `REJECTED` raises. `pipeline/drift.py`
performs a deterministic vocabulary check (refdes emitted by Writers vs
Registry); a drift report is fed to the Auditor as ground truth and
forces termination after the round cap.

Model assignment is sourced from `api/config.py` via
`anthropic_model_main` (Opus 4.7), `anthropic_model_sonnet` (Sonnet 4.6),
`anthropic_model_fast` (Haiku 4.5).

### Support modules

| Module | Role |
|--------|------|
| `prompts.py` | Single source for every persona system prompt (Scout, Registry, Mapper, Cartographe, Clinicien, Lexicographe, Auditor, phase narrator). |
| `events.py` | asyncio pubsub keyed by slug. The orchestrator publishes `phase_started` / `phase_progress` / `phase_completed` / `phase_narration` / `coverage_check_*` / `expand_*` events. The WS `/pipeline/progress/{slug}` relays them. |
| `phase_narrator.py` | Post-phase Haiku narration (2-3 sentences). Reads the artefact, summarises, publishes via `events.py`. Decoupled — narration failure does not block the pipeline. |
| `expansion.py` | Targeted self-extend: focused Scout + Clinicien on a `focus_symptoms` set. Appends to `raw_research_dump.md` and rewrites `rules.json` incrementally. Used by `POST /pipeline/packs/{slug}/expand` and the `expand` branch of `POST /pipeline/repairs`. |
| `coverage.py` | Haiku forced-tool call — classifies whether a tech symptom is already covered by the existing `rules.json`. Returns `{covered, confidence, matched_rule_id, reason}`. The `confidence ≥ 0.7` threshold short-circuits generation. |
| `intent_classifier.py` | Haiku forced-tool call backing `POST /pipeline/classify-intent`: free-text label → top-3 device slugs with confidence. Used by the landing 2-field form. |
| `subsystem.py` | Pure deterministic — tags each graph node (`power`/`charge`/`usb`/`display`/`audio`/`rf`/`io`/`compute`) via regex on refdes and signal names. No LLM. |
| `graph_transform.py` | `pack_to_graph_payload()` — synthesises action nodes, emits the JSON consumed by `web/js/graph.js` (column order: Actions → Components → Nets → Symptoms). |
| `tool_call.py` | `call_with_forced_tool()` — wraps `messages.create` with `tool_choice={"type":"tool"}` and Pydantic validation. Used by every forced-tool call site in the pipeline. |
| `telemetry/token_stats.py` | Per-phase token tracking (input / output / cache_read / cache_creation). Surfaced via progress events. |

### Notable behaviours

- **Scout resilience.** Handles SDK `pause_turn` resumption, rejects thin
  dumps (low symptom/source/component counts), retries with broadened
  scope. Accepts optional inputs from the technician — schematic graph,
  parsed boardview, datasheet PDFs — and seeds web search with the MPN
  list extracted from those documents. The `focus_symptom` parameter
  allocates 3-4 web_search queries to a specific tech symptom (used by
  the `full` and `expand` branches of `POST /pipeline/repairs`).
- **Writers cache prefix.** All three writers share a long prefix (raw
  dump + registry + system prompt) marked `cache_control: ephemeral`.
  Writer 1 (Cartographe) starts first and writes the cache. The
  orchestrator sleeps `cache_warmup_seconds` then fans out writers 2
  (Clinicien) and 3 (Lexicographe) concurrently via `asyncio.gather`.
- **Single shape source.** All Pydantic models for the pack live in
  `api/pipeline/schemas.py`. They double as runtime validators and as
  JSON Schema sources for the forced-tool `input_schema`. Never duplicate
  a shape — import from there.

### `POST /pipeline/repairs` — three-branch routing

The main client entry point ("new ticket"). Given a `device_label` plus
`symptom`, the orchestrator picks one of three branches to minimise
latency and cost.

```
                POST /pipeline/repairs {device_label, symptom}
                                │
                                ▼
                ┌──────────────────────────────┐
                │ pack present under memory/?  │
                └───────┬─────────┬────────────┘
                       NO        YES
                        │         │
                        ▼         ▼
              ┌─────────────┐    coverage.check_symptom_coverage()
              │ kind="full" │     Haiku forced-tool on rules.json
              │ run full    │     │
              │ factory     │     ▼
              │ focus_symp= │     covered & conf≥0.7   covered=False
              │ symptom     │     & matched_rule_id    or conf<0.7
              └─────────────┘             │                    │
                                          ▼                    ▼
                                ┌──────────────────┐  ┌────────────────────┐
                                │ kind="none"      │  │ kind="expand"      │
                                │ no LLM           │  │ expansion.expand_  │
                                │ return matched   │  │ pack(focus_        │
                                │ rule_id +        │  │ symptoms=[…])      │
                                │ coverage_reason  │  │ scout+clinicien    │
                                └──────────────────┘  │ targeted, append   │
                                                      │ to rules.json      │
                                                      └────────────────────┘
```

`RepairResponse` exposes `pipeline_kind` (`full`/`expand`/`none`),
`matched_rule_id`, `coverage_reason`. The frontend (`web/js/home.js`)
reads these to decide whether to open the pipeline timeline (full/expand)
or to enter the repair directly (none).

---

## Workflow B — Schematic Ingestion

**Goal.** Compile a PDF schematic into a queryable `ElectricalGraph`
usable by the deterministic engines and surfaced to the agent through
`mb_schematic_graph`. Independent of Workflow A.

**Entry point.** `api/pipeline/schematic/orchestrator.py::ingest_schematic(pdf_path, device_slug, client)`.
HTTP wrapper: `POST /pipeline/ingest-schematic`.

### Stages

```
PDF
 │
 ▼
┌──────────────────────────────────────┐
│ 1. renderer.py                       │  pdftoppm (poppler) → PNG per page
│    + pdfplumber scan-detection       │  parametrised DPI, orientation hint
└──────────────────────────────────────┘
 │
 ▼
┌──────────────────────────────────────┐
│ 2. grounding.py (optional)           │  text/layout markers extraction
│                                      │  stabilises the vision pass
└──────────────────────────────────────┘
 │
 ▼
┌──────────────────────────────────────┐
│ 3. page_vision.py                    │  Opus 4.7 vision (DO NOT migrate
│    forced tool: submit_schematic_    │  to Sonnet — see CLAUDE.md note
│    page                              │  on rail-name OCR hallucinations)
│                                      │  cache warmup: page 1 serial,
│                                      │  then asyncio.gather for the rest
└──────────────────────────────────────┘
 │   memory/{slug}/schematic_pages/page_NNN.json
 ▼
┌──────────────────────────────────────┐
│ 4. merger.py     (deterministic)     │  dedup refdes cross-page,
│                                      │  stitch nets by label,
│                                      │  synth __local__{page}__{id}
└──────────────────────────────────────┘
 │   schematic_graph.json
 ▼
┌──────────────────────────────────────┐
│ 5. compiler.py   (deterministic)     │  classify edges (power/signal),
│                                      │  rail extraction + voltage,
│                                      │  Kahn topo sort → boot_sequence,
│                                      │  attach quality report
└──────────────────────────────────────┘
 │   electrical_graph.json
 ▼
┌──────────────────────────────────────┐
│ 6. net_classifier.py (optional)      │  Haiku/Sonnet forced tool
│    submit_net_classification         │  classify each net:
│                                      │  power / signal / clock / reset /
│                                      │  data_bus / connector + voltage.
│                                      │  Regex fallback in offline mode.
└──────────────────────────────────────┘
 │   nets_classified.json
 ▼
┌──────────────────────────────────────┐
│ 7. passive_classifier.py (optional)  │  LLM forced tool + heuristic
│    submit_passive_classification     │  fallback. Per-passive role:
│                                      │  decoupling / pull_up / pull_down /
│                                      │  series / feedback / tank /
│                                      │  signal_path
└──────────────────────────────────────┘
 │   passive_classification_llm.json
 ▼
┌──────────────────────────────────────┐
│ 8. boot_analyzer.py (optional)       │  Opus 4.7 post-compile pass.
│    submit_analyzed_boot_sequence     │  Refines boot_sequence with
│                                      │  always-on / sequenced / on-demand
│                                      │  + sequencer_refdes. Graceful
│                                      │  fail.
└──────────────────────────────────────┘
     boot_sequence_analyzed.json
```

Steps 6–8 are independent and can be triggered separately
(`POST /pipeline/packs/{slug}/schematic/classify-nets`,
`POST /pipeline/packs/{slug}/schematic/analyze-boot`). Inside
`ingest_schematic` they are gathered with `asyncio.gather` to avoid
serialising latency.

### Notable details

- **All shapes** live in `api/pipeline/schematic/schemas.py` — same rule
  as Workflow A.
- **Boot sequence dual-path.** `compiler.boot_sequence` is the
  deterministic topological order; `boot_analyzer.analyzed_boot_sequence`
  is the Opus-refined version. The simulator accepts either.
- **Compiler is deterministic.** No LLM after stage 3. The deterministic
  engines (simulator, hypothesize) depend on this purity.
- **CLI.** `python -m api.pipeline.schematic.cli pdf page` is a single-page
  vision debug tool, not a full ingestion entry point. It also re-runs
  the passive classifier on an existing pack via `--classify-passives SLUG`.

---

## Bench Generator (calibration step)

**Goal.** Auto-generate test scenarios (cause → expected cascade) from a
finished knowledge pack to calibrate the simulator's reliability score on
a given device. Sits between Workflow A+B (which it reads) and Workflow C
(which reads its output).

**Entry point.**
`api/pipeline/bench_generator/orchestrator.py::generate_from_pack(slug)`.
CLI: `scripts/generate_bench_from_pack.py --slug=…`.

### Pipeline

```
memory/{slug}/registry.json + rules.json + knowledge_graph.json
    + electrical_graph.json + raw_research_dump.md
                         │
                         ▼
       ┌────────────────────────────────┐
       │ extractor.py    Sonnet 4.6     │  propose_scenarios (forced tool)
       │   + optional Opus rescue       │  for span / topology rejects
       └───────────────┬────────────────┘
                       │  list[ProposedScenarioDraft]
                       ▼
       ┌────────────────────────────────┐
       │ validator.py    deterministic  │  V1 sanity
       │                                │  V2 grounding (evidence_span ⊂
       │                                │      source_quote, literal)
       │                                │  V2b semantic (refdes + rails
       │                                │      mentioned, topologically
       │                                │      connected)
       │                                │  V3 topology (refdes/rails exist
       │                                │      in ElectricalGraph)
       │                                │  V4 pertinence (mode/kind)
       │                                │  V5 dedup
       └───────────────┬────────────────┘
                       │  survivors + rejections
                       ▼
       ┌────────────────────────────────┐
       │ scoring.py    F1-soft          │  tunable FP/FN weights
       └───────────────┬────────────────┘
                       │
                       ▼
       ┌────────────────────────────────┐
       │ writer.py                      │
       │   memory/{slug}/               │
       │     simulator_reliability.json │  global score + per-scenario
       │   benchmark/auto_proposals/    │  per-run archive + latest.json
       └────────────────────────────────┘
```

`simulator_reliability.json` is read by `api/agent/reliability.py` and
injected as a one-line tag in both diagnostic runtime system prompts so
the agent can flag low-reliability devices to the technician. Skipping
this file silently degrades the agent's self-awareness.

### Frozen oracle vs auto-generated — strict separation

| Artefact | Frozen human oracle | Auto-generated |
|----------|---------------------|----------------|
| Path | `benchmark/scenarios.jsonl` (~17 scenarios, hand-validated) | `benchmark/auto_proposals/…` |
| Use | Final scoring (`scripts/eval_simulator.py`, the evolve loop) | Per-device reliability calibration |
| Editing | Human-curated, read-only for AI | Regenerable per device |

The two never merge. The split closes a score-gaming back-door (commit
`4d0c9ba`).

---

## Workflow C — Diagnostic Runtime

**Goal.** Live conversation between the technician and a Claude agent
with access to the boardview, the knowledge pack, and the deterministic
engines via custom tools.

**Entry point.**
`WS /ws/diagnostic/{device_slug}?tier={fast|normal|deep}&repair={id}&conv={id}`
in `api/main.py`.

### Two runtimes, two memory strategies

The two runtimes are not a primary plus a fallback in the trivial sense.
They implement the same WebSocket protocol but carry different
philosophies. `DIAGNOSTIC_MODE=managed|direct` (default `managed`) picks
one. The frontend cannot tell which is running.

| Dimension | `runtime_managed.py` (~3150 LOC) | `runtime_direct.py` (~1130 LOC) |
|-----------|-----------------------------------|----------------------------------|
| Pack delivery | Mounted as a Managed Agents `memory_store` (read-only) at `/mnt/memory/wrench-board-{slug}/`, queried via the `agent_toolset_20260401` `read/write/edit/grep` toolset | Not mounted — the agent calls `mb_*` on demand; tools re-read JSONs from disk each time |
| Cost profile | Pack is out of every-turn context, accessed via filesystem on demand | Pack re-paid each `mb_get_component`, amortised by `cache_control: ephemeral` on system+tools |
| History | Anthropic-side persistence (30 d via `client.beta.sessions.events.list`) plus local JSONL mirror | Local JSONL is the only source; replayed into `messages=[…]` on reopen |
| Recovery from expired session | Haiku summarises the dead session, summary injected into the new one | Direct replay of the JSONL |
| Cross-repair memory | `mirror_outcome_to_memory` writes validated findings to the device store, accessible to any future session | Findings on disk only, re-read explicitly when needed |
| SDK dependency | `client.beta.agents`, `client.beta.sessions`, `client.beta.memory_stores` (beta header `managed-agents-2026-04-01`) | Standard `client.messages.stream` |
| When to use | Repeated work on the same device (a workshop seeing the same model 10× / month) | Demos, local dev, MA outage, simpler inspection |

### Tier selection

Query string `?tier=` at WS open.

| Tier | Model | Use |
|------|-------|-----|
| `fast` | `claude-haiku-4-5` | triage, cheap classification |
| `normal` | `claude-sonnet-4-6` | default conversation |
| `deep` | `claude-opus-4-7` | causal reasoning, hypothesize |

Switching tier means reopening the WebSocket — explicit new conversation,
no in-session swap.

### Custom tools

Manifest: `api/agent/manifest.py`. Total: **36 tools** (verified
`grep -c '"name":' api/agent/manifest.py`). Selection is dynamic via
`build_tools_manifest(session)` — BV control tools are stripped when no
boardview is loaded; `cam_capture` only appears when `session.has_camera`.

| Family | Count | Tools | Implementation | Dispatched from |
|--------|-------|-------|----------------|-----------------|
| MB knowledge | 5 | `mb_get_component`, `mb_get_rules_for_symptoms`, `mb_record_finding`, `mb_record_session_log`, `mb_expand_knowledge` | `api/agent/tools.py` | `runtime_*._dispatch_mb_tool()` |
| MB schematic + hypothesize | 2 | `mb_schematic_graph`, `mb_hypothesize` | `api/tools/schematic.py`, `api/tools/hypothesize.py` | idem |
| MB measurements | 6 | `mb_record_measurement`, `mb_list_measurements`, `mb_compare_measurements`, `mb_observations_from_measurements`, `mb_set_observation`, `mb_clear_observations` | `api/tools/measurements.py` (+ `agent/measurement_memory.py`) | idem |
| MB validation | 1 | `mb_validate_finding` | `api/tools/validation.py` (+ `agent/validation.py`) | idem |
| BV control (boardview) | 13 | `bv_scene`, `bv_highlight`, `bv_focus`, `bv_reset_view`, `bv_flip`, `bv_annotate`, `bv_dim_unrelated`, `bv_highlight_net`, `bv_show_pin`, `bv_draw_arrow`, `bv_measure`, `bv_filter_by_type`, `bv_layer_visibility` | `api/tools/boardview.py` | `api/agent/dispatch_bv.py` |
| BV protocol (stepwise) | 4 | `bv_propose_protocol`, `bv_update_protocol`, `bv_record_step_result`, `bv_get_protocol` | `api/tools/protocol.py` | `runtime_*._dispatch_protocol_tool()` |
| Profile | 3 | `profile_get`, `profile_check_skills`, `profile_track_skill` | `api/profile/tools.py` | `runtime_*._dispatch_profile_tool()` |
| Live vision | 1 | `cam_capture` | `runtime_managed.py::_dispatch_cam_capture` (+ `agent/macros.py`) | managed-only, conditional on `session.has_camera` |
| Sub-agent consultation | 1 | `consult_specialist` | `runtime_managed.py::_run_subagent_consultation` (+ `_run_knowledge_curator`) | managed-only |

Two tools are **managed-only** by construction: `cam_capture` (live
camera frame capture is wired through Managed Agents events) and
`consult_specialist` (sub-agent chaining is a `client.beta.agents.create`
primitive). The manifest masks both in direct mode.

`api/agent/tool_dispatch.py` factors the shared waterfall (extracted
2bac918) so that the dispatch ladder is no longer duplicated across the
two runtimes.

### Support modules in `api/agent/`

| Module | Role |
|--------|------|
| `chat_history.py` | Append-only JSONL per conversation under `memory/{slug}/repairs/{rid}/conversations/{cid}/messages.jsonl`. Source of truth for direct-mode replay and the managed-mode mirror. Also `index.json` (conv list) and `status.json` (`open` / `in_progress` / `closed`). |
| `conversation_log.py` | Per-conversation outcome narrative. `record_session_log` writes a synthetic Markdown under `memory/{slug}/conversation_log/{stamp}_{rid}_{cid}.md` (per-device, not per-repair) and mirrors to the device MA store. Consumed by multi-repair retrospectives. |
| `recovery_state.py` | Reconstructs a Markdown state block (`build_repair_state_block`) from `outcome.json` + `measurements.jsonl` + `diagnosis_log.jsonl` so a WS reopen retrieves context without an LLM summariser. |
| `board_state.py` | Boardview overlay snapshot (highlights, focus, dim, layer visibility). Persisted under `memory/{slug}/repairs/{rid}/board_state.json` after each `bv_*` call. `replay_board_state_to_ws` replays events on WS reopen so the frontend recovers the exact overlay. |
| `macros.py` | Files+Vision: persists drag-drop uploads and `cam_capture` frames under `memory/{slug}/repairs/{rid}/macros/`. Path-safe by construction (`macro_path_for` rejects `..`). Builds Anthropic `ImageRef` blocks for vision turns. |
| `field_reports.py` | Cross-session findings (per-device, not per-repair). Mirrored to the device MA store when MA is up. |
| `measurement_memory.py` | Per-repair measurement journal (`measurements.jsonl`). Auto-classifies V/A/W/°C/Ω → `ComponentMode` / `RailMode`. Synthesises `Observations` for the simulator. |
| `diagnosis_log.py` | Append-only per-repair log of every `mb_hypothesize` turn — observation, hypothesis, pruning. Consumed by the evolve evaluation loops. |
| `validation.py` | `RepairOutcome` persistence (`outcome.json` per repair). Receives the technician's "Mark fix" click. |
| `schematic_boardview_bridge.py` | Enriches the `SimulationTimeline` (schematic-side) with the `Board` 2D part positions (PCB-side). Emits an `EnrichedTimeline` plus up to 8 `ProbePoint` (physical measurement route). |
| `reliability.py` | Reads `simulator_reliability.json`, injects a single-line tag into both runtime system prompts. |
| `memory_seed.py` | First WS open of a fresh repair: writes pack + findings into the agent's first context (managed: filesystem seed; direct: first-turn injection). Marker file `managed.json` skips re-seed. |
| `memory_stores.py` | Per-device cache of MA memory stores. Three symmetric `ensure_*` functions (see *Memory architecture*) for the layered model. NoOp when MA beta is unavailable. |
| `managed_ids.py` | Loader for `managed_ids.json` (env + 3 tier-scoped agents Haiku/Sonnet/Opus). |
| `pricing.py` | Token cost estimator (April 2026: Haiku $1/$5, Sonnet $3/$15, Opus $5/$25; cache-read 0.10×, cache-creation 1.25×). Surfaced in progress events. |
| `sanitize.py` | Anti-hallucination guardrail (see *Anti-hallucination*). |
| `tool_dispatch.py` | Shared `ToolContext` registry — extracted dispatch waterfall used by both runtimes. |
| `session_start_mode.py` | Decides between fresh-seed and resume on WS open. |

### Memory architecture — 4 stores layered

Managed mode attaches **four memory stores** to every session, each with
its own scope and write regime. This is what lets the agent resume a
repair without reloading the pack and reach its scribe notebook without
an LLM resummarising the conversation.

| Store name (Anthropic side) | Scope | Mode | Content |
|------------------------------|-------|------|---------|
| `wrench-board-global-patterns` | cross-device | RO | Archetypes (e.g. "rail dead → check decoupling caps adjacent"). Hand-curated. |
| `wrench-board-global-playbooks` | cross-device | RO | Measurement-protocol templates fed to `bv_propose_protocol`. Hand-curated. |
| `wrench-board-{slug}` | per-device | RO | Mirror of the knowledge pack — `registry`, `rules`, `dictionary`, `knowledge_graph`, `electrical_graph` files. Seeded by `memory_seed.seed_memory_store_from_pack`. |
| `wrench-board-repair-{slug}-{repair_id}` | per-repair | RW | Agent's scribe notebook — `state.md`, `decisions/<n>.md`, `measurements/<n>.md`, `open_questions.md`. Written by the agent itself via `agent_toolset_20260401`. |

On repair reopen, the agent runs `read state.md` instead of receiving a
pre-cooked summary — it self-orients.

Three symmetric `ensure_*` functions in `memory_stores.py`:
- `ensure_global_store(client, kind)` — idempotent, shared across devices (`patterns` / `playbooks`).
- `ensure_memory_store(client, slug)` — per-device.
- `ensure_repair_store(client, slug, repair_id)` — per-repair.

Direct mode has no equivalent: without MA the agent cannot keep a scribe
between sessions, so `recovery_state.build_repair_state_block`
synthetically rebuilds context from disk artefacts (`outcome.json` +
`measurements.jsonl` + `diagnosis_log.jsonl`).

### Files + Vision — the agent can ask to see

A microsoldering diagnosis depends on what the probe is touching at the
instant. Two mechanisms surface that to the agent:

- **Drag-drop** from the frontend → WS event `client.upload_macro` →
  `_handle_client_upload_macro` (managed runtime) →
  `macros.persist_macro` saves under
  `memory/{slug}/repairs/{rid}/macros/<file>` → an Anthropic `ImageRef` is
  inserted in the next turn.
- **Agent request** via `cam_capture` (managed-only) — agent emits the
  tool call, runtime sends `server.request_capture` to the frontend, the
  frontend grabs a frame from the selected USB camera and replies with
  `client.capture_response`. `_handle_client_capture_response` persists
  the image and the agent receives the `tool_result` with an `ImageRef`
  it can read on the following turn.

Session fields:
- `session.has_camera: bool` — drives manifest gating.
- `session.pending_captures: dict[capture_id, asyncio.Future]` —
  correlates request and response.

Replay: `GET /api/macros/{slug}/{repair_id}/{filename}` (path-safe).

The `VISION` block in `bootstrap_managed_agent.py::SYSTEM_PROMPT`
explicitly instructs the agent when to request a frame.

### Knowledge Curator + `consult_specialist`

Sub-agent invocable from the managed runtime via `consult_specialist`.
When the main agent hits a pointed knowledge question, it delegates:

- `_run_subagent_consultation` (generic) — orchestrates the call, relays
  the result back as the tool_result.
- `_run_knowledge_curator` (current specialisation) — sub-agent that
  searches `wrench-board-{slug}` + `wrench-board-global-patterns` and
  synthesises.

Managed-only because the chaining primitive is
`client.beta.agents.create`, not Messages API.

### Stepwise diagnostic protocol

When the agent leaves Q&A mode and enters measurement-plan mode, it emits
a numbered protocol ("1. measure 5V0 on PP5V0_S0, expected 5.0 V; 2. if
dead, measure U2300 input; …"). Four BV-namespaced tools manage the
cycle:

| Tool | Effect |
|------|--------|
| `bv_propose_protocol` | Creates a new `Protocol` (title, rationale, steps) and attaches it to the active conversation. |
| `bv_update_protocol` | Adds / removes / reorders / cancels a step. |
| `bv_record_step_result` | Records a measurement (value + unit + note) and advances `current_step_id`. |
| `bv_get_protocol` | Reads the active protocol (used after a session is interrupted). |

Persistence: `memory/{slug}/repairs/{rid}/protocol.json` with `history`
append-only. Frontend endpoint
`GET /pipeline/repairs/{repair_id}/protocol`. Models in
`api/tools/protocol.py::Protocol/Step/StepResult/HistoryEntry`.

### Persistence layout (per-repair)

```
memory/{slug}/
  repairs/{repair_id}/
    conversations/{conv_id}/
      messages.jsonl              # all Anthropic-shaped events
      status.json                 # open | in_progress | closed
      ma_session_{tier}.json      # MA session id (managed only)
    index.json                    # conv list, tiers, costs
    findings.json                 # snapshot of attached field reports
    outcome.json                  # validated fix (mb_validate_finding)
    protocol.json                 # active stepwise protocol + history
    measurements.jsonl            # append-only measurement journal
    diagnosis_log.jsonl           # turn-by-turn observations + hypotheses
    board_state.json              # boardview overlay snapshot
    macros/<filename>.{png,jpg}   # uploads + cam_capture frames
  conversation_log/
    {stamp}_{rid}_{cid}.md        # per-conversation outcome narrative
                                  # (per-device, not per-repair)
  managed.json                    # seed marker + store ids (managed only)
```

`messages.jsonl` is appended in **both** modes — the mirror lets us
rebuild history when an MA session expires (30 d TTL).

### Bootstrap (managed mode prerequisite)

Before the first WS open in `DIAGNOSTIC_MODE=managed`, run once:

```bash
.venv/bin/python scripts/bootstrap_managed_agent.py
```

The script creates the MA environment + 3 tier-scoped agents
(Haiku/Sonnet/Opus) and writes `managed_ids.json` at the repo root
(gitignored). Idempotent. Without it, `runtime_managed.py::load_managed_ids()`
raises and the WS returns an error — direct mode has no bootstrap step.

---

## The deterministic engines (the core)

Two pure-sync modules under `api/pipeline/schematic/`. Neither calls the
network at runtime. The microsolder-evolve loop optimises both.
**Cosmetic refactors are forbidden** so that score deltas remain clean.

### `simulator.py` — `SimulationEngine`

Event-driven behavioural simulator. Walks phase-by-phase over the
`boot_sequence` (or the `analyzed` boot sequence when present), takes a
list of failures (`refdes` + `mode`) plus optional rail overrides, and
emits a `SimulationTimeline` carrying for each phase:

- dead rails / live rails;
- dead components (cascade through dependencies);
- signal states;
- the cause of blocking.

Surfaced to the agent via
`mb_schematic_graph(query="simulate", failures=…, rail_overrides=…)` and
to the UI via `POST /pipeline/packs/{slug}/schematic/simulate`.

Public types (in `api/pipeline/schematic/simulator.py`): `BoardState`,
`SimulationTimeline`, `Failure`, `RailOverride`, `SimulationEngine`.

### `hypothesize.py` — inverse diagnosis

Takes a partial observation (components or rails dead/alive) and
enumerates refdes-kill candidates that explain it:

- **Single-fault exhaustive.** Simulate each refdes killed individually,
  score against the observation with F1-soft-penalty.
- **2-fault pruned.** Top-K survivors of single-fault × components whose
  cascade intersects the residual unexplained observations.

Returns top-N with structured diff and a deterministic French narration.
Depends on `SimulationEngine`. No I/O, no LLM.

Public types: `ObservedMetric`, `Observations`, `HypothesisMetrics`,
`HypothesisDiff`, `Hypothesis`, `PruningStats`, `HypothesizeResult`.

### Property-based invariants

`tests/pipeline/schematic/test_simulator_invariants.py` ships 10
contracts the two modules must honour for every device that has an
`electrical_graph.json` under `memory/`. The runner auto-discovers
devices via `_discover_devices()` and applies the invariants to each.

| # | Invariant | Guarantees |
|---|-----------|------------|
| INV-1 | cascade ⊆ graph | Simulator never fabricates a refdes. |
| INV-2 | `failures = []` ⇒ empty cascade | No spontaneous death. |
| INV-3 | every cascade death has a physical cause | No orphan death. |
| INV-4 | source death ⇒ rail dead | Power causality. |
| INV-5 | rail dead ⇒ consumers dead (unless live alternative) | Coherent propagation. |
| INV-6 | determinism | Same input → same timeline run-to-run. |
| INV-7 | rail with no source immune to internal kills | No magic death. |
| INV-8 | top-5 recall ≥ threshold on relevant pairs | hypothesize finds the cause. |
| INV-9 | cascade verdict consistency | No internal contradiction. |
| INV-10 | `hypothesize` on empty observation ⇒ no positive score | No false signal without signal. |

This is the safety net for `microsolder-evolve` — any commit `evolve:`
that breaks one invariant is reverted immediately.

---

## Workflow D — microsolder-evolve

**Goal.** Overnight autonomous loop that mutates targeted files, scores
the change against a frozen oracle, and either keeps it (commit prefixed
`evolve:`) or reverts via git. Runs while the technician sleeps.

**Mechanism.** Bash loop (`scripts/*evolve-runner.sh`) spawns a fresh
`claude -p` session every ~60 seconds. Each session loads a skill from
`.claude/skills/`, runs **one** iteration (analyse → propose → edit →
measure → keep/discard → log), and exits. State lives under `evolve/`.

### Four surfaces, four loops

| Loop | Skill | Files mutated | Eval script | Oracle |
|------|-------|---------------|-------------|--------|
| `sim` | `microsolder-evolve` | `api/pipeline/schematic/simulator.py`, `api/pipeline/schematic/hypothesize.py` | `scripts/eval_simulator.py` | `benchmark/scenarios.jsonl` (~17 hand-validated) |
| `pipeline` | `microsolder-pipeline-evolve` | `api/pipeline/schematic/compiler.py`, `net_classifier.py`, `passive_classifier.py` | `scripts/eval_pipeline.py` | per-device `electrical_graph.json` invariants |
| `pipeline-vision` | `microsolder-pipeline-evolve-vision` | `api/pipeline/schematic/page_vision.py`, `grounding.py`, `renderer.py` | `scripts/eval_pipeline_vision.py` | re-run vision on N test pages |
| `agent` | `microsolder-agent-evolve` | `scripts/bootstrap_managed_agent.py` (SYSTEM_PROMPT), `api/agent/manifest.py` (tool descriptions), `api/agent/sanitize.py` (refdes detection) | `scripts/eval_diagnostic_agent.py` (managed mode, tier=normal/Sonnet) | `benchmark/agent_scenarios.jsonl` |

Bootstrap and runner pairs:
- `scripts/evolve-bootstrap.sh` + `scripts/evolve-runner.sh`
- `scripts/agent-evolve-bootstrap.sh` + `scripts/agent-evolve-runner.sh`
- `scripts/pipeline-evolve-bootstrap.sh` + `scripts/pipeline-evolve-runner.sh`
- `scripts/pipeline-evolve-vision-bootstrap.sh` + `scripts/pipeline-evolve-vision-runner.sh`

### Hard rules for the loops

- The oracle (`benchmark/scenarios.jsonl`) is **read-only for the evolve
  agent**; only humans curate it.
- The evaluator (`api/pipeline/schematic/evaluator.py`) is **off-limits**
  to the loop — closes the score-gaming back-door (commit `4d0c9ba`).
- Commits prefixed `evolve:` mixed in with `feat:` / `fix:` commits are
  expected; reverts of `evolve:` commits are also normal (anti-pattern
  detection by the loop itself).
- A commit that breaks any of the 10 simulator invariants is reverted
  automatically by the runner.

Reference docs: `docs/EVOLVE.md` (operator-level), specs and plans under
`docs/superpowers/`.

---

## Cross-cutting — anti-hallucination

Hard rule #5 of `CLAUDE.md`. Defense in depth, two layers.

1. **Tool discipline.** Every tool that surfaces refdes data returns
   `{found: false, closest_matches: [...]}` for unknown inputs.
   `mb_get_component` (in `api/agent/tools.py`) uses Levenshtein-validated
   matching against the registry plus the parsed boardview. `bv_*` tools
   cross-check against `session.board.part_by_refdes()`. The system
   prompt instructs the agent to pick from `closest_matches` or ask the
   user — never fabricate.
2. **Post-hoc sanitizer.** Every outbound `agent.message` text passes
   through `api/agent/sanitize.py::sanitize_agent_text` before
   `ws.send_json`. The sanitizer scans for refdes-shaped tokens
   (`\b[A-Z]{1,3}\d{1,4}\b`) and, when a board is loaded, validates them
   against `session.board.part_by_refdes`. Unknown matches are wrapped as
   `⟨?U999⟩` and logged server-side.

Both runtimes (managed + direct) route through the sanitizer — there is
no path that bypasses it.

---

## Cross-cutting — boardview parsers

Registry-based dispatch under `api/board/parser/`. A parser is one file
that decorates `@register` and declares `extensions = (".ext",)`. Adding
a format = one new file, no edit to `base.py`.

12 concrete parsers ship today.

### DONE — verified on real files

| Parser | Format | Notes |
|--------|--------|-------|
| `test_link.py` | OpenBoardView `.brd` v3 ASCII (clean-room) | Refuses obfuscated files via `ObfuscatedFileError`. |
| `brd2.py` | KiCad-boardview `.brd2` | KiCad's standard ASCII export. |
| `kicad.py` | `.kicad_pcb` native | Helpers in `_kicad_extract.py`. |
| `asc.py` | ASUS TSICT `.asc` (multi-file or combined) | Directory-aware on `format.asc` / `parts.asc` / `pins.asc` / `nails.asc`. |
| `fz.py` | ASUS PCB Repair Tool `.fz` | Magic-byte dispatch: zlib (clear) or XOR (gated by `WRENCH_BOARD_FZ_KEY`, returns 422 if absent). |
| `bdv.py` | HONHAN BoardViewer `.bdv` | Symmetric arithmetic decryption (key 160..286), then re-parses as Test_Link. |
| `cad.py` | GenCAD 1.4 ASCII (Mentor / Allegro) + dispatch umbrella | Delegates to `_gencad.py`, `_fz_zlib.py`, `BRD2Parser`, or `Test_Link` depending on shape. |

### PARTIAL — implemented, limited coverage

- `tvw.py` — Tebo IctView 3.0/4.0. Decodes ASCII rotation cipher (rot-13
  / rot-10) or rejects binary containers honestly.

### SPECULATIVE — heuristics, not validated end-to-end

- `bv.py` (ATE BoardView 1.5) — detects ASCII Test_Link shape; rejects binary input.
- `gr.py` (BoardView R5.0) — detects markers `Components:` / `Pins:` / `TestPoints:`.
- `cst.py` (IBM Lenovo Castw v3.32) — detects INI-style `[Format] [Components] [Pins] [Nails]`.
- `f2b.py` (Unisoft ProntoPLACE Place5) — detects ASCII Test_Link markers; ignores `Annotations:`.

These four extract correctly when fed a Test_Link-shape ASCII dialect but
no real proprietary file has been parsed end-to-end yet. The label is
explicit so the technician is not lied to.

### Shared helpers

- `_ascii_boardview.py` — `parse_test_link_shape(text, dialect)` factors
  out the Test_Link-shape dialects.
- `_fz_zlib.py` — zlib decompression + pipe-delimited format scanner
  (Quanta / ASRock / ASUS Prime / Gigabyte).
- `_gencad.py` — ASCII GenCAD 1.4 parser (`$HEADER`, `$SHAPES`,
  `$COMPONENTS`, `$SIGNALS`).
- `_kicad_extract.py` — modules and nets extraction from KiCad s-expr.

### Anti-hallucination on the board side

`api/board/validator.py` is pure — no I/O. `is_valid_refdes`,
`resolve_part`, `resolve_net`, `resolve_pin`, `suggest_similar` (Levenshtein
neighbours) are all called by the sanitizer and by the MB tools.

### Integration tests on real boards

- `tests/board/test_parser_real_hardware.py` — fixtures from the MNT
  Reform motherboard (493 parts, 2 104 pins) — open hardware.
- `tests/board/test_parser_consistency.py` — cross-parser invariants on
  every detected file.
- `tests/board/test_parser_realistic_scale.py` — scale-up sweep.
- `tests/board/test_real_files_runner.py` — drop-in runner for ad hoc
  local files.

---

## Cross-cutting — on-disk corpus

`memory/{device_slug}/` is the canonical store. HTTP endpoints, MB tools,
and the UI Memory Bank all read from here. Nothing else duplicates the
shapes.

### Producer / consumer matrix

| Artefact | Written by | Read by |
|----------|------------|---------|
| `raw_research_dump.md` | `pipeline/scout.py` (+ append from `pipeline/expansion.py`) | `pipeline/registry.py`, `pipeline/writers.py`, `pipeline/bench_generator/extractor.py` |
| `registry.json` | `pipeline/registry.py` | `pipeline/mapper.py`, `pipeline/writers.py`, `pipeline/drift.py`, `agent/tools.py::mb_get_component`, `web/js/memory_bank.js` |
| `knowledge_graph.json` | `pipeline/writers.py::Cartographe` | `pipeline/graph_transform.py`, `pipeline/subsystem.py`, `web/js/graph.js` |
| `rules.json` | `pipeline/writers.py::Clinicien` (+ `pipeline/expansion.py`) | `pipeline/coverage.py`, `agent/tools.py::mb_get_rules_for_symptoms`, `pipeline/bench_generator` |
| `dictionary.json` | `pipeline/writers.py::Lexicographe` | `agent/tools.py::mb_get_component` |
| `audit_verdict.json` | `pipeline/auditor.py` | `web/js/home.js`, `web/js/memory_bank.js` |
| `schematic_pages/page_NNN.json` | `pipeline/schematic/page_vision.py` | `pipeline/schematic/merger.py` |
| `schematic_graph.json` | `pipeline/schematic/merger.py` | `pipeline/schematic/compiler.py` |
| `electrical_graph.json` | `pipeline/schematic/compiler.py` | `simulator.py`, `hypothesize.py`, `api/tools/schematic.py`, `pipeline/bench_generator`, `tests/pipeline/schematic/test_simulator_invariants.py` (auto-discovery) |
| `boot_sequence_analyzed.json` | `pipeline/schematic/boot_analyzer.py` | `simulator.py` (via `analyzed_boot=…`), `pipeline/__init__.py` (optional merge) |
| `nets_classified.json` | `pipeline/schematic/net_classifier.py` | `pipeline/__init__.py` (merge), `api/tools/schematic.py::mb_schematic_graph(query="net_domain")` |
| `passive_classification_llm.json` | `pipeline/schematic/passive_classifier.py` | `hypothesize.py` (cascade selection by passive role), `compiler.py` (post-merge) |
| `simulator_reliability.json` | `pipeline/bench_generator/writer.py` | `agent/reliability.py` |
| `field_reports/*.md` | `agent/field_reports.py::record_field_report` | MA device store mirror, agent (via filesystem `grep`) |
| `conversation_log/{stamp}_{rid}_{cid}.md` | `agent/conversation_log.py::record_session_log` | `agent/conversation_log.py::list_session_logs`, MA store mirror |
| `repairs/{rid}/conversations/{cid}/messages.jsonl` | `agent/chat_history.py::append_event` | `runtime_direct.py` (replay), `runtime_managed.py` (JSONL fallback summary) |
| `repairs/{rid}/measurements.jsonl` | `agent/measurement_memory.py::append_measurement` | `api/tools/measurements.py::mb_*_measurements`, simulator observations |
| `repairs/{rid}/diagnosis_log.jsonl` | `agent/diagnosis_log.py::append_turn` | evolve evaluation corpus |
| `repairs/{rid}/outcome.json` | `agent/validation.py::record_outcome` | UI repair row, `agent/recovery_state.build_repair_state_block` |
| `repairs/{rid}/board_state.json` | `agent/board_state.py::save_board_state` | `agent/board_state.py::replay_board_state_to_ws` (WS reopen) |
| `repairs/{rid}/protocol.json` | `api/tools/protocol.py::save_protocol` | `api/tools/protocol.py::load_active_protocol`, `GET /pipeline/repairs/{rid}/protocol` |
| `repairs/{rid}/macros/<file>` | `agent/macros.py::persist_macro` | `GET /api/macros/{slug}/{rid}/{filename}`, agent (re-vision) |
| `managed.json` (per-device) | `agent/memory_seed.py::write_seed_marker` | `agent/memory_seed.py::read_seed_marker` (skip re-seed) |

**Invariant.** Any new module producing a JSON under `memory/{slug}/`
must declare its shape in `pipeline/schemas.py` or
`pipeline/schematic/schemas.py`. No ad hoc shapes in markdown or
comments.

---

## Cross-cutting — HTTP + WebSocket surface

Sources of truth: `api/main.py` (3), `api/board/router.py` (1),
`api/profile/router.py` (4), `api/pipeline/__init__.py` (~34).

### Pipeline — packs and lifecycle (`api/pipeline/__init__.py`)

- `POST /pipeline/generate` — synchronous knowledge factory (~30–120 s)
- `POST /pipeline/ingest-schematic` — Workflow B HTTP wrapper
- `GET  /pipeline/packs` — pack list with presence bitmask
- `GET  /pipeline/packs/{slug}` — pack metadata
- `GET  /pipeline/packs/{slug}/full` — bundle of all JSON artefacts (Memory Bank)
- `GET  /pipeline/packs/{slug}/findings` — field reports for a device
- `GET  /pipeline/packs/{slug}/graph` — synthesised graph payload
- `POST /pipeline/packs/{slug}/expand` — `pipeline/expansion.py` (focused Scout + Clinicien)
- `POST /pipeline/packs/{slug}/documents` — upload technician-supplied datasheets / schematic / boardview
- `GET  /pipeline/packs/{slug}/documents` — uploaded document list
- `GET  /pipeline/packs/{slug}/sources` — source attribution per artefact
- `PUT  /pipeline/packs/{slug}/sources/{kind}` — switch source for a given artefact kind
- `GET  /pipeline/taxonomy` — brand > model > version tree (home view)

### Pipeline — schematic and engines

- `GET  /pipeline/packs/{slug}/boardview` (+ HEAD)
- `GET  /pipeline/packs/{slug}/schematic.pdf` (+ HEAD)
- `GET  /pipeline/packs/{slug}/schematic` — `electrical_graph.json` + meta
- `GET  /pipeline/packs/{slug}/schematic/pages` — raw vision pages
- `GET  /pipeline/packs/{slug}/schematic/pages/{page_n}.png` (+ HEAD)
- `GET  /pipeline/packs/{slug}/schematic/boot` — analysed boot sequence
- `GET  /pipeline/packs/{slug}/schematic/passives` — passive classification
- `POST /pipeline/packs/{slug}/schematic/analyze-boot` (202) — fires `boot_analyzer` in background
- `POST /pipeline/packs/{slug}/schematic/classify-nets` (202) — fires net classifier
- `POST /pipeline/packs/{slug}/schematic/simulate` — drives `SimulationEngine`
- `POST /pipeline/packs/{slug}/schematic/hypothesize` — inverse diagnosis from observation

### Pipeline — repairs, conversations, measurements

- `POST   /pipeline/repairs` — three-branch routing (full / expand / none)
- `GET    /pipeline/repairs` — repair list (home)
- `GET    /pipeline/repairs/{repair_id}` — repair metadata
- `DELETE /pipeline/repairs/{repair_id}` — remove a repair (artefacts + MA store cleanup)
- `GET    /pipeline/repairs/{repair_id}/conversations` — conversation list
- `DELETE /pipeline/repairs/{repair_id}/conversations/{conv_id}` — remove a conversation
- `GET    /pipeline/repairs/{repair_id}/protocol` — active stepwise protocol
- `POST   /pipeline/packs/{slug}/repairs/{repair_id}/measurements` (201) — append to journal
- `GET    /pipeline/packs/{slug}/repairs/{repair_id}/measurements` — read journal

### Pipeline — landing and progress

- `POST /pipeline/classify-intent` — Haiku forced-tool, free-text → top-3 device slugs
- `WS   /pipeline/progress/{slug}` — live events
  (`phase_started`, `phase_progress`, `phase_completed`, `phase_narration`,
  `coverage_check_*`, `expand_*`)

### Board (`api/board/router.py`)

- `POST /api/board/parse` — upload + parse via `parser_for(path)` → `Board` JSON

### Profile (`api/profile/router.py`)

- `GET /profile` — full technician profile (catalog / skills / preferences)
- `PUT /profile/identity`
- `PUT /profile/tools`
- `PUT /profile/preferences`

### Main (`api/main.py`)

- `GET /health` — healthcheck
- `GET /api/macros/{slug}/{repair_id}/{filename}` — replay Files+Vision images
- `WS  /ws/diagnostic/{slug}?tier=&repair=&conv=` — live diagnostic conversation

---

## Cross-cutting — frontend

Vanilla HTML + CSS + JS, no build step, no bundler. D3 v7 for boardview
and graph; marked + DOMPurify for the chat. Inline SVG icons.

### Modules

| File | Role |
|------|------|
| `web/index.html` | Shell — top bar, left rail, metabar, workspace, status bar (all `position: fixed`). |
| `web/js/main.js` | Boot, hash navigation, section dispatch. |
| `web/js/router.js` | `SECTIONS`, `navigate()`, rail button handlers. |
| `web/js/home.js` | List of repairs grouped by brand > model; new-repair modal; calls `POST /pipeline/repairs`. |
| `web/js/landing.js` | Free-text 2-field intent classifier form. |
| `web/js/memory_bank.js` | Pack explorer reading `/pipeline/packs/{slug}/full`. |
| `web/js/graph.js` | D3 force-layout knowledge graph (Actions → Components → Nets → Symptoms). |
| `web/js/schematic.js`, `schematic_minimap.js` | Schematic viewer + minimap. |
| `web/js/pipeline_progress.js` | WS consumer of `/pipeline/progress/{slug}` — drawer UI. |
| `web/js/llm.js` | Diagnostic chat panel; opens WS `/ws/diagnostic/{slug}?…`; auto-opens on `?repair=` URL. |
| `web/js/protocol.js` | Stepwise protocol overlay + confirm modal. |
| `web/js/camera.js`, `camera_preview.js` | USB camera selection + capture preview for `cam_capture`. |
| `web/js/profile.js` | Profile editor wiring `PUT /profile/*`. |
| `web/js/i18n.js` | EN source strings + FR overlays under `web/i18n/`. |
| `web/js/icons.js`, `mascot.js` | Inline SVG icon set + mascot. |
| `web/brd_viewer.js` | D3 boardview renderer; consumes WS boardview events; exposes `window.Boardview`. |

UI ships in English (source), with French overlays generated by a
parallel translation agent into `web/i18n/_modules/*.fr.json`.

Design tokens (`web/styles/tokens.css`) lock four semantic accent colours
to meaning: `--amber` = symptom, `--cyan` = component, `--emerald` =
net/rail, `--violet` = action. Never repurpose them.

---

## Cross-cutting — tests + benchmark

Pytest suite mirrors the `api/` layout. ~1185 test functions across 160
files. Marker `@pytest.mark.slow` for tests that hit the Anthropic API,
ingest a real schematic, or act as accuracy gates. `make test` runs
`-m "not slow"`; `make test-all` runs the full suite.

Key test trees:
- `tests/agent/` — sanitizer, manifest, runtime contracts, MA event flow.
- `tests/board/` — parser fixtures + cross-parser invariants + real
  hardware (MNT Reform).
- `tests/pipeline/` — Scout / Registry / Writers / Auditor / drift /
  expansion / coverage / intent classifier.
- `tests/pipeline/schematic/` — renderer / grounding / page_vision /
  merger / compiler / simulator / hypothesize / **simulator
  invariants** (auto-discovery on every device with an
  `electrical_graph.json`).
- `tests/tools/` — `bv_*`, `mb_*`, protocol, measurements, validation.

Benchmark trees:
- `benchmark/scenarios.jsonl` — frozen oracle for the simulator (~17
  scenarios, hand-validated).
- `benchmark/agent_scenarios.jsonl` — frozen oracle for the diagnostic
  agent.
- `benchmark/sources/` — provenance contract per scenario (per
  `benchmark/README.md`).
- `benchmark/auto_proposals/` — per-device auto-generated scenarios from
  the bench generator (never merged into the frozen oracle).

---

## Layout summary

```
api/
  main.py              FastAPI app + /ws/diagnostic
  config.py            Pydantic Settings (.env)
  pipeline/
    orchestrator.py    Workflow A entry
    scout.py registry.py mapper.py writers.py auditor.py
    drift.py expansion.py coverage.py intent_classifier.py
    subsystem.py graph_transform.py prompts.py events.py
    phase_narrator.py tool_call.py schemas.py
    schematic/
      orchestrator.py  Workflow B entry
      renderer.py grounding.py page_vision.py merger.py
      compiler.py net_classifier.py passive_classifier.py
      boot_analyzer.py simulator.py hypothesize.py
      evaluator.py cli.py schemas.py
    bench_generator/
      orchestrator.py extractor.py validator.py scoring.py
      writer.py prompts.py errors.py schemas.py
  agent/
    runtime_managed.py runtime_direct.py
    manifest.py tool_dispatch.py tools.py
    sanitize.py reliability.py
    chat_history.py conversation_log.py recovery_state.py
    board_state.py macros.py field_reports.py
    measurement_memory.py diagnosis_log.py validation.py
    schematic_boardview_bridge.py
    memory_seed.py memory_stores.py managed_ids.py pricing.py
    session_start_mode.py
    seed_data/global_patterns/ global_playbooks/
  board/
    model.py validator.py router.py
    parser/   12 format parsers + helpers + base + registry
  tools/
    boardview.py schematic.py hypothesize.py
    measurements.py validation.py protocol.py ws_events.py
  profile/
    catalog.py derive.py model.py prompt.py router.py
    store.py tools.py
  session/
    state.py            per-WS-connection container
web/
  index.html brd_viewer.js
  js/      main router home landing memory_bank graph schematic
           pipeline_progress llm protocol camera profile i18n icons
  styles/  tokens layout brd graph home memory_bank pipeline_progress
           llm modal protocol camera schematic stub …
  boards/  demo BRD/KiCad artefacts
  i18n/_modules/  EN source + FR overlays
tests/    pytest suite (160 files, ~1185 functions)
memory/   per-device knowledge packs + repairs (gitignored except .gitkeep)
board_assets/  open-hardware boards + ATTRIBUTIONS.md
benchmark/  oracle scenarios + auto proposals + sources
scripts/   bootstrap_managed_agent.py
           generate_bench_from_pack.py
           eval_simulator.py eval_pipeline.py
           eval_pipeline_vision.py eval_diagnostic_agent.py
           evolve-runner.sh + 3 sibling runners (4 evolve loops)
docs/     ARCHITECTURE.md (this file)
          EVOLVE.md HACKATHON.md
          superpowers/specs/ superpowers/plans/
```

---

## Invariants and known debt

### Never

1. Cosmetic refactor on `simulator.py` or `hypothesize.py`. The
   evolve loop measures deltas there.
2. Write to `benchmark/scenarios.jsonl` from any AI agent. Human-curated
   oracle only.
3. Merge `benchmark/auto_proposals/` into the frozen oracle.
4. Duplicate a JSON shape outside `pipeline/schemas.py` or
   `pipeline/schematic/schemas.py`.
5. Skip writing `simulator_reliability.json`. The agent loses its
   reliability self-awareness.
6. Migrate the pipeline to Managed Agents. The stateless / stateful
   split is intentional.
7. Break any of the 10 simulator invariants. The evolve loop reverts on
   detection.
8. Promote a SPECULATIVE parser to DONE without an end-to-end run on a
   real proprietary file.

### Always

1. Tools return `{found: false, reason: …, closest_matches: […]}`. Never
   fabricated data.
2. Every agent reply passes through `sanitize_agent_text()` before
   `ws.send_json`.
3. Streaming token-by-token over the WebSocket. Never batch a full
   response — same contract for pipeline progress.
4. Use `git commit -- path1 path2` with explicit paths. Evolve runs in
   parallel; `git add .` would bundle its work under the wrong message
   (real incident `e053002`, corrected in `71dd23a`).

### Known architectural debt

| Area | Description | Why we tolerate it |
|------|-------------|---------------------|
| Tool dispatch waterfall | Some tool families still dispatch via if/elif rather than a single registry; `tool_dispatch.py` extracted the shared waterfall but per-runtime branches remain. | Manifest is stable; full registry refactor not yet justified. |
| Boot sequence dual-path | `compiler.boot_sequence` (topological) vs `boot_analyzer.analyzed_boot_sequence` (Opus). | Analyzer is optional with graceful fail; simulator handles either. |
| WS event schema not shared | Backend uses Pydantic (`api/tools/ws_events.py`); frontend matches `event.type` strings. | Cheap enough; TypeScript codegen would cost more than it saves. |
| `memory/` JSON not versioned | No migration framework when a shape evolves. | Files are regenerable from source; `pipeline/schemas.py` is contract-first. |
| Empty stub modules (`api/vision/`, `api/telemetry/`) | Two-line `__init__.py` files. | Reserved namespaces. |

---

## Extension points

### Add a phase to Workflow A

1. New module under `api/pipeline/` with its forced tool.
2. Pydantic shape added to `pipeline/schemas.py`.
3. Call hooked into `orchestrator.generate_knowledge_pack()`.
4. Artefact written under `memory/{slug}/`.
5. Update `pipeline/drift.py` if the phase introduces canonical vocabulary.
6. Update `pipeline/auditor.py` if the verdict must cover the new phase.

### Add a tool to the agent

1. Handler under `api/agent/` or `api/tools/`.
2. Entry in `api/agent/manifest.py` with a JSON Schema `input_schema`.
3. Dispatch branch in **both** runtimes
   (`runtime_managed._dispatch_tool` and the matching ladder in
   `runtime_direct.py`).
4. If BV, also `api/agent/dispatch_bv.py` and `api/tools/boardview.py`.
5. Unit test isolated from runtime.

### Add a boardview parser

1. New file under `api/board/parser/`.
2. Class decorated `@register`, attribute `extensions = (".xxx",)`.
3. Parse to the shared `Board` model (`api/board/model.py`).
4. Minimal fixture under `tests/board/`.

### Add a UI section

1. Append to `SECTIONS` in `web/js/router.js` and entry to `SECTION_META`.
2. Rail button in `web/index.html` with `data-section="…"`.
3. Either a real DOM block or `<section class="stub" data-section-stub="…">`.
4. Handler in `web/js/main.js` if the section needs mount logic.

---

## Cross-references

- Project rules and conventions: [`CLAUDE.md`](../CLAUDE.md)
- Evolve operator guide: [`docs/EVOLVE.md`](EVOLVE.md)
- Specs: [`docs/superpowers/specs/`](superpowers/specs/)
- Plans: [`docs/superpowers/plans/`](superpowers/plans/)
- Benchmark contract: [`benchmark/README.md`](../benchmark/README.md)

This document reflects the repo as of `2026-04-27`. Maintain alongside
structural changes; tactical extensions (one new tool, one new parser,
one new section) belong in their own spec under `docs/superpowers/`.
