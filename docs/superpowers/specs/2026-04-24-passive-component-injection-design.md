# Passive Component Injection (Phase 4) — Design

## Context

Phase 1 of the reverse-diagnostic fault-modes family shipped on 2026-04-23
(23 commits, `4ddb2a2 → 4a3af11`). It handles four IC/rail fault modes
(`dead`, `alive`, `anomalous`, `hot`, `shorted`), a measurement journal,
and a per-mode CI accuracy gate. Field walk-through of MNT Reform
community repairs confirmed a structural limit we already knew: the
engine can only reason about **active** components. Passives (R / C / FB /
D) are already carried through the merger and compiler into
`ElectricalGraph.components`, but they have no `kind` or `role` metadata,
and every cascade they produce is empty — so they get silently pruned out
of the hypothesis ranking. Roughly **60 % of real board-level repair
cases** (decoupling cap shorted, ferrite bead burned, feedback divider
resistor open, series current-limit resistor open) are therefore invisible
to the engine today.

Phase 4 closes that gap. It keeps the existing `ElectricalGraph` shape
rétro-compatible, extends `ComponentNode` with `kind` + `role`, ships a
passive-role classifier module, wires a `(kind, role, mode)` cascade
dispatch table into `_simulate_failure`, surfaces passive-specific
observation modes (`open`, `short`) in the UI picker, and adds
**hand-written scenarios** to the benchmark corpus to avoid the
auto-referential scoring bias that Phase 1 corpus was criticized for.

## Goal

Ship a single Phase 4 drop that:

1. Extends `ComponentNode` with `kind: ComponentKind` and `role: str | None`
   (default `kind="ic"`, `role=None` — all existing Phase 1 data re-loads
   unchanged).
2. Adds `passive_r`, `passive_c`, `passive_d`, `passive_fb` to
   `ComponentKind` (`passive_q` reserved for Phase 4.5, not implemented).
3. Extends `ComponentMode` with `"open"` and `"short"` (union of all
   modes). Coherence `kind ↔ mode` is enforced at `hypothesize()` entry
   against the graph, not in a pure Pydantic validator.
4. Ships `api/pipeline/schematic/passive_classifier.py` — deterministic
   heuristic classifier (role inferred from connected nets + existing
   `decouples` / `filters` / `feedback_in` typed edges), with an optional
   Opus post-pass (parallel to `net_classifier.py`, same call shape).
5. Extends `compile_electrical_graph` to run the passive classifier and
   populate `ComponentNode.kind` + `role` on every R / C / D / FB, and to
   populate `PowerRail.decoupling` from the classifier's output.
6. Implements `_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str],
   Callable]` in `hypothesize.py` — every `(kind, role, mode)` tuple maps
   to an explicit cascade handler. Unmapped combinations return an empty
   cascade (pruned out).
7. Extends `_applicable_modes(electrical, refdes)` to return
   `["open", "short"]` for `kind ∈ {passive_r, passive_c, passive_d,
   passive_fb}` with a known `role`, and `["alive"]` (no candidate) for
   role-less passives.
8. Refactors the transitive-rails cleanup from
   `hypothesize.py::_simulate_failure` (lines 270–280, the `shorted` mode
   patch) up into `SimulationEngine` (separate task T0, committed on its
   own). This is a clean-up, not a feature — its value is to avoid
   re-introducing the same hack when passives trigger source kills.
9. Introduces a **score visibility multiplier** for topologically weak
   cascades: `(passive_c, decoupling, open)` and
   `(passive_r, pull_up, open)` contribute `0.5 × tp` instead of `1.0 ×
   tp` to acknowledge that the physical failure is real but the
   observable signature is soft. Applied as a post-multiplier on the TP
   term in `_score_candidate`. Configurable via a module-level table.
10. Extends the frontend inspector picker with a kind-aware mode set:
    passives → `[unknown, alive, open, short]`; ICs and rails unchanged.
11. Extends `scripts/gen_hypothesize_benchmarks.py` to also sample
    `(passive_refdes, passive_mode)` scenarios, with per-role
    applicability gating so unmatched `(role, mode)` combinations are not
    emitted.
12. Adds **at least three hand-written scenarios** to a new
    `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` file:
    cases where the ground truth passive mismatches the mechanical
    simulator output (so the scoring chain is tested end-to-end against
    field-realistic observations, not against the simulator's own fantasy).

## Non-goals

- **Q (transistor) failures.** Deferred to Phase 4.5. A discrete
  transistor is conceptually more like an IC (has a gain, can be
  driven, can short B-E/B-C in many modes) than a passive. Modelling it
  well requires a mode vocabulary richer than `open`/`short` — out of
  scope here.
- **Numeric proximity scoring.** A measured `+3V3 = 2.87 V` still
  classifies to the discrete `anomalous` bucket; the numeric delta does
  not tune the passive candidate's score. Deferred (Phase 5 as per the
  Phase 1 spec).
- **Passive failure propagation through analog paths.** We do not model
  ESR drift, leakage, or partial shorts. `open` and `short` are binary
  failure modes; everything in between is out of scope.
