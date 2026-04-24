# BMS Q Roles — Phase 4.6 Design

## Context

Phase 4.5 and 4.5.1 shipped four canonical Q roles (`load_switch`,
`level_shifter`, `inrush_limiter`, `flyback_switch`) covering the active
Qs in the main power path and on signal buses. On MNT Reform that's
5/14 Q classified — Q1/Q2/Q15/Q17 as `flyback_switch` (the buck pair
Qs) and Q3 as `load_switch` (VIN → PVIN gate). The remaining 9 Qs
(Q4-Q12) stay `role=None` because no existing rule matches their
topology.

On inspection, Q5-Q12 are clearly BMS-side Qs: their pins sit on the
`BAT1..BAT8` cell taps and the `BAT1FUSED` fused-cell output. MNT's
battery sub-system uses an LTC4121 charger/monitor plus per-cell
passive balance networks and one series protection FET on the fused
output. Two distinct topologies:

- **Q5** — pins on `{BAT1, BAT1FUSED}` (two distinct BAT-family rails).
  This is a classic **cell_protection** pattern: a series MOSFET that
  disconnects the cell (or pack) output on fault. One per protected
  output.
- **Q6-Q12** — pins labelled `BAT2..BAT8` with S and D on the *same*
  net label (seven of them, one per remaining cell). This is the
  vision-pass artefact of a **cell_balancer** topology: a MOSFET in
  series with a bleed resistor between the cell's high tap and a lower
  reference, with the pdfplumber/vision extractor merging the two sides
  of the resistor into one net label. The role of the FET is to drain
  excess charge from the highest cell so the pack balances passively.

Q4 (S=GND, D unlabelled) doesn't match any BMS topology and stays
unclassified — out of scope.

Phase 4.6 adds these two roles so MNT reaches 13/14 classified Qs, and
wires one real cascade (`cell_protection` open/stuck_off → downstream
BAT rail dead) plus `cell_balancer` as an alive-only classification
(cell-level drift isn't observable from rail-level state without BMS
telemetry, which the workbench doesn't have).

## Goal

Ship one drop that:

1. Adds `cell_protection` and `cell_balancer` to `passive_q.role`
   vocabulary.
2. Extends `_classify_transistor` with two new heuristic rules, placed
   **before** `inrush_limiter` so `BAT`-named rails don't mis-match the
   inrush rule (which fires on any `VIN`/`BAT` name).
3. Adds one cascade handler (`_cascade_q_cell_protection_dead`) and
   one helper (`_find_cell_protection_downstream`) that picks the
   downstream rail based on net-name asymmetry (fused/prot/out
   suffix).
4. Wires 8 new dispatch-table entries (2 roles × 4 modes).
5. Updates the Opus passive-classifier prompt and the agent diagnostic
   prompt with the two new roles' failure semantics (French UI copy,
   English identifiers).
6. Adds two hand-written scenarios on MNT Reform validating the
   cascade end-to-end (`cell_protection open` and `cell_protection
   stuck_off` against `BAT1FUSED: dead`).

