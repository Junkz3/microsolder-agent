# Discrete Transistor (Q) Injection — Phase 4.5 Design

## Context

Phase 4 and 4.1 shipped passive-component injection — the reverse-diagnostic
engine now handles R/C/D/FB failure modes and achieves ~97% passive
coverage on MNT Reform via heuristic + Opus classification. Discrete
transistors (Q, typically MOSFETs for load switches and level shifters,
BJTs for level shifting and biasing) are **still invisible to the
engine** — their `ComponentType == "transistor"` doesn't map to any
`ComponentKind` and `compile_electrical_graph` leaves them with the
default `kind="ic"`.

On a typical embedded board (MNT Reform, Framework, Pi-class), discrete
Q represent ~5-10% of active components but account for a disproportionate
share of repair-worthy failures, particularly:

- **Load switch D-S short** → rail stuck-on when it should be off (classic
  standby-current complaint).
- **Level shifter stuck** → bus stuck at one logic level, peripheral
  unresponsive.
- **Inrush limiter open** → main rail never powers up.

None of these map cleanly to existing `{dead, alive, anomalous, hot,
shorted}` IC vocabulary or to `{open, short}` passive vocabulary.

Phase 4.5 adds Q as a first-class kind with richer failure modes and a
new cascade semantic for "rail permanently on".

## Goal

Ship one drop that:

1. Adds `passive_q` to `ComponentKind` (the reserved slot) and plugs the
   transistor type into the classifier pipeline.
2. Extends `ComponentMode` with `stuck_on` and `stuck_off` — Q-specific
   modes. Passive modes `open` and `short` also apply to Q when
   physically meaningful.
3. Extends `RailMode` with `stuck_on` — the tech's observation vocabulary
   for "rail alive when it should be off" (typical standby-current
   complaint).
4. Adds `always_on_rails: frozenset[str]` to the cascade dict — new
   bucket for rails a candidate failure causes to be permanently on.
   Scoring matches observed `rail.stuck_on` against predicted
   `always_on_rails`, disjoint from `shorted_rails` (which keeps its
   Phase 1 semantics of "rail at 0V or overvolt").
5. Ships `_classify_transistor()` in `passive_classifier.py` — heuristic
   rules identifying `load_switch`, `level_shifter`, `inrush_limiter`.
   Unknown topology falls through to `role=None` (same pattern as other
   passives); the Opus pass fills the holes.
6. Extends `_PASSIVE_CASCADE_TABLE` with ~15 `(passive_q, role, mode)`
   entries covering the 3 roles × 4 modes (filtered by applicability).
7. Extends the frontend picker: passive_q nodes show
   `[unknown, alive, open, short, stuck_on, stuck_off]`; rails gain
   `[..., stuck_on]` (in addition to existing `shorted`).
8. Extends auto-classify rules in `measurement_memory.py`: a rail
   measurement at ~nominal voltage with a `note` mentioning standby/off
   state classifies to `stuck_on`.
9. Adds hand-written scenarios covering the 3 role families to the
   anti-auto-referential YAML corpus.

## Non-goals

- **Gate-to-source short semantics** (G-S short). Physically meaningful
  for MOSFET diag but the cascade effect depends heavily on gate-drive
  topology (pull-up vs pull-down, EN signal polarity). Modelled as
  `stuck_off` when gate pull-down is present, `stuck_on` when pull-up.
  No separate `g_s_short` mode — scope creep.
- **Gain drift / leakage** (analog modes). Requires numeric scoring
  (Phase 5 SPICE-lite territory).
- **BJT-specific modes** (B-E short vs C-E short). Mapped to the same
  `open/short/stuck_on/stuck_off` vocabulary — the `role` disambiguates.
- **Bias current mirror / Q-pair topologies**. Out of scope (<5% of
  repair cases, mostly analog audio).
- **Flyback switch in discrete SMPS**. Rare on modern boards (integrated
  switchers dominate). Defer to a Phase 4.5.1 if field data justifies.
- **Auto-detection of gate-drive polarity** from schematic extraction.
  Heuristic uses presence/absence of a pull-up or pull-down R on the
  gate net; when ambiguous, `role=None` and the Opus pass decides.

## Architecture

Same 5-concern structure as Phase 4, minimal incremental changes:

1. **Shape extension** — `schemas.py`. `ComponentKind` gains `passive_q`,
   `ComponentMode` gains `stuck_on`/`stuck_off`, `RailMode` gains
   `stuck_on`. Additive.