- **Field-real corpus calibration.** We ship the hand-written scenarios
  in this phase; broader calibration against real oscilloscope traces
  is a separate effort.
- **SimulationEngine composition for multi-fault passive pairs.** The
  existing 2-fault pass unions cascades element-wise, same approximation
  as Phase 1. No attempt to compose passive failure modes across two
  refdes in a physically-consistent way.

## Prerequisites from the existing codebase

Three observations from a careful read of the current implementation — all
locked before drafting this spec:

1. **Passives are already in `ElectricalGraph.components`.** The merger
   does not filter by type, and `compile_electrical_graph` (line 68 of
   `api/pipeline/schematic/compiler.py`) passes `graph.components` through
   unchanged. So the migration is additive — no new top-level field, no
   signature change on `ElectricalGraph`.
2. **Passives are excluded from `PowerRail.consumers`.** The
   `_augment_consumers_from_pins` helper (compiler line 171,
   `_CONSUMER_COMPONENT_TYPES`) intentionally skips passives because
   their relationship to rails is decoupling / filtering, not
   consumption. We keep that exclusion — the `shorted` cascade for a
   passive resolves its affected rail through the new classifier's
   `role` + connected-net lookup, not through `rail.consumers`.
3. **`typed_edges` kinds `decouples`, `filters`, `feedback_in` are already
   emitted by the vision pass.** They are currently excluded from the
   anomalous BFS (`SIGNAL_EDGE_KINDS`). Phase 4 uses them as the primary
   evidence for role classification — `C --decouples--> rail` →
   `C.role = "decoupling"`. No new edge kinds needed.

The net effect of the prerequisites: the spec focuses on logic, not data
migration. No fixture regeneration is required for artefacts the Phase 1
corpus already produced; the classifier simply enriches existing nodes
in place.

## Architecture

Five concerns, each owned by one module.

1. **Shape extension** — `api/pipeline/schematic/schemas.py`. Add
   `ComponentKind`, extend `ComponentNode`. Rétro-compat default values.
2. **Passive role classifier** — `api/pipeline/schematic/passive_classifier.py`
   (new). Deterministic heuristic first, optional Opus enrichment
   second, same shape as `net_classifier.py`. Emits per-refdes
   `(kind, role, confidence)` assignments.
3. **Compiler integration** — `api/pipeline/schematic/compiler.py`.
   Invoke classifier at the end of `compile_electrical_graph`, write
   `kind` / `role` onto `ComponentNode`, populate
   `PowerRail.decoupling` from classifier output.
4. **Cascade dispatcher** — `api/pipeline/schematic/hypothesize.py`.
   New `_PASSIVE_CASCADE_TABLE`, new `_applicable_modes` branch for
   passives, scoring visibility multiplier, T0 refactor of the
   transitive-rails hack into `SimulationEngine`.
5. **Frontend picker** — `web/js/schematic.js` + `web/styles/schematic.css`.
   Contextual mode set per kind, CSS tokens for the new picker entries.

## Data shapes (Pydantic v2, `extra="forbid"` kept)

### `schemas.py`

```python
ComponentKind = Literal[
    "ic",
    "passive_r",
    "passive_c",
    "passive_d",
    "passive_fb",
]
# "passive_q" reserved for Phase 4.5 — intentionally not in the literal
# union to prevent accidental emission before the cascade table handles
# it.

PassiveRole = str
"""Canonical values (non-enforced):
- passive_r: series · feedback · pull_up · pull_down · current_sense · damping
- passive_c: decoupling · bulk · filter · ac_coupling · tank · bypass
- passive_d: flyback · rectifier · esd · reverse_protection · signal_clamp
- passive_fb: filter

Follows the existing PinRole / EdgeKind pattern — free-form string so new
roles don't break the schema, set-membership checks in the cascade
dispatcher gate behavior. Unknown roles simply don't match the dispatch
table → empty cascade → pruned out."""


class ComponentNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refdes: str
    type: ComponentType
    kind: ComponentKind = "ic"      # NEW — default preserves Phase 1 semantics
    role: str | None = None          # NEW — passive role, null for ICs
    value: ComponentValue | None = None
    pages: list[int] = Field(default_factory=list)
    pins: list[PagePin] = Field(default_factory=list)
    populated: bool = True
```

The default `kind="ic"` matters: every Phase 1 `ElectricalGraph` on disk
reloads unchanged, and every existing test fixture passes. Only the
classifier rewrites `kind` to `"passive_*"` when it recognises a passive.

### `hypothesize.py`

```python
ComponentMode = Literal[
    "dead", "alive", "anomalous", "hot",
    "open", "short",
]

FailureMode = Literal[
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
]


class Observations(BaseModel):
    """(unchanged in shape — new mode values flow through the Literal).

    A post-load validator in `hypothesize()` itself cross-checks each
    observed (target, mode) against the compiled graph:

      - state_comps[U*]      must use an IC mode
                              (dead / alive / anomalous / hot)
      - state_comps[passive] must use a passive mode
                              (open / short / alive)

    This cross-graph validation lives in a helper
    `_validate_obs_against_graph(electrical, observations)` called at the
    top of `hypothesize()`. It raises `ValueError` with a descriptive
    message pointing at the offending target. Pure-Pydantic validation
    is not enough: the coherence depends on the loaded graph, not on the
    shape alone.
    """
```