**Out of scope:** synthetic corpus regen (MNT has only one
cell_protection Q, too few samples to move a gate). Q4's
classification (topology doesn't fit either role).

## Heuristic — `_classify_transistor` additions

A new module-level regex, used by both rules:

```python
_BAT_FAMILY_PATTERN = re.compile(
    r"^(?:BAT|VBAT|CHGBAT|BATTERY|CELL)\d*(?:FUSED|PROT|RAW|OUT|PACK|CHG|IN)?$"
)
```

Matches: `BAT`, `BAT1..BAT99`, `BAT1FUSED`, `BATPACK`, `CHGBAT`,
`VBAT`, `CELL1`, `CELL1PROT`, etc. Rejects: `CR1220` (coin-cell
naming), foreign bus names, arbitrary strings.

Two new rules, inserted immediately **after Rule 0 (flyback_switch)
and before the `if len(nets) < 3: return None` 3-pin guard**. The
guard must not block them: Q6-Q12 expose only two labelled nets
(`[None, BAT2, BAT2]` → two entries after `_pin_nets` drops None),
and the vision-merged cell_balancer topology would fall through to
`None` if gated behind the guard. The priority also matters for
correctness: the existing Rule 1 (inrush_limiter) fires on any
`VIN`- or `BAT`-substring in the rail name and would grab Q5
(`BAT1FUSED` rail) before BMS roles get a chance.

Both rules compute their own local helpers (`gnd_nets`,
`unique_nets`, `bat_nets`) because the shared
`rail_nets`/`gnd_nets`/`nonrail_nonGND` block lives further down in
`_classify_transistor`, past the 3-pin guard.

**Rule 0.5 — `cell_protection`.** Pin-net set (deduplicated) contains
two or more distinct BAT-family labels, and no pin is on a ground
net. Fires with confidence 0.75.

```python
unique_nets = set(nets)
bat_nets = {n for n in unique_nets if _BAT_FAMILY_PATTERN.match(n)}
gnd_here = any(_is_ground_net(n) for n in nets)
if len(bat_nets) >= 2 and not gnd_here:
    return "cell_protection", 0.75
```

**Rule 0.6 — `cell_balancer`.** Exactly one distinct BAT-family label
in the pin-net set, appearing on two or more pins (the vision
double-labelling artefact), and no non-BAT, non-None net. Fires with
confidence 0.65 (lower because the evidence is shape-by-exclusion
rather than positive).

```python
if len(bat_nets) == 1:
    the_bat = next(iter(bat_nets))
    if nets.count(the_bat) >= 2:
        foreign = [n for n in unique_nets if n != the_bat]
        if not foreign:
            return "cell_balancer", 0.65
```

Both rules are written so a Q with mixed BAT and non-BAT nets
(e.g. `BAT_PACK` + `VIN` + `EN_PACK`) falls through to the generic
rules below — conservative, avoids poisoning the charger-path Qs on
other boards.

## Cascade — `cell_protection` open / stuck_off

`cell_protection` is a series FET. Open-channel semantics: the
downstream rail (the "protected" side) goes dead; consumers of that
rail lose power. Short-channel semantics: the FET conducts even when
fault conditions should have opened it, but from the tech's probe
perspective nothing on a rail is visibly different — we model it as
`_cascade_passive_alive`.

**Helper — `_find_cell_protection_downstream(electrical, q)`.**
Replaces the generic `_find_downstream_rail` for this role because
`_find_downstream_rail` relies on `source_refdes` annotations that the
vision pass rarely produces on BMS Qs. Instead, pick the downstream
rail by net-name asymmetry:

1. Collect all unique BAT-family pin nets that are registered
   `power_rails`.
2. If none / only one: return None (insufficient topology).
3. If any carry a `FUSED|PROT|OUT|PACK` suffix: pick the unique one
   matching. That's the protected output.
4. Otherwise: fall back to `_find_downstream_rail(electrical, q)`
   (consumers-count heuristic).

**Handler — `_cascade_q_cell_protection_dead`.** Identical shape to
`_cascade_q_load_dead` but uses the new helper. Returns
`dead_rails={downstream}` and `dead_comps=consumers(downstream)` via
`_simulate_rail_loss`. On unresolvable topology returns
`_empty_cascade()`.

`cell_balancer` reuses `_cascade_passive_alive` for every mode — no
rail-level observable.

## Dispatch table — 8 new entries

Inserted below the existing `flyback_switch` block in the
`_PASSIVE_CASCADE_TABLE`:

```python
("passive_q", "cell_protection", "open"):      _cascade_q_cell_protection_dead,
("passive_q", "cell_protection", "short"):     _cascade_passive_alive,
("passive_q", "cell_protection", "stuck_on"):  _cascade_passive_alive,
("passive_q", "cell_protection", "stuck_off"): _cascade_q_cell_protection_dead,

("passive_q", "cell_balancer",   "open"):      _cascade_passive_alive,
("passive_q", "cell_balancer",   "short"):     _cascade_passive_alive,
("passive_q", "cell_balancer",   "stuck_on"):  _cascade_passive_alive,
("passive_q", "cell_balancer",   "stuck_off"): _cascade_passive_alive,
```

No change to `_SCORE_VISIBILITY` — default 1.0 is appropriate. The
balancer's `_cascade_passive_alive` naturally scores neutrally, so it
won't pollute hypothesis rankings.

## Prompt surface — Opus passive classifier

In `passive_classifier._SYSTEM_PROMPT`, the `passive_q` block gets two
new entries appended after `flyback_switch`:

```
    - cell_protection — Q in series with a battery cell or pack output
                         (source = cell-side BAT net, drain = fused /
                         pack-output BAT net). Gate controlled by the
                         BMS IC to disconnect on fault (over-discharge,
                         over-current, over-temp). Failure: channel
                         open → pack rail dead; D-S short → no fault
                         protection (silent).
    - cell_balancer   — Q + bleed resistor across a cell tap, gated by
                         the BMS to drain excess charge during balance
                         cycles. Pin pattern looks like S and D share
                         the same cell-tap net (the balance resistor
                         merges in extraction). Failure: stuck_on →
                         continuously drains that cell; open → balance
                         cycle silent, cells drift.
```

And `ComponentAssignment.role` docstring (schemas.py-adjacent
Pydantic field) extends the `passive_q` role enumeration string to
include `cell_protection · cell_balancer`.

## Prompt surface — agent diagnostic (manifest.py)

In the `Modes Q (Phase 4.5)` block of the agent diagnostic prompt,
append a new bullet after `flyback_switch`:

```
  - Sur un cell_protection (Q série d'une cellule / pack, pins sur
    BATn / BATnFUSED) : `open` / `stuck_off` = cellule déconnectée →
    rail fused côté pack dead ; `short` / `stuck_on` = plus de
    protection (observable uniquement sur surcharge / déséquilibre
    cellule, pas direct sur un rail).
  - Sur un cell_balancer (Q + R de balance passive, pins sur BATn
    répétés) : modes non observables depuis un rail. Utile comme
    cible physique d'inspection quand une cellule drift seule dans
    la télémétrie BMS.
```

## Tests

**Unit tests** in `tests/pipeline/schematic/test_passive_classifier.py`:

- `test_transistor_cell_protection_heuristic` — synthetic Q with pins
  `{BAT1, BAT1FUSED}` → `("cell_protection", 0.75)`.
- `test_transistor_cell_protection_rejects_with_gnd` — same topology
  plus a GND pin → falls through, not cell_protection.
- `test_transistor_cell_balancer_heuristic` — Q with S=D=`BAT2`, G=None
  → `("cell_balancer", 0.65)`.
- `test_transistor_cell_balancer_rejects_foreign_net` — Q with
  S=`BAT2`, D=`BAT2`, G=`EN_BMS` → falls through (the EN net is
  foreign).
- `test_cell_protection_priority_over_inrush_limiter` — Q with pins
  `{BAT, BATFUSED}` matches cell_protection, not inrush_limiter.
- `test_bat_family_pattern_accepts_known_labels` /
  `test_bat_family_pattern_rejects_unknown_labels` — regex coverage.

**Cascade test** in `tests/pipeline/schematic/test_hypothesize.py`:

- `test_cell_protection_open_kills_fused_rail` — minimal
  `ElectricalGraph` with one cell_protection Q (nets `{BAT1,
  BAT1FUSED}`, downstream rail = BAT1FUSED, one consumer). `Q.open`
  cascade yields `dead_rails={BAT1FUSED}` and the consumer in
  `dead_comps`.

**Hand-written scenarios** in `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml`:

- `mnt-reform-q-cell-protection-open-bat1fused` — `state_rails={BAT1FUSED: dead}`,
  match kind=`passive_q` role=`cell_protection` mode=`open`,
  accept_in_top_n=5.
- `mnt-reform-q-cell-protection-stuck-off-bat1fused` — same
  observation, mode=`stuck_off`, accept_in_top_n=5.

## Regeneration

After shipping, run:

```bash
.venv/bin/python scripts/regen_electrical_graph.py --slug mnt-reform-motherboard
```

The regen script (from commit `6dd8a33`) already re-runs the heuristic
fresh, so the new rules pick up Q5-Q12 with no additional work.
Expected output: 8 Q roles freshly classified, `passive_fills_reapplied`
drops by ~7 (Q6-Q12 no longer need Opus fills), and
`electrical_graph.json` gets rewritten with the BMS roles.

The Opus passive classifier will still run on future full pipeline
ingests but will now see only Q4 in its input (heuristic leaves it
None, LLM may or may not commit). That's fine.

## Acceptance criteria

1. `make test` green on the full fast suite (tests/pipeline/schematic/
   entirely green; no change to tests outside schematic/).
2. MNT Reform post-regen electrical_graph has exactly one
   `cell_protection` Q (Q5) and seven `cell_balancer` Qs
   (Q6..Q12), confirmed by a `.venv/bin/python` one-liner counting
   roles.
3. Hand-written scenario `mnt-reform-q-cell-protection-open-bat1fused`
   passes — Q5 `open` in top-5 for `state_rails={BAT1FUSED: dead}`.
4. The agent diagnostic prompt includes both new roles in the
   `Modes Q` block, verified by a grep.
5. Accuracy suite (`test_hypothesize_accuracy.py`) stays at 23
   passed / 15 skipped — no regression on existing gates.

## Files touched

- `api/pipeline/schematic/passive_classifier.py` — regex constant,
  rules 0.5 and 0.6, docstring, LLM system prompt.
- `api/pipeline/schematic/hypothesize.py` — helper, handler, 8
  dispatch entries.
- `api/agent/manifest.py` — diagnostic prompt bullet.
- `tests/pipeline/schematic/test_passive_classifier.py` — six new
  tests (two heuristic happy paths, two rejections, priority, regex
  coverage).
- `tests/pipeline/schematic/test_hypothesize.py` — one cascade test.
- `tests/pipeline/schematic/fixtures/hand_written_scenarios.yaml` —
  two scenarios.

Expected diff: ~300 lines added across six files. No deletions.

## Phased follow-ups (not in P4.6)

- **P4.6.1** — if another board surfaces a BMS with more protection
  Qs (e.g. two series FETs for charge/discharge), revisit the
  `_find_cell_protection_downstream` heuristic. Currently assumes
  exactly one `FUSED|PROT|OUT|PACK`-suffixed rail.
- **P4.6.2** — optional: extend `cell_balancer` with a topology-based
  cascade if a board ships per-cell voltage telemetry as
  `state_comps` observations. Defer until a field report actually
  needs it.
