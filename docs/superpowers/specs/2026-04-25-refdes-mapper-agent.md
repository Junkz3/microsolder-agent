# Refdes Mapper agent — Phase 2.5

**Date:** 2026-04-25
**Status:** in-progress
**Supersedes:** corrects the architectural mistake in `2026-04-24-scout-with-optional-documents-design.md`
**Related:** `docs/superpowers/specs/2026-04-22-backend-v2-knowledge-factory.md`

## What went wrong with the 2026-04-24 design

The 2026-04-24 spec gave Scout the `ElectricalGraph` directly, with the
intent that Scout would use it to *target* MPN-specific web searches and
attach refdes literally to its quotes. In practice, the LLM behind
Scout — given a free-form Markdown output target and a pre-existing
graph that already names every refdes/MPN of the device — synthesised
refdes attributions for symptoms that came from different threads or
that no source ever named. URL-by-URL audit on `mnt-reform-motherboard`
found **23/23 accepted bench scenarios with refdes attributions
absent from the cited forum threads**. The dump itself opened with a
verbatim BOM dump of the graph as the "Device overview", and every
"Likely cause" line picked refdes from the graph that supplied the
function the symptom needed.

Two patterns failed structurally:

1. **Free-form prose + structured graph input is an attractive
   nuisance.** A forced-tool call constrained by Pydantic + literal
   substring checks would not have allowed it; free Markdown does.
2. **One agent doing two incompatible jobs**: Scout was simultaneously
   the *web extractor* and the *function→refdes mapper*. The mapper
   job belongs in a forced-tool, post-extraction phase.

## Design

### Phase 2.5 — Refdes Mapper

A new sub-agent runs between Phase 2 (Registry) and Phase 3 (Writers).
It receives:

- `raw_research_dump.md` (Phase 1 output)
- `registry.json` (Phase 2 output)
- `electrical_graph.json` (technician-supplied, optional)

It emits a single forced-tool call to `submit_refdes_mappings` carrying
a list of `RefdesAttribution` items. Output persists at
`memory/{slug}/refdes_attributions.json`.

When `electrical_graph.json` is absent, the orchestrator skips this
phase entirely — the bench generator falls back to its existing rail-
overlap heuristic for the `canonical→refdes` bridge.

### Why this works where Scout-with-graph failed

Three structural protections, in order of strength:

- **Forced-tool Pydantic shape** — the model cannot write free prose;
  every field is typed, every attribution has a discrete schema. The
  generative degree of freedom is collapsed to a structured list.
- **Closed `evidence_kind` enum** — exactly two legitimate kinds
  (`literal_refdes_in_quote`, `mpn_match_in_quote`). No "topology
  inference" or "rail overlap" option exists. The model cannot smuggle
  graph-as-source under a permissive label.
- **Server-side post-validation** — every emitted attribution is
  rechecked deterministically:
  - `evidence_quote` MUST be a substring of the raw dump.
  - For `literal_refdes_in_quote`: the `refdes` MUST appear literally
    (case-insensitive) inside `evidence_quote`.
  - For `mpn_match_in_quote`: the MPN value
    `graph.components[refdes].value.mpn` MUST appear literally in
    `evidence_quote`. The graph is the only source of truth for the
    MPN; the model cannot invent an MPN.
  - Failed attributions are dropped, not allowed to bypass with
    a retry. An empty `RefdesMappings` is a legitimate, accepted output.

### Pydantic shape

```python
class RefdesAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str       # must match a registry component
    refdes: str               # must exist in graph.components
    confidence: float         # [0, 1]
    evidence_kind: Literal[
        "literal_refdes_in_quote",
        "mpn_match_in_quote",
    ]
    evidence_quote: str       # ≥ 30 chars, literal substring of dump
    reasoning: str            # ≤ 240 chars, why this attribution holds


class RefdesMappings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    device_slug: str
    attributions: list[RefdesAttribution] = Field(default_factory=list)
```

### Prompt design

`MAPPER_SYSTEM` is short, structured, and lays out a hard contract. The
key elements:

- One concrete positive example (functional name in dump + matching
  MPN in graph → attribution with `mpn_match_in_quote`).
- One concrete negative example (functional name in dump + matching
  function in graph but no MPN in dump → NO attribution, leave
  empty).