## Passive role classifier

`api/pipeline/schematic/passive_classifier.py` (new, ~250 LOC).

### Heuristic rules (deterministic, no LLM)

Ordered — first match wins, per refdes type:

**Resistors (`type == "resistor"`)**

| Evidence                                                              | Role              |
|-----------------------------------------------------------------------|-------------------|
| `feedback_in` typed edge points at this R                             | `feedback`        |
| Pin on a rail + pin on GND (no signal pin)                            | `pull_down` (rare; warn) |
| Pin on a signal net + pin on a rail                                   | `pull_up`         |
| Both pins on different rails, low value (<1 Ω typical)                | `current_sense`   |
| One pin on a rail, other pin on a consumer's `power_in` pin           | `series`          |
| Both pins on adjacent signal nets (no rail, no GND)                   | `damping`         |
| Fallback                                                              | `null` (unclassified) |

**Capacitors (`type == "capacitor"`)**

| Evidence                                                              | Role              |
|-----------------------------------------------------------------------|-------------------|
| `decouples` typed edge points at this C                               | `decoupling`      |
| Pin on a rail + pin on GND, near a consumer IC (< 3 pin hops)         | `decoupling`      |
| Pin on a rail + pin on GND, large value (>10 µF)                      | `bulk`            |
| Pin on a rail + pin on GND, between a regulator and its load          | `filter`          |
| Both pins on signal nets                                              | `ac_coupling`     |
| Near an oscillator / crystal                                          | `tank`            |
| Fallback                                                              | `null`            |

**Diodes (`type == "diode"`)**