2. **Classifier** — `passive_classifier.py`. Fills existing
   `_classify_transistor` stub, extends `_TYPE_TO_KIND` with
   `"transistor": "passive_q"`. Opus prompt gains the 3 Q roles.
3. **Compiler integration** — none required. `compile_electrical_graph`
   already iterates every component; the classifier now emits entries
   for `type=="transistor"` automatically.
4. **Cascade dispatcher** — `hypothesize.py`. New bucket
   `always_on_rails` propagated through `_empty_cascade`,
   `_simulate_failure` passive branch, `_score_candidate`. New
   `_applicable_modes` branch for Q. New `_PASSIVE_CASCADE_TABLE`
   entries. Updated `_validate_obs_against_graph`.
5. **Frontend** — `web/js/schematic.js` + `schematic.css`.
   `MODE_SETS.passive_q` + rail picker updated. Glyph for `stuck_on` ≠
   `shorted`.

## Data shapes

### `schemas.py`

```python
ComponentKind = Literal[
    "ic",
    "passive_r",
    "passive_c",
    "passive_d",
    "passive_fb",
    "passive_q",   # NEW
]
```

No other schema changes — `ComponentNode.role` is already `str | None`
and accepts new role strings without migration.

### `hypothesize.py`

```python
ComponentMode = Literal[
    "dead", "alive", "anomalous", "hot",
    "open", "short",
    "stuck_on", "stuck_off",   # NEW — Q-specific
]

RailMode = Literal[
    "dead", "alive",
    "shorted",       # to GND OR overvolt (Phase 1 semantics)
    "stuck_on",      # NEW — rail alive when it should be off
]

FailureMode = Literal[
    "dead", "anomalous", "hot", "shorted",
    "open", "short",
    "stuck_on", "stuck_off",   # NEW
]

_IC_MODES: frozenset[str] = frozenset({"dead", "alive", "anomalous", "hot"})
_PASSIVE_MODES: frozenset[str] = frozenset(
    {"open", "short", "alive", "stuck_on", "stuck_off"}
)
# stuck_on/stuck_off live in _PASSIVE_MODES — they apply to Q (a passive
# kind). The heuristic in _applicable_modes further narrows by kind:
# Q gets {open, short, stuck_on, stuck_off}; R/C/D/FB still get {open, short}.
```

`_empty_cascade()` gains a key:

```python
def _empty_cascade() -> dict:
    return {
        "dead_comps": frozenset(),
        "dead_rails": frozenset(),
        "shorted_rails": frozenset(),
        "always_on_rails": frozenset(),   # NEW
        "anomalous_comps": frozenset(),
        "hot_comps": frozenset(),
        "final_verdict": "",
        "blocked_at_phase": None,
    }
```

### Scoring

`_score_candidate` needs one new matcher for `state_rails[rail] == "stuck_on"`
against `predicted_rails[rail] = "stuck_on"` (sourced from `always_on_rails`).

```python
predicted_rails: dict[str, str] = {}
for rail in cascade["dead_rails"]:
    predicted_rails[rail] = "dead"
for rail in cascade["shorted_rails"]:
    predicted_rails[rail] = "shorted"
for rail in cascade["always_on_rails"]:
    predicted_rails[rail] = "stuck_on"   # NEW — disjoint from shorted
# shorted and stuck_on can't both apply to same rail by construction.
```

`_relevant_to_observations` check grows the "any rail" side:

```python
any_rail = (
    cascade["dead_rails"] | cascade["shorted_rails"] | cascade["always_on_rails"]
)
```

### Visibility multiplier

`_SCORE_VISIBILITY` gets no new entries by default. Q cascades are
topologically strong (a load_switch short observable via its downstream
rail directly). No dampening needed.

## Q role classifier (heuristic)

`_classify_transistor(graph, comp) -> (role, confidence)`:

### Rules in order

**1. `load_switch` (high confidence, most common field case)**

Signature: Q has 3+ pins where:
- One pin connects to a rail that has a `source_refdes` (power_rails map)
- Another pin connects to a DIFFERENT rail (downstream, often
  `source_refdes=None` or source=this Q)
- A third pin (gate) connects to a net that is NOT a rail and whose
  label contains `EN`, `_PWR_EN`, `POWER`, or the net is on a typed
  edge `kind=enables` as the destination

Role : `load_switch`, confidence 0.75.

**2. `level_shifter`**