- Penalty wording ("an empty list is a correct, valid answer; an
  invented attribution is grounds for rejection").
- Closed `evidence_kind` is restated and the literal-substring rules
  are spelled out.

### Server-side validator

`api/pipeline/mapper.py::_validate_attributions(mappings, dump, registry, graph)`
runs on the LLM output before the orchestrator persists it:

```
For each a in mappings.attributions:
    1. a.canonical_name must exist in registry.components by canonical_name
    2. a.refdes must exist in graph.components
    3. a.evidence_quote must be a substring of dump (literal, case-sensitive)
    4. If a.evidence_kind == "literal_refdes_in_quote":
         a.refdes (case-insensitive) must appear in a.evidence_quote
    5. If a.evidence_kind == "mpn_match_in_quote":
         graph.components[a.refdes].value.mpn must be set
         AND must appear (case-sensitive) in a.evidence_quote
    Failure → drop the attribution with a logged warning
```

The validator returns the surviving subset. The pipeline does not retry
the LLM — failed attributions are the model's loss, not the orchestrator's.

### Integration with bench-gen

`api/pipeline/bench_generator/prompts.py::build_functional_candidate_map`
gains a third source priority:

1. **`memory/{slug}/refdes_attributions.json`** when present (NEW)
2. `registry.refdes_candidates` (legacy from 2026-04-24, kept for
   back-compat on existing packs but not produced going forward)
3. Heuristic rail-overlap (legacy fallback)

`validator.py::check_refdes_mentioned_in_quote` gains a parallel strict
path: when attributions exist for the canonical cited in the source
quote, `cause.refdes` MUST be one of those refdes (no heuristic
fallback for that canonical).

### What the orchestrator stops doing

`api/pipeline/orchestrator.py::generate_knowledge_pack`:

- **Stops** passing `graph=graph` to `run_scout`. Scout returns to its
  pre-2026-04-24 behaviour — pure web extraction in functional language.
  The Scout user prompt no longer contains any "# Provided
  ElectricalGraph" / "# Provided boardview" / "# Provided local
  datasheets" sections.
- **Stops** passing `graph=graph` to `run_registry_builder`. Registry
  returns to its pre-2026-04-24 behaviour — pure canonical vocabulary,
  no `refdes_candidates` field emitted. The schema-level field stays
  on `RegistryComponent` for legacy pack compat.
- **Adds** `run_mapper()` call after Registry, only when `graph` is
  loaded.

The 2026-04-24 SCOUT_SYSTEM "When you have local documents" section
becomes dead code. We leave it in the file as a no-op (the Scout user
prompt no longer triggers it) until a follow-up commit removes it.

## File-by-file change list

| File | Change |
|------|--------|
| `api/pipeline/schemas.py` | + `RefdesAttribution`, `RefdesMappings` |
| `api/pipeline/mapper.py` | NEW — `run_mapper`, `_validate_attributions` |
| `api/pipeline/prompts.py` | + `MAPPER_SYSTEM`, `MAPPER_USER_TEMPLATE` |
| `api/pipeline/orchestrator.py` | drop `graph=` from Scout/Registry, insert `run_mapper` after Registry, persist `refdes_attributions.json` |
| `api/pipeline/bench_generator/prompts.py` | `_candidates_from_attributions` reads JSON file; map_priority = file → registry → heuristic |
| `api/pipeline/bench_generator/validator.py` | V2b.1 strict-via-attributions when file present |
| `tests/pipeline/test_mapper.py` | NEW — prompt, validator, integration |
| `tests/pipeline/test_orchestrator_uploads.py` | extend — file is created when graph is loaded |
| `tests/pipeline/test_scout_with_graph.py` | DEPRECATE / remove if no longer reachable from orchestrator |

## Out of scope

- Reading datasheet PDFs into LLM context. Same as 2026-04-24 spec —
  Scout cites them via `local://` URLs only when Scout is given the
  block, which the orchestrator no longer does. The
  `# Provided local datasheets` block is now dead.
- Multi-rev attribution (MNT R-2 vs v3.0 board variants having
  different refdes mappings). Out of scope; any pack maps to one graph.
- Confidence-weighted attribution merging when multiple evidence_kinds
  agree. V1 keeps highest-confidence attribution per `canonical_name`.

## Validation gates

1. `make test` passes.
2. Mapper validator unit tests reject all five negative paths
   (canonical missing, refdes not in graph, quote not in dump, refdes
   not in quote for literal kind, MPN absent / not in quote for MPN kind).
3. Pipeline regen on `mnt-reform-motherboard`:
   - `raw_research_dump.md` device overview must NOT cite any of
     U2/U4/U7/U10/U17/U20/U24/U27/Q5–Q12/FB15/FB18/FB20/FB21 by
     refdes (those are graph-only, never in forum threads).
   - `refdes_attributions.json` exists and contains only
     attributions whose evidence quotes match the literal contract.
4. Bench-gen run on regenerated pack:
   - URL-by-URL audit on every accepted scenario passes (every
     refdes attribution traces to either a literal refdes mention OR
     a literal MPN mention in the actual forum thread, OR a Mapper
     attribution that was server-side-validated).
   - Score and cascade_recall reported but treated as secondary —
     the gate is honesty, not volume.

## Cost expectation

One additional Sonnet forced-tool call per pipeline run. Input ≈ dump
(~6k tokens) + registry (~3k) + graph compact summary (~5k) = ~14k
tokens. Output ≈ 5–15 attributions × ~80 tokens = ~1k tokens.
At Sonnet 4.6 pricing: 14k×$3/M + 1k×$15/M = ~$0.06 per run.

Total pipeline cost delta vs 2026-04-24: **+~$0.06 per run** for the
new phase, **−~$0** for Scout/Registry (same calls, smaller user
prompts). Effectively no cost change, much higher trust.