| Evidence                                                              | Role              |
|-----------------------------------------------------------------------|-------------------|
| Across an inductor (both terminals on the same L's pins)              | `flyback`         |
| Between a DC input rail and GND, polarity marker present              | `rectifier`       |
| On a signal net + GND, small package                                  | `esd`             |
| In series between a power input and a regulator                       | `reverse_protection` |
| Between a signal net and a rail (clamping)                            | `signal_clamp`    |
| Fallback                                                              | `null`            |

**Ferrite beads (`type == "ferrite"`)**

Always `role = "filter"` when the ferrite has one pin on a rail and the
other on a downstream rail variant (e.g. `+3V3` → `+3V3_AUDIO`). Null
otherwise.

### Role-inference helpers

The classifier consumes:
- `SchematicGraph.components` — to iterate passives and their pins
- `SchematicGraph.nets` / `graph.typed_edges` — to resolve pin nets
- `ElectricalGraph.power_rails` — to distinguish rails from signals
- `ClassifiedNet` (from `net_classifier`) — to distinguish signal vs rail
  nets when the former isn't yet promoted to `power_rail`

It emits a dict `dict[str, tuple[ComponentKind, str | None, float]]` of
`refdes → (kind, role, confidence)` that the compiler merges into the
`components` dict.

### Optional Opus pass

Same shape as `net_classifier.classify_nets_llm` — an async
`classify_passives_llm(graph, client, model)` that batches 150 passives
per call, uses forced-tool output on a new `PassiveClassification`
Pydantic shape, and merges with the heuristic baseline. The LLM path
runs in parallel with `net_classifier.classify_nets_llm` via
`asyncio.gather` in the compiler, same pattern as `boot_analyzer`.

On LLM failure or missing client, the classifier falls back to the
heuristic output — never raises. Confidence = 0.6 for heuristic hits,
0.9+ for LLM-confirmed.

### Persistence

`passive_classification.json` written next to `electrical_graph.json` in
`memory/{slug}/` — same pattern as `nets_classified.json`. The
`ComponentNode.kind` / `role` fields are the primary store; the JSON is
a cache so the classifier can be re-run in isolation from the CLI
without recompiling the graph.

## Cascade dispatch

### `_PASSIVE_CASCADE_TABLE` — explicit per `(kind, role, mode)`

Lives in `hypothesize.py`. Table of `(kind, role, mode)` →
`Callable[[ElectricalGraph, ComponentNode], dict]`, where the dict is
the 7-key cascade shape (`dead_comps`, `dead_rails`, `shorted_rails`,
`anomalous_comps`, `hot_comps`, `final_verdict`, `blocked_at_phase`).

```python
_PASSIVE_CASCADE_TABLE: dict[tuple[str, str, str], CascadeFn] = {
    # === resistors ===
    ("passive_r", "series",       "open"):  _cascade_series_open,
    ("passive_r", "series",       "short"): _cascade_passive_alive,  # wire, negligible
    ("passive_r", "feedback",     "open"):  _cascade_feedback_open_overvolt,
    ("passive_r", "feedback",     "short"): _cascade_feedback_short_undervolt,
    ("passive_r", "pull_up",      "open"):  _cascade_pull_up_open,
    ("passive_r", "pull_up",      "short"): _cascade_pull_up_short,
    ("passive_r", "pull_down",    "open"):  _cascade_pull_up_open,       # same effect
    ("passive_r", "pull_down",    "short"): _cascade_passive_alive,
    ("passive_r", "current_sense","open"):  _cascade_series_open,        # same as series
    ("passive_r", "current_sense","short"): _cascade_passive_alive,
    ("passive_r", "damping",      "open"):  _cascade_passive_alive,      # cosmetic
    ("passive_r", "damping",      "short"): _cascade_passive_alive,

    # === capacitors ===
    ("passive_c", "decoupling",   "open"):  _cascade_decoupling_open,    # 0.5 visibility
    ("passive_c", "decoupling",   "short"): _cascade_decoupling_short,
    ("passive_c", "bulk",         "open"):  _cascade_decoupling_open,    # same handler
    ("passive_c", "bulk",         "short"): _cascade_decoupling_short,
    ("passive_c", "filter",       "open"):  _cascade_filter_cap_open,    # 0.5 visibility
    ("passive_c", "filter",       "short"): _cascade_decoupling_short,
    ("passive_c", "ac_coupling",  "open"):  _cascade_signal_path_open,
    ("passive_c", "ac_coupling",  "short"): _cascade_signal_path_dc,
    ("passive_c", "tank",         "open"):  _cascade_tank_open,
    ("passive_c", "tank",         "short"): _cascade_tank_short,
    ("passive_c", "bypass",       "open"):  _cascade_decoupling_open,
    ("passive_c", "bypass",       "short"): _cascade_decoupling_short,

    # === diodes ===
    ("passive_d", "flyback",           "open"):  _cascade_flyback_open,
    ("passive_d", "flyback",           "short"): _cascade_flyback_short,
    ("passive_d", "rectifier",         "open"):  _cascade_rectifier_open,
    ("passive_d", "rectifier",         "short"): _cascade_rectifier_short,
    ("passive_d", "esd",               "open"):  _cascade_passive_alive,
    ("passive_d", "esd",               "short"): _cascade_signal_to_ground,
    ("passive_d", "reverse_protection","open"):  _cascade_series_open,
    ("passive_d", "reverse_protection","short"): _cascade_passive_alive,
    ("passive_d", "signal_clamp",      "open"):  _cascade_passive_alive,
    ("passive_d", "signal_clamp",      "short"): _cascade_signal_to_ground,

    # === ferrite beads ===
    ("passive_fb", "filter", "open"):  _cascade_filter_open,     # rail dead downstream
    ("passive_fb", "filter", "short"): _cascade_passive_alive,   # shorted ferrite is a wire
}
```

### Cascade handler functions — the table of primitives

Each handler resolves the passive's connected rails / ICs via the graph,
then returns a cascade dict. Small set of reusable primitives:

```python
def _cascade_series_open(electrical, passive) -> dict:
    """R series or D series open → downstream rail dies + its consumers."""
    # Identify the DOWNSTREAM net (the one fed by the series element).
    downstream_rail = _find_downstream_rail(electrical, passive)
    if downstream_rail is None:
        return _empty_cascade()
    # Kill the rail + propagate through SimulationEngine as if the rail's
    # source was killed (reuse the primitive, just marking a different
    # source).
    return _simulate_rail_loss(electrical, downstream_rail)


def _cascade_feedback_open_overvolt(electrical, passive) -> dict:
    """R feedback open in a buck/boost divider → regulator output hits max.
    Modelled as `shorted_rails` (Phase 1 encoding for overvoltage)."""
    regulated_rail = _find_regulated_rail_of_feedback(electrical, passive)
    if regulated_rail is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["shorted_rails"] = frozenset({regulated_rail})
    # Downstream consumers are likely damaged (overvoltage) → anomalous.
    consumers = electrical.power_rails[regulated_rail].consumers or []
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_decoupling_open(electrical, passive) -> dict:
    """C decoupling open → upstream IC instability. Topologically weak."""
    upstream_ic = _find_decoupled_ic(electrical, passive)
    if upstream_ic is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset({upstream_ic})
    return c


def _cascade_decoupling_short(electrical, passive) -> dict:
    """C decoupling short → decoupled rail shorted to GND."""
    decoupled_rail = _find_decoupled_rail(electrical, passive)
    if decoupled_rail is None:
        return _empty_cascade()
    source = electrical.power_rails[decoupled_rail].source_refdes
    # Re-use the existing _simulate_shorted primitive from Phase 1.
    return _simulate_shorted_consumer_of_rail(
        electrical, decoupled_rail, source,
    )


def _cascade_filter_open(electrical, passive) -> dict:
    """FB filter open → downstream rail completely dead."""
    downstream_rail = _find_downstream_rail(electrical, passive)
    if downstream_rail is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream_rail)


def _cascade_passive_alive(electrical, passive) -> dict:
    """Default for modes that are physically plausible but produce no
    observable cascade (short on a wire, etc.). Empty → pruned."""
    return _empty_cascade()
```

The six handlers above are the **reusable primitives**. The remaining
~15 entries in `_PASSIVE_CASCADE_TABLE` (pull_up, pull_down, signal_path,
tank, flyback, rectifier, signal_to_ground, filter_cap, feedback_short)
follow the same shape — a short function that looks up the affected
rail / IC through the graph and returns a cascade dict assembled from
the three cascade atoms (`dead_comps`, `anomalous_comps`,
`shorted_rails`). The implementation plan enumerates them one per task.

Handlers resolve topology through small look-ups:
- `_find_downstream_rail(passive)` — inspect `passive.pins`, pick the pin
  whose net is the output side of the series element (heuristic:
  whichever pin's net has the most downstream consumers in `PowerRail`).
- `_find_decoupled_rail(passive)` — the non-GND pin's net if it's a rail,
  else the `decouples` edge's target.
- `_find_decoupled_ic(passive)` — `decouples` edge's target IC if emitted
  by vision, else the closest IC sharing the decoupled rail.
- `_find_regulated_rail_of_feedback(passive)` — walk the `feedback_in`
  edge backward to its regulator, then its `power_out` rail.

### Unmapped combinations

`_PASSIVE_CASCADE_TABLE.get((kind, role, mode))` returns `None` when the
triple is not in the table. The dispatcher in `_simulate_failure` logs a
debug line and returns an empty cascade — the candidate is then pruned
by `_relevant_to_observations`. Unknown roles (`null`) automatically
fall into this branch.

### `_applicable_modes` — updated

```python
def _applicable_modes(electrical, refdes) -> list[str]:
    comp = electrical.components.get(refdes)
    if comp is None:
        return []
    kind = getattr(comp, "kind", "ic")
    role = getattr(comp, "role", None)

    if kind == "ic":
        # Phase 1 behaviour unchanged.
        modes = ["dead", "hot"]
        if _has_outgoing_signal_edge(electrical, refdes):
            modes.append("anomalous")
        if _is_rail_consumer(electrical, refdes):
            modes.append("shorted")
        return modes

    # Passives — only enumerate modes for which the cascade table has an
    # entry with a non-alive handler. This keeps the candidate set tight
    # and skips `_cascade_passive_alive` entries.
    modes = []
    for mode in ("open", "short"):
        if role is None:
            continue
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is not None and handler is not _cascade_passive_alive:
            modes.append(mode)
    return modes
```

## Scoring — visibility multiplier for soft cascades

Some passive failure modes are physically real but topologically weak
(`(C, decoupling, open)`, `(R, pull_up, open)`, `(C, filter, open)`).
Their cascade only touches 1-2 anomalous components with no
corroborating rail/component observations. Without a dampener, these
cascades tie with much stronger hypotheses on the 1-observation case,
which bloats the top-3.

### Multiplier table

```python
# Partial TP-weight multipliers keyed by (kind, role, mode).
# Defaults to 1.0 when key is absent.
_SCORE_VISIBILITY: dict[tuple[str, str, str], float] = {
    ("passive_c", "decoupling", "open"): 0.5,
    ("passive_c", "bulk",       "open"): 0.5,
    ("passive_c", "filter",     "open"): 0.5,
    ("passive_r", "pull_up",    "open"): 0.5,
    ("passive_r", "pull_down",  "open"): 0.5,
    # damping / bypass entries intentionally omitted — their handler is
    # `_cascade_passive_alive`, so they're filtered out before scoring.
    # short cases are generally topologically visible (rail shorted) →
    # no multiplier needed.
}
```

### Applied in `_score_candidate`

The multiplier scales only the TP contribution of the primary candidate
(the passive whose cascade is being scored). FP/FN/contradiction weights
stay at 1.0 — false positives are still full-cost.

```python
score = float(tp_effective - fp_w * fp - fn_w * fn)
# tp_effective = tp_c * multiplier + tp_r  (multiplier applies to
# component-level TPs only, since the soft cascades we dampen surface
# component-level observations).
```

The multiplier is applied in the **single-fault** pass. In the two-fault
pass, the union cascade inherits the lower multiplier when either element
is a soft-visibility passive — simple `min()` aggregate.

Both `_PASSIVE_CASCADE_TABLE` and `_SCORE_VISIBILITY` are module-level
constants, exported so `tune_hypothesize_weights.py` can sweep them.

## Simulator cleanup — T0 (separate commit)

`hypothesize.py::_simulate_failure` lines 270–280 have a hack: after
running `SimulationEngine` for a shorted-consumer, it walks rails a
second time to mark any rail whose `source_refdes` is transitively dead
as dead too. Phase 4 is going to amplify this case (passives that
short rails → source dies → downstream rails orphaned), so we lift the
logic into `SimulationEngine` itself.

### Refactor

`api/pipeline/schematic/simulator.py::SimulationEngine.run()` currently
kills rails whose `source_refdes` is in the initial `killed_refdes`.
The new behaviour: after the first pass, iterate rails once more to
mark any rail whose source is now in `cascade_dead_components` but not
in `killed_refdes` as also dead. Repeat until fixpoint (bounded iteration,
since `dead_components` is monotonically increasing and finite).

### Removing the patch

Once `SimulationEngine` handles the transitive case, the 10 lines of
patch in `_simulate_failure("shorted")` collapse to:

```python
downstream = _simulate_dead(electrical, analyzed_boot, [source]) if source else _empty_cascade()
# all_dead_comps + transitive_dead_rails loops removed — simulator handles it
```

Commits as T0, independent of Phase 4 content, can roll back in isolation.
Test: `tests/pipeline/schematic/test_simulator.py` grows by one case
(dead source of rail A → rail A dead → rail B (sourced by a consumer of
rail A) dead by transitivity).

## HTTP surface

No endpoint shape change. The existing
`POST /pipeline/packs/{slug}/schematic/hypothesize` body still takes
`state_comps` / `state_rails` / `metrics_comps` / `metrics_rails`. The
only difference is that the validation of `state_comps[passive_refdes]`
now accepts `"open"` / `"short"` as valid modes — previously it would
have been rejected by the (now-extended) `ComponentMode` Literal.

`GET /pipeline/packs/{slug}/schematic` — the `ElectricalGraph` JSON response
grows by 2 fields per component (kind + role) and an additional ~1500
entries (the passives that previously had kind="ic" by default are now
correctly tagged). Consumers (frontend, agent tools) that iterate
`components` must filter by `kind` if they want ICs only.

`GET /pipeline/packs/{slug}/schematic/passives` — **new read-only
endpoint** returning the classifier's per-refdes assignments as a flat
list, for debugging and for the hand-written test fixture generation
scripts. Payload:

```json
[
  {"refdes": "C156", "kind": "passive_c", "role": "decoupling",
   "confidence": 0.9, "source": "heuristic"},
  {"refdes": "FB2",  "kind": "passive_fb", "role": "filter",
   "confidence": 0.85, "source": "heuristic"}
]
```

## Frontend

### Mode picker — contextual by kind

`web/js/schematic.js::updateInspector(node)` branches on
`node.data.kind` (new field exposed from `ComponentNode.kind`):

```javascript
const MODE_SETS = {
  ic:        ["unknown", "alive", "dead", "anomalous", "hot"],
  passive_r: ["unknown", "alive", "open", "short"],
  passive_c: ["unknown", "alive", "open", "short"],
  passive_d: ["unknown", "alive", "open", "short"],
  passive_fb:["unknown", "alive", "open", "short"],
  rail:      ["unknown", "alive", "dead", "shorted"],
};
```

CSS tokens for the new icons reuse the existing palette (amber for
anomalous, emerald for alive, the new `passive-*` states get the
cyan/violet mix already present for passives in the board colour
vocabulary):

- `open` → cyan outline + `⚪` glyph
- `short` → amber fill + `⚡` glyph (same as rail `shorted` — semantically
  coherent: both mean "current flowing where it shouldn't")

### Node rendering

The graph view already colour-codes by type (the OKLCH cyan-for-component
vs emerald-for-net rule). Passives keep that cyan but get a lighter
tint proportional to their `confidence` score from the classifier — a
passive with `confidence < 0.5` renders at 40 % opacity to cue the tech
that the role is tentative and worth verifying.

## Benchmark extension

### Auto-generated corpus

`scripts/gen_hypothesize_benchmarks.py` extends to sample `(passive_refdes,
mode)` in addition to the Phase 1 IC scenarios:

- Per `(passive_refdes, role, mode)` tuple with a non-`_cascade_passive_alive`
  handler, sample the cascade, pick 2–4 affected components/rails as
  `alive` observations around it to create a realistic fingerprint.
- Expected new volume on MNT: ~1200 passives × ~1.3 modes avg → ~1500
  scenarios. Total corpus grows from ~155 to ~1700 scenarios. Fixture
  file ~200 KB.

### Hand-written scenarios (new, critical)

`tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` — at
least **3 scenarios** encoding physical cases where the tech's
observation is consistent with the field corpus but MAY or MAY NOT
exactly align with the simulator's fantasy. These guard against the
auto-referential bias that Phase 1 corpus suffers from.

Initial three:

```yaml
- id: mnt-reform-c156-decoupling-short
  description: "+3V3 sagging at 0.8V + U7 (LPC) cold/dead, C156 shorted short-to-GND"
  observations:
    state_rails: {"+3V3": "shorted"}
    state_comps: {"U7": "dead"}
  ground_truth:
    kill_refdes: ["C156"]
    kill_modes:  ["short"]
  accept_in_top_n: 3

- id: mnt-reform-r43-feedback-open-overvolt
  description: "+5V measured at 7.2V (overvoltage), R43 feedback divider open"
  observations:
    state_rails: {"+5V": "shorted"}   # Phase 1 encoding for overvoltage
  ground_truth:
    kill_refdes: ["R43"]
    kill_modes:  ["open"]
  accept_in_top_n: 3

- id: mnt-reform-fb2-filter-open
  description: "LPC_VCC rail entirely dead, FB2 burned open"
  observations:
    state_rails: {"LPC_VCC": "dead"}
    state_comps: {"U7": "dead"}
  ground_truth:
    kill_refdes: ["FB2"]
    kill_modes:  ["open"]
  accept_in_top_n: 3
```

The scenario refdes (`C156`, `R43`, `FB2`) correspond to actual
components on the ingested MNT Reform board — if they don't exist in the
compiled graph, the scenario is SKIPPED with a warning (so CI doesn't
fail on a fresh ingest). The IDs picked here are candidates; task T13
confirms against the live `memory/mnt-reform-motherboard/electrical_graph.json`
and substitutes if needed.

### Per-mode CI gates

`tests/pipeline/schematic/test_hypothesize_accuracy.py` extends the
parametrize:

```python
@pytest.mark.parametrize("mode", [
    "dead", "anomalous", "hot", "shorted",   # Phase 1
    "open", "short",                          # Phase 4
])
def test_top1_accuracy_per_mode(mode):
    ...
```

Conservative starting thresholds:

| Mode  | top-1 | top-3 | MRR  |
|-------|-------|-------|------|
| dead  | 80 %  | 90 %  | 0.85 |
| anomalous | 50 % | 70 % | 0.65 |
| hot   | 70 %  | 90 %  | 0.80 |
| shorted | 55 % | 75 % | 0.65 |
| **open**  | **40 %** | **65 %** | **0.55** |
| **short** | **55 %** | **75 %** | **0.65** |

`open` is lower because of the soft-cascade multiplier — hypotheses for
`(C, decoupling, open)` legitimately tie with anomalous-IC hypotheses.
`short` matches `shorted` because both rely on rail-level evidence.

### Hand-written scenario gate

A separate CI test `test_hand_written_scenarios_accept_ground_truth`
iterates the YAML file and asserts each `ground_truth` appears in the
`top_n` hypotheses. No aggregate threshold — per-scenario pass/fail.

### Weight tuning

`scripts/tune_hypothesize_weights.py` reads both corpuses
(auto + hand-written), sweeps the PENALTY_WEIGHTS grid AND optionally
the `_SCORE_VISIBILITY` multipliers (with a coarser grid — 3 steps
per entry so the search stays tractable). Commits tuned values only
if hand-written scenarios still pass AND aggregate weighted top-3
improves.

## Applicability explosion — sanity check

Phase 1 budget: ~600 sims on MNT Reform (449 IC × ~1.3 modes).
Phase 4 adds passives:

| Kind | Count (MNT est.) | Avg applicable modes | Sims added |
|---|---|---|---|
| passive_r | ~800 | 1.5 (most series/feedback/pull_up) | ~1200 |
| passive_c | ~1000 | 1.8 (decoupling open+short + filter) | ~1800 |
| passive_d | ~40 | 1.2 | ~50 |
| passive_fb | ~20 | 1 (filter only, open+short) | ~20 |

Total ~3100 additional sims. At 0.5 ms each → ~1.5 s uncapped.

Mitigation:
1. **Cascade-intersection pruning still applies.** Most passive cascades
   touch exactly one rail/component; if that target isn't in the
   observations, the candidate is dropped pre-scoring.
2. **Classifier coverage.** In practice ~30 % of passives will have
   `role = None` after the heuristic pass (fallback cases). Those get
   zero applicable modes → excluded from the candidate set entirely.
   Expected effective passive pool: ~1200 passives × 1.5 modes = ~1800.
3. **Applicability gate on `_cascade_passive_alive` entries.** Modes
   that map to the alive handler are skipped — ~30 % reduction.

Expected end-state: ~600 IC + ~1200 passive = ~1800 sims / hypothesize
call → ~900 ms p95 (1.8x the Phase 1 budget). **If the run breaches
the 1.5 s budget**, enable the existing `MAX_PAIRS` cap on the 2-fault
pass (currently 100; drop to 50 for passive pairs) — the 1-fault
pass is untouched because that's where the hand-written scenarios
score.

The CI perf gate is raised from 500 ms (Phase 1) to **1500 ms p95**. The
bench script reports per-mode p95 so regressions localize to the mode
class.

## Files impacted

| File | Action | Est. delta |
|---|---|---|
| `api/pipeline/schematic/schemas.py` | modify — `ComponentKind`, `ComponentNode` fields | +25 LOC |
| `api/pipeline/schematic/passive_classifier.py` | **create** — heuristic + optional Opus | ~300 LOC |
| `api/pipeline/schematic/compiler.py` | modify — invoke classifier, wire `role` onto components | +60 LOC |
| `api/pipeline/schematic/orchestrator.py` | modify — pass AsyncAnthropic client through to classifier | +20 LOC |
| `api/pipeline/schematic/simulator.py` | modify — T0 transitive-rails fixpoint | +30 LOC |
| `api/pipeline/schematic/hypothesize.py` | modify — `_PASSIVE_CASCADE_TABLE`, handlers, `_applicable_modes` update, mode vocab, scoring multiplier, obs validator | +350 LOC |
| `api/pipeline/schematic/cli.py` | modify — surface `--classify-passives` switch for re-runs | +15 LOC |
| `api/pipeline/__init__.py` | modify — `GET /schematic/passives` read-only endpoint | +30 LOC |
| `web/js/schematic.js` | modify — `MODE_SETS`, picker update, confidence-tinted rendering | +80 LOC |
| `web/styles/schematic.css` | modify — `.sim-mode-picker[data-kind=passive_*]` tokens | +30 LOC |
| `tests/pipeline/schematic/test_passive_classifier.py` | **create** — heuristic rules, LLM fallback | ~180 LOC |
| `tests/pipeline/schematic/test_hypothesize.py` | modify — passive cases, coherence validator | +120 LOC |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify — per-mode gates open/short | +50 LOC |
| `tests/pipeline/schematic/test_hand_written_scenarios.py` | **create** — loads YAML, runs engine, asserts ground truth in top-N | ~80 LOC |
| `tests/pipeline/schematic/test_simulator.py` | modify — T0 transitive-rails test | +40 LOC |
| `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` | **create** — initial 3 scenarios | ~60 lines |
| `tests/pipeline/schematic/fixtures/hypothesize_scenarios.json` | regenerate | grows to ~200 KB |
| `tests/pipeline/test_schematic_api.py` | modify — passive endpoint smoke | +30 LOC |
| `scripts/gen_hypothesize_benchmarks.py` | modify — passive sampling | +80 LOC |
| `scripts/bench_hypothesize.py` | modify — per-mode p95 (open/short) | +20 LOC |
| `scripts/tune_hypothesize_weights.py` | modify — sweep `_SCORE_VISIBILITY` too | +40 LOC |

Grand total: ~1600 LOC new/changed + ~500 LOC test/fixture. Realistic
range after reviewer adjustment: **~2000 LOC**, ~18–20 tasks on the
implementation plan.

## Rollout — task groupings

Five groups, each ending in a strict commit gate:

| Group | Tasks | Goal |
|---|---|---|
| **T0** | 1 task (isolation) | `SimulationEngine` transitive-rails fixpoint refactor. Ships on its own commit. |
| **A — Shape + classifier** | 4 tasks | `ComponentKind`, extended `ComponentNode`, `passive_classifier.py` (heuristic only), compiler integration. |
| **B — Cascade dispatch** | 5 tasks | Mode vocab extension, coherence validator, `_PASSIVE_CASCADE_TABLE` + handlers, `_applicable_modes` update, scoring multiplier. |
| **C — Corpus + CI** | 4 tasks | Hand-written YAML + loader test, extend auto-gen script, per-mode CI gates, tune weights. |
| **D — Frontend** | 3 tasks | Kind-aware picker, confidence-tinted rendering, smoke-test with Alexis in browser (BROWSER-VERIFY gate before commit). |
| **E — LLM enrichment (optional)** | 1 task | Opus post-pass for passive classifier, merged with heuristic. Shipped last so it's not on the critical path. |

Each task lands in a single focused commit. `make test` passes after
each. Group D needs browser-verify with Alexis before commit per the
feedback memory.

## Open questions — resolved during brainstorm

- **Scope** — R + C + FB + D locked; Q deferred to Phase 4.5.
- **Schema migration** — extend `ComponentNode` in place, defaults keep
  Phase 1 data compatible; no new top-level field.
- **Mode vocab** — unified `ComponentMode = Literal[..., "open", "short"]`
  with coherence validator applied at `hypothesize()` entry.
- **Cascade dispatch** — explicit `_PASSIVE_CASCADE_TABLE` (grep-friendly)
  over clever reuse. Handlers call existing primitives; no combinatorial
  explosion.
- **Transitive rails cleanup** — moved upstream into `SimulationEngine`
  as task T0, independent commit.
- **Naming conflict** — `api/pipeline/schematic/net_classifier.py`
  already exists and classifies nets by functional domain; Phase 4
  cannot extend it. New module `passive_classifier.py` instead.

## Backlog (out of scope of this phase)

- **Q (transistor) cascade handlers.** Phase 4.5. Requires a fifth
  passive kind plus richer mode vocabulary (B-E short, C-E short, gain
  drift). ~400 LOC.
- **Numeric proximity scoring.** Phase 5. A measured value that deviates
  N % from nominal tie-breaks between candidates with equal discrete
  scores.
- **Per-role thresholds in auto-classify of measurements.** Currently
  rail auto-classify is role-blind; a 2 V measurement on a 3.3 V rail
  is classified `anomalous` regardless of whether the rail is decoupled
  or raw. A richer auto-classify would weight by role — deferred.
- **ESR / leakage / partial-short analog modes.** Requires a SPICE-lite
  simulator path. Different product entirely.
- **Per-scenario explanation UI.** When the engine surfaces a soft
  hypothesis (`(C, decoupling, open)` with 0.5 TP), the frontend should
  tell the tech *why* the score is low (« cascade topologique faible,
  vérifier physiquement »). Nice-to-have, not in scope.