Signature: Q has 3 pins where:
- Two pins are on non-rail signal nets (different net labels)
- The third pin (gate) connects to a rail OR to another control signal
- The two signal nets are on different voltage domains (inferred from
  any net's label — `*_3V3_*`, `*_1V8_*`, `*_1V2_*`)

Role : `level_shifter`, confidence 0.65.

**3. `inrush_limiter`**

Signature: Q in series with a power input path:
- One pin on VIN / BAT / +12V (high-voltage input)
- Another pin on a rail that has a consumer IC with `power_in` pin
- Gate connects to an RC delay network (capacitor + resistor to GND) or
  to a `soft_start_en` typed net

Role : `inrush_limiter`, confidence 0.6.

**4. Fallback**

Role : `None`, confidence 0.0. The Opus pass fills in.

### Classifier map update

```python
_TYPE_TO_KIND: dict[str, str] = {
    "resistor":   "passive_r",
    "capacitor":  "passive_c",
    "diode":      "passive_d",
    "ferrite":    "passive_fb",
    "transistor": "passive_q",   # NEW
}
```

### Opus prompt extension

Add to `_SYSTEM_PROMPT` in `passive_classifier.py`:

```
  passive_q (transistors — discrete MOSFET / BJT):
    - load_switch     — high-side gating of a rail (source = upstream rail,
                         drain = downstream rail, gate = EN / _PWR_EN signal).
                         Most common Q on embedded boards.
    - level_shifter   — Q between two signal nets in different logic voltage
                         domains (3V3 ↔ 1V8, 1V8 ↔ 1V2). Typical on I2C bridges.
    - inrush_limiter  — Q in series with a power input, gate controlled by
                         an RC delay for soft-start. Classic on laptop VIN paths.
```

## Cascade dispatch (Q-specific)

`_PASSIVE_CASCADE_TABLE` grows by ~15 entries. Non-`_cascade_passive_alive`
rows:

```python
# ========================= TRANSISTORS ===========================

("passive_q", "load_switch",    "open"):     _cascade_q_load_dead,
("passive_q", "load_switch",    "short"):    _cascade_q_load_stuck_on,
("passive_q", "load_switch",    "stuck_on"): _cascade_q_load_stuck_on,
("passive_q", "load_switch",    "stuck_off"):_cascade_q_load_dead,

("passive_q", "level_shifter",  "open"):     _cascade_q_shifter_signal_broken,
("passive_q", "level_shifter",  "short"):    _cascade_q_shifter_signal_stuck,
("passive_q", "level_shifter",  "stuck_on"): _cascade_q_shifter_signal_stuck,
("passive_q", "level_shifter",  "stuck_off"):_cascade_q_shifter_signal_broken,

("passive_q", "inrush_limiter", "open"):     _cascade_q_inrush_rail_dead,
("passive_q", "inrush_limiter", "short"):    _cascade_passive_alive,
("passive_q", "inrush_limiter", "stuck_on"): _cascade_passive_alive,
("passive_q", "inrush_limiter", "stuck_off"):_cascade_q_inrush_rail_dead,
```

### Handlers

```python
def _cascade_q_load_dead(electrical, q) -> dict:
    """Load switch open/stuck_off → downstream rail dead + consumers dead."""
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    return _simulate_rail_loss(electrical, downstream)


def _cascade_q_load_stuck_on(electrical, q) -> dict:
    """Load switch short/stuck_on → downstream rail permanently on.
    Consumers become anomalous (active when they should be off).
    """
    downstream = _find_downstream_rail(electrical, q)
    if downstream is None:
        return _empty_cascade()
    c = _empty_cascade()
    c["always_on_rails"] = frozenset({downstream})
    consumers = electrical.power_rails[downstream].consumers or []
    c["anomalous_comps"] = frozenset(consumers)
    return c


def _cascade_q_shifter_signal_broken(electrical, q) -> dict:
    """Level shifter open/stuck_off → signal not propagating → consumers anomalous."""
    nets = [p.net_label for p in q.pins if p.net_label]
    sig_nets = [n for n in nets if n not in electrical.power_rails and not _is_ground_net(n)]
    anomalous: set[str] = set()
    for edge in electrical.typed_edges:
        if edge.kind in {"consumes_signal", "depends_on"} and edge.dst in sig_nets:
            if edge.src in electrical.components:
                anomalous.add(edge.src)
    c = _empty_cascade()
    c["anomalous_comps"] = frozenset(anomalous)
    return c


def _cascade_q_shifter_signal_stuck(electrical, q) -> dict:
    """Level shifter short/stuck_on → signal stuck at one rail level →
    consumers anomalous (same shape as broken, distinction is in mode semantics
    not cascade topology). Keeps both entries for clarity + future divergence."""
    return _cascade_q_shifter_signal_broken(electrical, q)


def _cascade_q_inrush_rail_dead(electrical, q) -> dict:
    """Inrush limiter open/stuck_off → downstream regulator never powers up →
    rail downstream dead."""
    return _cascade_q_load_dead(electrical, q)
```

## `_applicable_modes` extension

```python
def _applicable_modes(electrical, refdes) -> list[str]:
    comp = electrical.components.get(refdes)
    if comp is None:
        return []
    kind = getattr(comp, "kind", "ic")
    role = getattr(comp, "role", None)

    if kind == "ic":
        # Phase 1 branch — unchanged.
        ...

    # Passive. R/C/D/FB have {open, short}. Q has {open, short, stuck_on, stuck_off}.
    if role is None:
        return []
    if kind == "passive_q":
        candidate_modes = ("open", "short", "stuck_on", "stuck_off")
    else:
        candidate_modes = ("open", "short")
    applicable: list[str] = []
    for mode in candidate_modes:
        handler = _PASSIVE_CASCADE_TABLE.get((kind, role, mode))
        if handler is not None and handler is not _cascade_passive_alive:
            applicable.append(mode)
    return applicable
```

## Observation validator update

`_validate_obs_against_graph` needs no logic change — `_PASSIVE_MODES`
already includes `stuck_on`/`stuck_off`. Rails gain `stuck_on` in the
RailMode literal, already Pydantic-validated.

## HTTP surface

Unchanged. The existing `POST /schematic/hypothesize` body accepts new
mode values through the Literal extension. `GET /schematic/passives`
endpoint (Phase 4) now returns Q entries alongside R/C/D/FB.

## Frontend

### `MODE_SETS` extended

```javascript
const MODE_SETS = {
  ic:         ["unknown", "alive", "dead", "anomalous", "hot"],
  passive_r:  ["unknown", "alive", "open", "short"],
  passive_c:  ["unknown", "alive", "open", "short"],
  passive_d:  ["unknown", "alive", "open", "short"],
  passive_fb: ["unknown", "alive", "open", "short"],
  passive_q:  ["unknown", "alive", "open", "short", "stuck_on", "stuck_off"],
  rail:       ["unknown", "alive", "dead", "shorted", "stuck_on"],
};

const MODE_GLYPH = {
  // ... existing ...
  stuck_on:  "🔒",   // distinct from shorted (⚡)
  stuck_off: "🚫",
};
```

### CSS

```css
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_on"],
.sim-mode-picker[data-kind="rail"] button[data-mode="stuck_on"] {
  color: var(--violet);
  border-color: color-mix(in oklch, var(--violet) 40%, transparent);
}
.sim-mode-picker[data-kind="passive_q"] button[data-mode="stuck_off"] {
  color: var(--text-3);
  border-color: color-mix(in oklch, var(--text-3) 40%, transparent);
}
```

## Auto-classify (measurement_memory.py)

Rail auto-classify for numeric measurements currently has rules mapping
voltage to `dead`/`alive`/`anomalous`/`shorted`. Add:

```python
# Rule: rail voltage ≈ nominal BUT tech notes indicate standby / off state
# → stuck_on
if abs(measured - nominal) / nominal < 0.1:
    if note and any(k in note.lower() for k in ("veille", "standby", "off",
                                                 "power_off", "sleep")):
        return "stuck_on"
```

Same pattern as the existing `note="short"` → `shorted` promotion. Keeps
the auto-classify table centralized and tunable.

## Hand-written scenarios (extension)

Append to `hand_written_scenarios.yaml`:

```yaml
- id: mnt-reform-q-load-switch-stuck-on
  description: |
    The board consumes 500mA in standby even with the lid closed.
    +3V3_USB measures 3.3V when EN=low (should be 0V). A load-switch
    MOSFET downstream of this rail has D-S shorted permanently.
  device_slug: mnt-reform-motherboard
  observations:
    state_rails: { "+3V3_USB": "stuck_on" }
  ground_truth_match:
    kind: passive_q
    role: load_switch
    expected_mode: short
  accept_in_top_n: 5

- id: mnt-reform-q-inrush-open
  description: |
    Main VIN reaches the board but no rail ever powers up on cold boot.
    Inrush-limiter MOSFET open (burned from excessive cold-inrush).
  device_slug: mnt-reform-motherboard
  observations:
    state_rails: { "VIN_BUCK": "dead" }
  ground_truth_match:
    kind: passive_q
    role: inrush_limiter
    expected_mode: open
  accept_in_top_n: 10
```

## CI gate calibration

The per-mode CI gate in `test_hypothesize_accuracy.py` grows to cover
`stuck_on` and `stuck_off`:

```python
"stuck_on":  {"top1": 0.00, "top3": 0.25, "mrr": 0.13},
"stuck_off": {"top1": 0.00, "top3": 0.20, "mrr": 0.12},
```

Same conservative approach as Phase 4 open/short — zero top-1 expected
(ICs dominate auto-corpus scoring), focus on top-3. Hand-written
scenarios remain the real gate.

Corpus regeneration: `gen_hypothesize_benchmarks.py` iterates
`_applicable_modes` uniformly, so Q scenarios appear automatically
post-regen. Expected ~60 new scenarios (30 stuck_on + 30 stuck_off) on
MNT Reform if it has ≥30 classified Q components.

## Files impacted

| File | Action | Est. delta |
|---|---|---|
| `api/pipeline/schematic/schemas.py` | modify — ComponentKind gains passive_q | +5 LOC |
| `api/pipeline/schematic/passive_classifier.py` | modify — `_classify_transistor`, `_TYPE_TO_KIND`, Opus prompt | +80 LOC |
| `api/pipeline/schematic/hypothesize.py` | modify — modes, always_on_rails bucket, scoring, _applicable_modes, 3 new handlers, 12 table entries | +150 LOC |
| `api/agent/measurement_memory.py` | modify — stuck_on auto-classify rule | +15 LOC |
| `api/agent/manifest.py` | modify — mb_hypothesize tool enum extended with stuck_on/stuck_off | +10 LOC |
| `web/js/schematic.js` | modify — MODE_SETS.passive_q + rail stuck_on, MODE_GLYPH | +15 LOC |
| `web/styles/schematic.css` | modify — stuck_on / stuck_off button CSS | +15 LOC |
| `tests/pipeline/schematic/test_schemas.py` | modify — passive_q test | +10 LOC |
| `tests/pipeline/schematic/test_passive_classifier.py` | modify — transistor role tests | +50 LOC |
| `tests/pipeline/schematic/test_hypothesize.py` | modify — Q cascade handler tests, always_on_rails tests | +120 LOC |
| `tests/pipeline/schematic/test_hypothesize_accuracy.py` | modify — stuck_on/stuck_off gates | +10 LOC |
| `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` | modify — 2 new Q scenarios | +20 lines |

Grand total: ~480 LOC new/changed + ~190 LOC tests. Realistic ~600 LOC.
10-12 tasks on the implementation plan.

## Rollout

Four groups. Same discipline as Phase 4.

| Group | Tasks | Goal |
|---|---|---|
| **A — Shape + classifier** | 3 tasks | ComponentKind passive_q, `_classify_transistor` heuristic, Opus prompt extension. |
| **B — Cascade dispatch** | 3 tasks | `always_on_rails` bucket, RailMode stuck_on, `ComponentMode` stuck_on/stuck_off, scoring update, `_applicable_modes` branch, 3 new handlers + table entries. |
| **C — Frontend + auto-classify** | 2 tasks | `MODE_SETS.passive_q` + rail stuck_on, CSS, measurement auto-classify rule. Browser-verify before commit. |
| **D — Corpus + CI + verify** | 2 tasks | Hand-written scenarios, corpus regen via classify_passives on MNT, per-mode gates, accuracy pass. |

Each group lands in a single focused commit (or small stack when the
edit spans tightly-coupled files). Tests pass at every commit.

## Open questions — resolved during brainstorm

- **Mode vocabulary** — Q gets distinct `stuck_on`/`stuck_off` (richer
  than open/short), not collapsed into Phase 4 pair.
- **Cascade for stuck_on** — new `always_on_rails` bucket + new
  `RailMode="stuck_on"`. Not overloaded onto `shorted_rails` (would
  muddle physically-opposite diagnostic cases).
- **Role scope** — `load_switch` + `level_shifter` + `inrush_limiter`
  (~95% of field cases). Flyback switch + bias mirror deferred.

## Phased follow-ups (out of this spec)

- **Phase 4.5.1 — flyback_switch + bias_current_mirror roles**. Needed
  only if field reports flag discrete SMPS or analog audio cases.
  ~200 LOC.
- **Phase 4.6 — gate drive topology inference**. Use typed edges to
  detect whether a Q's gate has a pull-up or pull-down and refine the
  `stuck_on` vs `stuck_off` default. Currently heuristic guesses
  conservatively.
- **Phase 5 — numeric scoring**. Q failures often manifest as soft
  symptoms (slight voltage deviation, thermal rise under load).
  Proximity scoring would help here.
