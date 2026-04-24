# Scout with optional technician-provided documents

**Date:** 2026-04-24
**Status:** in-progress
**Supersedes:** none — extends `2026-04-22-backend-v2-knowledge-factory.md`
**Companion plan:** TODO-scout-with-optional-documents.md (handoff prompt)

## Problem

The `api/pipeline/` knowledge factory runs Scout with `device_label` only.
The Scout dump (`raw_research_dump.md`) speaks in functional language —
"LPC controller won't wake up", "charge board dead" — and never carries
refdes or rail labels. The bench auto-generator (`api/pipeline/bench_generator/`)
compensates with a deterministic rail-overlap heuristic (`build_functional_candidate_map`)
that maps registry canonicals to refdes by token-sharing on rail names. It
works partially (4 accepted scenarios on `mnt-reform-motherboard`, score 0.728
on commit `011e024`) but misses entities that don't source a same-named rail
(CPU SOM, charge board, eDP cable, J1).

When the technician *does* have a schematic PDF, a boardview, or datasheets
for the device on the bench, the pipeline should use them: target Scout's
web searches at MPN-specific failure modes, attach refdes literally to
quotes when justified, and let the Registry Builder emit grounded
`refdes_candidates` rather than relying on rail-overlap inference.

When the technician provides nothing → behaviour stays exactly as today.
This is **strictly additive**.

## Hard contracts (non-negotiable)

These rules separate "Scout enriched by documents" from "Scout that invents
plausible-looking attribution":

1. **Provenance URL externe obligatoire.** Every quote in the dump still
   needs a `source_url` pointing at an externally verifiable document.
   The schematic / boardview is *targeting* — never *ground truth* for a
   quote. Local datasheets cited as `local://datasheets/{filename}` are
   acceptable provenance only when the file is part of the upload bundle.

2. **MPN-based search only.** Scout may read "U7 has MPN=LM2677" from the
   ElectricalGraph and run `web_search "LM2677 failure site:ti.com"`. It
   may NOT read "U7 sources +5V" from the graph and write "Source-X says
   U7 failure causes +5V to die" without finding Source-X literally. The
   topology is targeting, not testimony.

3. **`refdes_candidates` justified.** When the registry phase emits
   `refdes_candidates`, each candidate's `evidence` must be either
   (a) a quote that links the canonical to the refdes via MPN/datasheet,
   or (b) `"inference from BOM MPN match"` (BOM being technician-supplied,
   so locally authoritative).

4. **No graph-as-source fallback.** If Scout finds no external source
   citing the refdes, Scout doesn't create a scenario for it. The graph
   is never a primary source.

## Surface

### A. Upload endpoint (new)

```http
POST /pipeline/packs/{device_slug}/documents
  multipart/form-data:
    - file:        binary
    - kind:        "schematic_pdf" | "boardview" | "datasheet" | "notes" | "other"
    - description: free text (optional)
```

Storage: `memory/{slug}/uploads/{ISO-timestamp}-{kind}-{filename}`. No
auto-processing. The technician triggers `POST /pipeline/generate`
(or `POST /pipeline/repairs`, which fires it) afterwards.

Per-device, not per-repair: uploads enrich the device pack and benefit
every future repair on the same device. `memory/{slug}/uploads/` is
already inside the gitignored memory tree.

### B. Orchestrator wiring

`generate_knowledge_pack(device_label, …)` extends the constructor with
an optional `uploaded_documents_dir: Path | None = None` (defaults to
`memory/{slug}/uploads/`). At pipeline start, before Scout:

1. Scan `uploads/` and group by `kind`. Schematic/boardview kinds are
   most-recent-wins, datasheet/notes/other accumulate.

2. **Schematic ingestion gate.** If a `schematic_pdf` upload is present
   AND `electrical_graph.json` is missing, run `ingest_schematic()`
   inline before Scout. If it fails, log and continue without graph.
   If `electrical_graph.json` already exists (technician used
   `/pipeline/ingest-schematic` directly, or a previous run produced it),
   skip ingestion regardless of any `schematic_pdf` upload.

3. **Boardview parsing.** If a boardview upload is present, parse via
   `api.board.parser.parser_for(path)`. On failure, log and continue
   without `Board`.

4. **Datasheet collection.** Collect `datasheet` upload paths into a
   list. No reading, no inlining — Scout is told they exist and may
   cite them as `local://datasheets/{filename}`.

5. Pass `graph: ElectricalGraph | None`, `board: Board | None`,
   `datasheet_paths: list[Path]` to `run_scout` and `graph` to
   `run_registry_builder`.

### C. `run_scout` signature extension

```python
async def run_scout(
    *,
    client: AsyncAnthropic,
    model: str,
    device_label: str,
    graph: ElectricalGraph | None = None,        # NEW
    board: Board | None = None,                  # NEW
    datasheet_paths: list[Path] | None = None,   # NEW
    max_continuations: int = 3,
    min_symptoms: int = 3,
    min_components: int = 3,
    min_sources: int = 3,
    max_retries: int = 1,
    stats: PhaseTokenStats | None = None,
) -> str: ...
```

When all three optional args are None / empty → user prompt is exactly
today's `SCOUT_USER_TEMPLATE.format(device_label=...)`. No regression.

When provided, the user prompt grows by additional sections, in this
order (all clearly delimited so the LLM can ignore any block it doesn't
need):

```
## Provided ElectricalGraph (compiled from technician-supplied schematic)
- Components: {refdes} kind={kind} role={role} mpn={mpn or '—'}
- Power rails: {label} v={voltage_nominal} source_refdes={src} consumers=[…]
- Boot phases: {index} {name}

## Provided boardview (technician-supplied)
- {Part.refdes} value={Part.value} footprint={Part.footprint}

## Provided datasheets
- local://datasheets/{filename}     (cite as source_url when relevant)
```

### D. `SCOUT_SYSTEM` extension

A new section "WHEN YOU HAVE LOCAL DOCUMENTS" gets appended to the
existing system prompt. It restates contracts 1–4 above, with examples
of legitimate vs illegitimate moves, and tells Scout:

- Use the MPN list to seed targeted `web_search` queries.
- When a quote describes a failure of a part you can identify by MPN,
  cite that part's refdes inside the quote's "Components mentioned".
- Quote the rail labels from the graph when a source describes the rail
  by its symptomatic behavior; never quote a rail label that the source
  didn't justify.
- Datasheets cited as `local://datasheets/...` are valid only when their
  filename appears in the "Provided datasheets" block.

### E. Registry Builder extension

`run_registry_builder` takes `graph: ElectricalGraph | None = None`. When
present, the user prompt includes a graph summary block and the system
prompt is extended with a "REFDES CANDIDATES" section:

> For every component in the registry, look at the graph block. If any
> graph refdes plausibly matches the canonical (by MPN match in the
> dump quote, by alias-token match against rail label that refdes
> sources, or by direct refdes mention in the dump), emit a
> `refdes_candidates: [...]` entry on the registry component. Each
> candidate carries `{refdes, confidence, evidence}` where `evidence`
> cites the quote or "inference from BOM MPN match". Empty list when
> no candidate can be justified — never invent.

When `graph` is None → registry shape stays exactly as today (no
`refdes_candidates` field). Pydantic schema makes the field
`list[RefdesCandidate] | None = None` so legacy packs reload untouched.

### F. Bench-generator simplification

`api/pipeline/bench_generator/prompts.py::build_functional_candidate_map`
becomes "prefer registry, fall back to heuristic":

- If `registry["components"][i].refdes_candidates` is non-empty → use
  those directly.
- If absent or empty → run today's `score_refdes_for_canonical`
  heuristic (unchanged code path, unchanged behaviour for legacy packs).

`validator.py::check_refdes_mentioned_in_quote` (V2b.1) tightens its
path-(b) check: when registry entries carry `refdes_candidates`,
`cause.refdes` MUST appear in that list (not just be heuristically
score-able). This closes a latent invention surface.

### G. Pydantic schema additions

`api/pipeline/schemas.py`:

```python
class RefdesCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refdes: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str

class RegistryComponent(BaseModel):
    # … existing fields …
    refdes_candidates: list[RefdesCandidate] | None = None
```

### H. Tests

- `tests/pipeline/test_scout_with_graph.py` — mocks the Anthropic client,
  asserts the user prompt contains the graph summary section when
  `graph` is provided and is byte-identical to today when not.
- `tests/pipeline/test_registry_refdes_candidates.py` — feeds a tiny
  `ElectricalGraph` + `raw_dump`, asserts that the schema validation
  accepts `refdes_candidates` and that the registry call wires them
  through.
- `tests/pipeline/bench_generator/test_validator.py` — extend with two
  cases: (a) registry has `refdes_candidates` → V2b.1 accepts only
  candidates from that list, (b) registry has no `refdes_candidates`
  → fallback to heuristic, current behaviour unchanged.

### I. Runtime validation

Re-run `scripts/generate_bench_from_pack.py --slug mnt-reform-motherboard`
in two configurations:

- **Baseline**: `memory/mnt-reform-motherboard/uploads/` empty.
  Expected: 4 accepted, score ≈ 0.728 (matches commit `011e024`).
- **Enriched**: `electrical_graph.json` already on disk from the
  earlier ingestion (the technician's schematic_pdf path).
  Target: ≥ 8 accepted, score ≥ 0.85, cascade_recall ≥ 0.80.

Audit scenario-by-scenario; reject anything where the quote doesn't
literally support the cited refdes/rail.

## Out of scope

- Reading datasheet PDFs into the LLM context (Anthropic Documents API).
  V1 only lets Scout *cite* local datasheets, not read them.
- Per-repair uploads. Uploads attach to the device pack, not the repair
  session. Future iteration if needed.
- Re-ingesting an existing schematic PDF when the user uploads a new
  version. V1: `electrical_graph.json` presence is the gate; the
  technician deletes the file to force re-ingest.

## File-by-file change list

| File | Change |
|------|--------|
| `api/pipeline/schemas.py` | + `RefdesCandidate`; `RegistryComponent.refdes_candidates` |
| `api/pipeline/scout.py` | extend signature + user-prompt assembly |
| `api/pipeline/prompts.py` | extend `SCOUT_SYSTEM`; extend `REGISTRY_SYSTEM` |
| `api/pipeline/registry.py` | accept optional `graph`; thread into prompt |
| `api/pipeline/orchestrator.py` | scan uploads, ingest schematic, parse boardview, thread to scout+registry |
| `api/pipeline/__init__.py` | + `POST /pipeline/packs/{slug}/documents` |
| `api/pipeline/bench_generator/prompts.py` | prefer `refdes_candidates` over heuristic |
| `api/pipeline/bench_generator/validator.py` | V2b.1 honors `refdes_candidates` strictly |
| `tests/pipeline/test_scout_with_graph.py` | new |
| `tests/pipeline/test_registry_refdes_candidates.py` | new |
| `tests/pipeline/bench_generator/test_validator.py` | extend |

## Validation gates before merge

1. `make test` passes (no regression in existing tests).
2. Baseline bench-gen run reproduces score 0.728 ± 0.01 on `mnt-reform-motherboard`.
3. Enriched bench-gen run hits the targets above with all accepted
   scenarios audited as legitimate (quote literally justifies the
   cited refdes/rail).
